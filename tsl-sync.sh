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
DELAY="${DELAY:-3}"              # pause (secondes) entre deux requêtes aux serveurs GD
FORCE="${FORCE:-0}"              # 1 = re-télécharger tous les niveaux pour comparer
NC_URL="${NC_URL:-}"             # ex : https://cloud.exemple.fr  (vide = pas d'envoi)
NC_USER="${NC_USER:-}"
NC_PASS="${NC_PASS:-}"           # mot de passe d'application Nextcloud
NC_DIR="${NC_DIR:-TSL}"          # dossier de destination dans Nextcloud
UPLOAD_AT_END="${UPLOAD_AT_END:-1}"  # 1 = tout envoyer à Nextcloud à la fin ; 0 = au fur et à mesure
DAILY="${DAILY:-1}"              # 1 = un dossier par jour (AAAA-MM-JJ) avec les niveaux nouveaux/modifiés ce jour-là
RESYNC="${RESYNC:-0}"            # 1 = renvoyer tous les fichiers locaux vers Nextcloud
GMD_BINARY_VERSION="${GMD_BINARY_VERSION:-47}"  # version du format de sauvegarde du jeu (clé k50, 47 = GD 2.2)
AWK_PARSER="${AWK_PARSER:-$SCRIPT_DIR/nuxt_levels.awk}"

STATE="$DATA_DIR/state.tsv"      # id  version  sha256  liste  chemin_relatif
LOG="$DATA_DIR/changements.log"
FILES="$DATA_DIR/fichiers"
TMP="$(mktemp -d)"
DAY="${TSL_DAY:-$(date +%F)}"
trap 'rm -rf "$TMP"' EXIT
trap 'log "Interrompu (l'"'"'état est sauvegardé, le prochain passage reprendra ici)"; exit 130' INT TERM

# Tous les messages vont sur la sortie d'erreur, pour ne jamais se mélanger aux données.
log()  { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }
note() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG" "$TMP/run.notes" >&2; }
die()  { log "ERREUR: $*"; exit 1; }

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

GD_BLOCKED=0
gd_post() {  # gd_post <endpoint> <données> <fichier de sortie>
    # Les réponses (parfois plusieurs Mo) restent dans des fichiers : les manipulations
    # de chaînes de bash sont beaucoup trop lentes sur de gros textes.
    # code retour : 0 = ok, 1 = erreur sur ce niveau, 2 = serveurs GD saturés / limite atteinte
    local ep="$1" data="$2" out="$3" head err try wait
    for try in 1 2 3; do
        sleep "$DELAY"
        curl -sS -m 180 -A "" -X POST "$GD_SERVER/$ep" -d "$data&secret=$GD_SECRET" -o "$out" 2>"$TMP/curl.err" || : > "$out"
        head="$(head -c 120 "$out" | tr -d '\r\n')"
        case "$head" in
            ""|"error code: 1015"*|"error code: 429"*|"error code: 1020"*)
                err="${head:-$(tr '\n' ' ' < "$TMP/curl.err")}"; err="${err:-réponse vide}"
                wait=$((try * 60))
                log "  serveurs GD : $err — nouvelle tentative dans $wait s ($try/3)"
                sleep "$wait" ;;
            "error code:"*) log "  serveurs GD : $head"; return 1 ;;
            *) return 0 ;;
        esac
    done
    log "  Les serveurs GD refusent les requêtes (limite atteinte). Arrêt des téléchargements pour ce passage ;"
    log "  ce qui est déjà téléchargé est conservé et le prochain passage reprendra la suite."
    return 2
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

nc_put_now() {  # envoie <chemin relatif dans FILES>
    local rel="$1" dir code
    dir="$(dirname "$rel")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -T "$FILES/$rel" "$(nc_dav "$rel")")" || code=000
    case "$code" in 200|201|204) return 0 ;; *) log "  Nextcloud : échec de l'envoi de $rel (HTTP $code)"; return 1 ;; esac
}

nc_delete_now() {
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X DELETE "$(nc_dav "$1")")" || code=000
    case "$code" in 200|204|404) return 0 ;; *) log "  Nextcloud : échec de la suppression de $1 (HTTP $code)"; return 1 ;; esac
}

nc_copy_now() {  # copie côté serveur Nextcloud <source> -> <destination>, sans renvoyer le fichier
    local src="$1" dst="$2" dir code
    dir="$(dirname "$dst")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X COPY \
        -H "Destination: $(nc_dav "$dst")" -H "Overwrite: T" "$(nc_dav "$src")")" || code=000
    case "$code" in 201|204) return 0 ;; *) log "  Nextcloud : échec de la copie de $src vers $dst (HTTP $code)"; return 1 ;; esac
}

# Les envois sont mis en file d'attente ($QUEUE) et faits en une fois à la fin
# (UPLOAD_AT_END=1, par défaut). Ce qui échoue reste dans la file pour le passage suivant.
QUEUE="$DATA_DIR/a_envoyer.tsv"
nc_put() {
    nc_enabled || return 0
    if [ "$UPLOAD_AT_END" = 1 ]; then printf 'PUT\t%s\n' "$1" >> "$QUEUE"; else nc_put_now "$1"; fi
}
nc_delete() {
    nc_enabled || return 0
    if [ "$UPLOAD_AT_END" = 1 ]; then printf 'DEL\t%s\n' "$1" >> "$QUEUE"; else nc_delete_now "$1" || true; fi
}

nc_copy() {
    nc_enabled || return 0
    if [ "$UPLOAD_AT_END" = 1 ]; then printf 'CPY\t%s\t%s\n' "$1" "$2" >> "$QUEUE"; else nc_copy_now "$1" "$2"; fi
}

nc_flush() {  # traite la file d'attente
    nc_enabled || return 0
    [ -s "$QUEUE" ] || return 0
    local op rel dst total cnt=0 ok=0 ko=0
    # garde seulement la dernière opération par fichier, dans l'ordre
    awk -F'\t' '{ k = ($1 == "CPY") ? $3 : $2; key[NR] = k; last[k] = NR; line[NR] = $0 }
        END { for (i = 1; i <= NR; i++) if (last[key[i]] == i) print line[i] }' \
        "$QUEUE" > "$TMP/queue"
    total="$(wc -l < "$TMP/queue")"
    log "Envoi vers Nextcloud ($total fichiers)"
    : > "$TMP/queue.fail"
    while IFS=$'\t' read -r op rel dst; do
        cnt=$((cnt + 1)); [ $((cnt % 50)) -eq 0 ] && log "  $cnt / $total"
        if [ "$op" = CPY ]; then nc_copy_now "$rel" "$dst" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\t%s\n' "$op" "$rel" "$dst" >> "$TMP/queue.fail"; }
        elif [ "$op" = DEL ]; then nc_delete_now "$rel" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\n' "$op" "$rel" >> "$TMP/queue.fail"; }
        elif [ -f "$FILES/$rel" ]; then nc_put_now "$rel" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\n' "$op" "$rel" >> "$TMP/queue.fail"; }
        fi
    done < "$TMP/queue"
    mv "$TMP/queue.fail" "$QUEUE"
    log "Nextcloud : $ok envoyés, $ko en échec$([ "$ko" -gt 0 ] && echo " (réessayés au prochain passage)")"
    [ "$ko" -eq 0 ]
}

nc_resync() {  # met en file tous les fichiers locaux (RESYNC=1)
    nc_enabled || return 0
    (cd "$FILES" && find . -type f ! -name '.*' | sed 's#^\./##' | sort) | while IFS= read -r rel; do
        printf 'PUT\t%s\n' "$rel"
    done >> "$QUEUE"
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

# ---------------------------------------------------------------- 0. mise à niveau des anciens .gmd
# Les premières versions du script n'écrivaient pas la clé k50 (version du format) :
# sans elle, le jeu importe un niveau vide. On l'ajoute sans re-télécharger.
fixed=0
while IFS= read -r -d '' f; do
    if ! grep -q '<k>k50</k>' "$f"; then
        sed -i "s#<k>k21</k><i>2</i>#<k>k21</k><i>2</i><k>k50</k><i>$GMD_BINARY_VERSION</i>#" "$f"
        nc_put "${f#"$FILES/"}"; fixed=$((fixed + 1))
    fi
done < <(find "$FILES" -name '*.gmd' -print0)
[ "$fixed" -gt 0 ] && log "Mise à niveau du format de $fixed fichiers .gmd (ajout de k50)"

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
        rc=0; gd_post getGJLevels21.php "type=10&str=$batch" "$TMP/search" || rc=$?
        [ "$rc" -eq 2 ] && { GD_BLOCKED=1; break; }
        [ "$rc" -eq 0 ] || continue
        [ "$(head -c 2 "$TMP/search")" = "-1" ] && continue
        # niveaux séparés par "|", avant le premier "#" ; champs clé:valeur ; 1 = id, 5 = version
        while IFS=$'\t' read -r gid gver; do
            [ -n "$gid" ] && GD_VER[$gid]="$gver"
        done < <(cut -d'#' -f1 "$TMP/search" | tr '|' '\n' | awk -F: '{ id = ""; v = "";
            for (j = 1; j < NF; j += 2) { if ($j == "1") id = $(j + 1); else if ($j == "5") v = $(j + 1) }
            if (id != "") print id "\t" v }')
    done
fi

# ---------------------------------------------------------------- 3. téléchargements
make_gmd() {  # make_gmd <fichier réponse (partie niveau)> <nom> <auteur> <fichier>
    local raw="$1" name="$2" author="$3" out="$4"
    awk -F: -v BV="$GMD_BINARY_VERSION" -v NAME="$(xml_escape "$name")" -v AUTHOR="$(xml_escape "$author")" '
    { for (j = 1; j < NF; j += 2) f[$j] = $(j + 1) }
    END {
        if (f["4"] == "") exit 1
        printf "<?xml version=\"1.0\"?><plist version=\"1.0\" gjver=\"2.0\"><dict>"
        printf "<k>kCEK</k><i>4</i><k>k2</k><s>%s</s>", NAME
        if (f["3"] != "") printf "<k>k3</k><s>%s</s>", f["3"]
        printf "<k>k4</k><s>%s</s><k>k5</k><s>%s</s>", f["4"], AUTHOR
        printf "<k>k13</k><t /><k>k21</k><i>2</i><k>k50</k><i>%d</i><k>k16</k><i>%d</i>", BV, (f["5"] == "" ? 1 : f["5"])
        if (f["45"] + 0 > 0) printf "<k>k48</k><i>%d</i>", f["45"]
        if (f["12"] + 0 > 0) printf "<k>k8</k><i>%d</i>", f["12"]
        if (f["35"] + 0 > 0) printf "<k>k45</k><i>%d</i>", f["35"]
        printf "</dict></plist>"
    }' "$raw" > "$out"
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
    rc=0; gd_post downloadGJLevel22.php "levelID=$id" "$TMP/resp" || rc=$?
    [ "$rc" -eq 2 ] && { GD_BLOCKED=1; failed=$((failed + 1)); break; }
    [ "$rc" -eq 0 ] || { failed=$((failed + 1)); continue; }
    if [ "$(head -c 2 "$TMP/resp")" != "1:" ]; then
        log "  niveau introuvable sur les serveurs GD (supprimé ou privé ?)"; failed=$((failed + 1)); continue
    fi
    cut -d'#' -f1 "$TMP/resp" | tr -d '\r\n' > "$TMP/raw"   # partie niveau (avant le premier #)
    if ! make_gmd "$TMP/raw" "$name" "$author" "$TMP/level.gmd"; then
        log "  réponse invalide, ignoré"; failed=$((failed + 1)); continue
    fi
    ver="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "5") print $(j + 1) }' "$TMP/raw")"
    sha="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "4") printf "%s", $(j + 1) }' "$TMP/raw" \
        | sha256sum | cut -d' ' -f1)"

    changed=0
    if [ -z "${ST_SHA[$id]:-}" ] || [ ! -f "$FILES/${ST_FILE[$id]:-/nonexistent}" ]; then
        [ -z "${ST_SHA[$id]:-}" ] && { note "[$lst] NOUVEAU : $name ($id) v$ver"; new=$((new + 1)); changed=1; }
    elif [ "$sha" != "${ST_SHA[$id]}" ]; then
        # niveau modifié : on archive l'ancienne version
        hist="historique/$(safe_name "$name") ($id)/${DAY}_v${ST_VER[$id]}.gmd"
        mkdir -p "$FILES/$(dirname "$hist")" && cp "$FILES/${ST_FILE[$id]}" "$FILES/$hist" && nc_put "$hist"
        note "[$lst] MODIFIÉ : $name ($id) v${ST_VER[$id]} -> v$ver (ancienne version dans $hist)"
        updated=$((updated + 1)); changed=1
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
    [ "$DAILY" = 1 ] && [ "$changed" = 1 ] && nc_copy "$rel" "$DAY/$rel"   # copie dans le dossier du jour
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
if [ "$DAILY" = 1 ]; then   # journal du jour (cumulé si plusieurs passages le même jour)
    mkdir -p "$FILES/$DAY"
    { printf '=== Passage du %s ===\n' "$(date '+%F %T')"
      if [ -s "$TMP/run.notes" ]; then cat "$TMP/run.notes"; else echo "Aucun changement."; fi
      echo; } >> "$FILES/$DAY/changements.log"
    nc_put "$DAY/changements.log"
fi
[ "$RESYNC" = 1 ] && nc_resync
nc_flush || failed=$((failed + 1))

log "Terminé : $new nouveaux, $updated modifiés, $same inchangés, $failed en échec"
[ "$failed" -eq 0 ]
