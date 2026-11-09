#!/usr/bin/env bash
# tsl-sync.sh — Télécharge les niveaux de The Shitty List (TSL / TSL+) en .gmd,
# détecte les mises à jour des niveaux et les envoie dans Nextcloud.
#
# Dépendances : bash 4+, curl, awk, sed, sha256sum (présents sur quasiment tout Linux).
# Configuration : /etc/tsl-sync.conf (voir tsl-sync.conf.example) ou variable TSL_CONF.

set -u -o pipefail
export LC_ALL=C.UTF-8 2>/dev/null || export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${TSL_CONF:-/etc/tsl-sync.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

DATA_DIR="${DATA_DIR:-/var/lib/tsl-sync}"
LISTS="${LISTS:-tsl tslplus}"
LIST_BASE_URL="${LIST_BASE_URL:-https://tsl.pages.dev/list}"
GD_SERVER="${GD_SERVER:-https://www.boomlings.com/database}"
GD_SECRET="${GD_SECRET:-Wmfd2893gb7}"
DELAY="${DELAY:-2}"              # pause (secondes) entre deux requêtes aux serveurs GD
FORCE="${FORCE:-0}"              # 1 = re-télécharger tous les niveaux pour comparer
NC_URL="${NC_URL:-}"             # ex : https://cloud.exemple.fr  (vide = pas d'envoi)
NC_USER="${NC_USER:-}"
NC_PASS="${NC_PASS:-}"           # mot de passe d'application Nextcloud
NC_DIR="${NC_DIR:-TSL}"          # dossier de destination dans Nextcloud
AWK_PARSER="${AWK_PARSER:-$SCRIPT_DIR/nuxt_levels.awk}"

STATE="$DATA_DIR/state.tsv"      # id  version  sha256  liste  chemin_relatif
LOG="$DATA_DIR/changements.log"
FILES="$DATA_DIR/fichiers"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

log()  { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
note() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }
die()  { log "ERREUR: $*" >&2; exit 1; }

for cmd in curl awk sed sha256sum od; do command -v "$cmd" >/dev/null || die "commande manquante : $cmd"; done
[ -f "$AWK_PARSER" ] || die "parser introuvable : $AWK_PARSER"
mkdir -p "$FILES" || die "impossible de créer $DATA_DIR"
touch "$STATE" "$LOG"

# Un seul passage à la fois (cron)
if command -v flock >/dev/null; then
    exec 9>"$DATA_DIR/.lock"
    flock -n 9 || { log "déjà en cours, abandon"; exit 0; }
fi

# ---------------------------------------------------------------- utilitaires
xml_escape() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

safe_name() {  # nom de fichier sans caractères interdits (Windows / Nextcloud)
    local s="$1"
    s="$(printf '%s' "$s" | tr '/\\:*?"<>|' '_________' | tr -d '\000-\037')"
    s="${s#"${s%%[![:space:].]*}"}"; s="${s%"${s##*[![:space:].]}"}"
    [ -n "$s" ] || s="niveau"
    printf '%s' "$s"
}

urlencode() {  # encode chaque octet hors [A-Za-z0-9.~_/-]
    printf '%s' "$1" | od -An -v -tx1 | tr -s ' \n' '  ' | awk '{
        for (i = 1; i <= NF; i++) {
            c = $i; v = index("0123456789abcdef", substr(c, 1, 1)) * 16 + index("0123456789abcdef", substr(c, 2, 1)) - 17
            if ((v >= 48 && v <= 57) || (v >= 65 && v <= 90) || (v >= 97 && v <= 122) || v == 45 || v == 46 || v == 47 || v == 95 || v == 126)
                printf "%c", v
            else printf "%%%s", toupper(c)
        } }'
}

gd_post() {  # gd_post <endpoint> <données> -> réponse sur stdout ; gère la limite de requêtes
    local ep="$1" data="$2" resp try
    for try in 1 2 3 4; do
        sleep "$DELAY"
        resp="$(curl -sS -m 60 -A "" -X POST "$GD_SERVER/$ep" \
            -d "$data&gameVersion=22&binaryVersion=42&secret=$GD_SECRET")" || resp=""
        case "$resp" in
            ""|"error code: 1015"*|"error code: 429"*) log "  serveur GD indisponible / limite atteinte, nouvelle tentative dans $((try * 30)) s"; sleep $((try * 30)) ;;
            "error code:"*) log "  serveur GD : $resp"; return 1 ;;
            *) printf '%s' "$resp"; return 0 ;;
        esac
    done
    return 1
}

# ---------------------------------------------------------------- Nextcloud
declare -A NC_DIRS_OK=()
nc_enabled() { [ -n "$NC_URL" ] && [ -n "$NC_USER" ] && [ -n "$NC_PASS" ]; }
nc_dav() { printf '%s/remote.php/dav/files/%s/%s' "${NC_URL%/}" "$(urlencode "$NC_USER")" "$(urlencode "${NC_DIR%/}/$1")"; }

nc_mkdirs() {  # crée récursivement le dossier distant <rel>
    local rel="$1" acc="" part code
    IFS='/' read -ra parts <<< "${NC_DIR%/}/$rel"
    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        acc="${acc:+$acc/}$part"
        [ -n "${NC_DIRS_OK[$acc]:-}" ] && continue
        code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X MKCOL \
            "${NC_URL%/}/remote.php/dav/files/$(urlencode "$NC_USER")/$(urlencode "$acc")")" || code=000
        case "$code" in 201|405) NC_DIRS_OK[$acc]=1 ;; *) log "  Nextcloud : impossible de créer $acc (HTTP $code)"; return 1 ;; esac
    done
}

nc_put() {  # nc_put <chemin relatif dans FILES>
    nc_enabled || return 0
    local rel="$1" dir code
    dir="$(dirname "$rel")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -T "$FILES/$rel" "$(nc_dav "$rel")")" || code=000
    case "$code" in 200|201|204) return 0 ;; *) log "  Nextcloud : échec de l'envoi de $rel (HTTP $code)"; return 1 ;; esac
}

nc_delete() {
    nc_enabled || return 0
    curl -sS -o /dev/null -u "$NC_USER:$NC_PASS" -X DELETE "$(nc_dav "$1")" || true
}

# ---------------------------------------------------------------- état
declare -A ST_VER=() ST_SHA=() ST_LIST=() ST_FILE=() GD_VER=()
while IFS=$'\t' read -r id ver sha lst file; do
    [ -n "$id" ] || continue
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$file"
done < "$STATE"

save_state() {
    local id
    for id in "${!ST_VER[@]}"; do
        printf '%s\t%s\t%s\t%s\t%s\n' "$id" "${ST_VER[$id]}" "${ST_SHA[$id]}" "${ST_LIST[$id]}" "${ST_FILE[$id]}"
    done | sort -n > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

# ---------------------------------------------------------------- 1. listes
declare -A WANT_LIST=() WANT_POS=() WANT_NAME=() WANT_AUTHOR=()
ORDER=()
for lst in $LISTS; do
    log "Lecture de la liste $lst"
    if ! curl -sS -m 60 -f "$LIST_BASE_URL/$lst" -o "$TMP/$lst.html"; then
        die "impossible de lire $LIST_BASE_URL/$lst (rien n'est modifié)"
    fi
    sed -n 's/.*id="__NUXT_DATA__"[^>]*>\(.*\)/\1/p' "$TMP/$lst.html" | sed 's#</script>.*##' \
        | awk -f "$AWK_PARSER" > "$TMP/$lst.tsv"
    nb="$(wc -l < "$TMP/$lst.tsv")"
    [ "$nb" -gt 0 ] || die "aucun niveau trouvé dans la liste $lst (le site a peut-être changé)"
    log "  $nb niveaux"

    old_index="$FILES/$lst/index.tsv"
    if [ -f "$old_index" ]; then  # changements de classement
        awk -F'\t' -v L="$lst" '
            NR == FNR { if (FNR > 1) { op[$2] = $1; on[$2] = $3 } next }
            FNR > 1 { seen[$2] = 1
                if (!($2 in op)) printf "[%s] ENTRÉE dans la liste : #%s %s (%s)\n", L, $1, $3, $2
                else if (op[$2] != $1) printf "[%s] CLASSEMENT : %s (%s) #%s -> #%s\n", L, $3, $2, op[$2], $1 }
            END { for (i in op) if (!(i in seen)) printf "[%s] SORTIE de la liste : %s (%s), était #%s\n", L, on[i], i, op[i] }
        ' "$old_index" <(printf 'position\tid\tnom\tauteur\n'; cat "$TMP/$lst.tsv") > "$TMP/$lst.moves"
        while IFS= read -r line; do note "$line"; done < "$TMP/$lst.moves"
    fi

    while IFS=$'\t' read -r pos id name author; do
        [ -n "${WANT_LIST[$id]:-}" ] && continue   # déjà présent dans une liste précédente
        WANT_LIST[$id]="$lst"; WANT_POS[$id]="$pos"; WANT_NAME[$id]="$name"; WANT_AUTHOR[$id]="$author"
        ORDER+=("$id")
    done < "$TMP/$lst.tsv"
done

# ---------------------------------------------------------------- 2. versions (par lots de 10)
if [ "$FORCE" != 1 ] && [ "${#ST_VER[@]}" -gt 0 ]; then   # inutile au tout premier passage
    log "Vérification des versions sur les serveurs GD (${#ORDER[@]} niveaux, ~$(( (${#ORDER[@]} + 9) / 10 * (DELAY + 1) / 60 + 1 )) min)"
    for ((i = 0; i < ${#ORDER[@]}; i += 10)); do
        [ $((i % 100)) -eq 0 ] && [ "$i" -gt 0 ] && log "  $i / ${#ORDER[@]}"
        batch="$(IFS=,; echo "${ORDER[*]:i:10}")"
        resp="$(gd_post getGJLevels21.php "type=10&str=$batch")" || continue
        [ "${resp:0:2}" = "-1" ] && continue
        # niveaux séparés par "|", avant le premier "#" ; champs clé:valeur ; 1 = id, 5 = version
        while IFS=$'\t' read -r gid gver; do
            [ -n "$gid" ] && GD_VER[$gid]="$gver"
        done < <(printf '%s' "${resp%%#*}" | tr '|' '\n' | awk -F: '{ id = ""; v = "";
            for (j = 1; j < NF; j += 2) { if ($j == "1") id = $(j + 1); else if ($j == "5") v = $(j + 1) }
            if (id != "") print id "\t" v }')
    done
fi

# ---------------------------------------------------------------- 3. téléchargements
make_gmd() {  # make_gmd <réponse brute> <nom> <auteur> <fichier>
    local raw="${1%%#*}" name="$2" author="$3" out="$4"
    printf '%s' "$raw" | awk -F: -v NAME="$(xml_escape "$name")" -v AUTHOR="$(xml_escape "$author")" '
    { for (j = 1; j < NF; j += 2) f[$j] = $(j + 1) }
    END {
        if (f["4"] == "") exit 1
        printf "<?xml version=\"1.0\"?><plist version=\"1.0\" gjver=\"2.0\"><dict>"
        printf "<k>kCEK</k><i>4</i><k>k2</k><s>%s</s>", NAME
        if (f["3"] != "") printf "<k>k3</k><s>%s</s>", f["3"]
        printf "<k>k4</k><s>%s</s><k>k5</k><s>%s</s>", f["4"], AUTHOR
        printf "<k>k13</k><t /><k>k21</k><i>2</i><k>k16</k><i>%d</i>", (f["5"] == "" ? 1 : f["5"])
        if (f["12"] + 0 > 0) printf "<k>k8</k><i>%d</i>", f["12"]
        if (f["35"] + 0 > 0) printf "<k>k45</k><i>%d</i>", f["35"]
        printf "</dict></plist>"
    }' > "$out"
}

new=0; updated=0; same=0; failed=0; n_done=0
for id in "${ORDER[@]}"; do
    lst="${WANT_LIST[$id]}"; name="${WANT_NAME[$id]}"; author="${WANT_AUTHOR[$id]}"
    rel="$lst/$(safe_name "$name") ($id).gmd"

    # rien à faire si la version n'a pas bougé
    if [ "$FORCE" != 1 ] && [ -n "${ST_VER[$id]:-}" ] && [ "${GD_VER[$id]:-}" = "${ST_VER[$id]}" ] \
        && [ -f "$FILES/${ST_FILE[$id]}" ]; then
        if [ "${ST_FILE[$id]}" != "$rel" ]; then   # renommé ou changé de liste
            mkdir -p "$FILES/$lst" && mv "$FILES/${ST_FILE[$id]}" "$FILES/$rel"
            nc_put "$rel" && nc_delete "${ST_FILE[$id]}"
            note "[$lst] RENOMMÉ : ${ST_FILE[$id]} -> $rel"
            ST_FILE[$id]="$rel"; ST_LIST[$id]="$lst"
        fi
        same=$((same + 1)); continue
    fi

    n_done=$((n_done + 1))
    log "Téléchargement [$n_done] $name ($id)"
    resp="$(gd_post downloadGJLevel22.php "levelID=$id")" || { failed=$((failed + 1)); continue; }
    if [ "${resp:0:2}" = "-1" ] || [ "${resp:0:2}" != "1:" ]; then
        log "  niveau introuvable sur les serveurs GD (supprimé ou privé ?)"; failed=$((failed + 1)); continue
    fi
    if ! make_gmd "$resp" "$name" "$author" "$TMP/level.gmd"; then
        log "  réponse invalide, ignoré"; failed=$((failed + 1)); continue
    fi
    ver="$(printf '%s' "${resp%%#*}" | awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "5") print $(j + 1) }')"
    sha="$(printf '%s' "${resp%%#*}" | awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "4") printf "%s", $(j + 1) }' \
        | sha256sum | cut -d' ' -f1)"

    if [ -z "${ST_SHA[$id]:-}" ] || [ ! -f "$FILES/${ST_FILE[$id]:-/nonexistent}" ]; then
        [ -z "${ST_SHA[$id]:-}" ] && { note "[$lst] NOUVEAU : $name ($id) v$ver"; new=$((new + 1)); }
    elif [ "$sha" != "${ST_SHA[$id]}" ]; then
        # niveau modifié : on archive l'ancienne version
        hist="historique/$(safe_name "$name") ($id)/$(date +%F)_v${ST_VER[$id]}.gmd"
        mkdir -p "$FILES/$(dirname "$hist")" && cp "$FILES/${ST_FILE[$id]}" "$FILES/$hist" && nc_put "$hist"
        note "[$lst] MODIFIÉ : $name ($id) v${ST_VER[$id]} -> v$ver (ancienne version dans $hist)"
        updated=$((updated + 1))
    else
        same=$((same + 1))
        if [ "$ver" = "${ST_VER[$id]}" ] && [ "${ST_FILE[$id]}" = "$rel" ]; then continue; fi
    fi

    mkdir -p "$FILES/$lst"
    if [ -n "${ST_FILE[$id]:-}" ] && [ "${ST_FILE[$id]}" != "$rel" ] && [ -f "$FILES/${ST_FILE[$id]}" ]; then
        rm -f "$FILES/${ST_FILE[$id]}"; nc_delete "${ST_FILE[$id]}"
    fi
    mv "$TMP/level.gmd" "$FILES/$rel"
    nc_put "$rel"
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$rel"
    save_state   # sauvegarde au fil de l'eau : une interruption ne fait rien perdre
done
save_state

# ---------------------------------------------------------------- 4. index + journal
for lst in $LISTS; do
    mkdir -p "$FILES/$lst"
    { printf 'position\tid\tnom\tauteur\tfichier\n'
      while IFS=$'\t' read -r pos id name author; do
          printf '%s\t%s\t%s\t%s\t%s\n' "$pos" "$id" "$name" "$author" "${ST_FILE[$id]:-(non téléchargé)}"
      done < "$TMP/$lst.tsv"; } > "$FILES/$lst/index.tsv"
    nc_put "$lst/index.tsv"
done
cp "$LOG" "$FILES/changements.log" && nc_put "changements.log"

log "Terminé : $new nouveaux, $updated modifiés, $same inchangés, $failed en échec"
[ "$failed" -eq 0 ]
