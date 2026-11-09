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
        code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X MKCOL \
            "${NC_URL%/}/remote.php/dav/files/$(urlencode "$NC_USER")/$(urlencode "$acc")")" || code=000
        case "$code" in 201|405) NC_DIRS_OK[$acc]=1 ;; *) log "  Nextcloud : impossible de créer $acc (HTTP $code)"; return 1 ;; esac
    done
}

nc_put_now() {  # envoie <chemin relatif dans FILES>
    local rel="$1" dir code
    dir="$(dirname "$rel")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -T "$FILES/$rel" "$(nc_dav "$rel")")" || code=000
    case "$code" in 200|201|204) return 0 ;; *) log "  Nextcloud : échec de l'envoi de $rel (HTTP $code)"; return 1 ;; esac
}

nc_delete_now() {
    local code
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X DELETE "$(nc_dav "$1")")" || code=000
    case "$code" in 200|204|404) return 0 ;; *) log "  Nextcloud : échec de la suppression de $1 (HTTP $code)"; return 1 ;; esac
}

nc_copy_now() {  # copie côté serveur Nextcloud <source> -> <destination>, sans renvoyer le fichier
    local src="$1" dst="$2" dir code
    dir="$(dirname "$dst")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X COPY \
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

# ---------------------------------------------------------------- Discord
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"   # URL du webhook Discord (vide = pas de notification)
DISCORD_ALWAYS="${DISCORD_ALWAYS:-0}"    # 1 = envoyer un résumé même s'il n'y a aucun changement
notify() { printf '%s\n' "$*" >> "$TMP/discord.txt"; }

json_string() {  # échappe un texte pour JSON (stdin -> stdout, avec guillemets)
    awk 'BEGIN { printf "\"" }
        { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, " "); gsub(/\r/, "")
          printf "%s%s", (NR > 1 ? "\\n" : ""), $0 }
        END { printf "\"" }'
}

discord_post() {  # discord_post <texte>
    local code try
    { printf '{"content":'; printf '%s' "$1" | json_string; printf ',"allowed_mentions":{"parse":[]}}'; } > "$TMP/discord.json"
    for try in 1 2 3; do
        code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -X POST \
            --data-binary "@$TMP/discord.json" "$DISCORD_WEBHOOK")" || code=000
        case "$code" in
            2??) sleep 1; return 0 ;;
            429) sleep 5 ;;
            *) log "  Discord : échec de l'envoi (HTTP $code)"; return 1 ;;
        esac
    done
    return 1
}

DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"  # si défini, les annonces sont publiées par le bot (tsl_bot.py)

bot_queue() {  # dépose les annonces pour le bot Discord
    [ -n "$DISCORD_BOT_TOKEN" ] || return 0
    [ -s "$TMP/discord.txt" ] || [ "$DISCORD_ALWAYS" = 1 ] || return 0
    mkdir -p "$DATA_DIR/notifications"
    local f="$DATA_DIR/notifications/$(date +%Y%m%d-%H%M%S)-$$"
    { printf '📋 **TSL** — passage du %s\n' "$(date '+%d/%m/%Y %H:%M')"
      if [ -s "$TMP/discord.txt" ]; then cat "$TMP/discord.txt"; else echo "✅ Aucun changement."; fi
      printf '_%s_\n' "$1"; } > "$f.tmp" && mv "$f.tmp" "$f.txt"
}

discord_send() {  # envoie les notifications accumulées, en messages de moins de 2000 caractères
    bot_queue "$1"
    [ -n "$DISCORD_WEBHOOK" ] || return 0
    local summary="$1" msg line
    if [ ! -s "$TMP/discord.txt" ]; then
        [ "$DISCORD_ALWAYS" = 1 ] && discord_post "✅ **TSL** — passage du $(date '+%d/%m/%Y %H:%M') : aucun changement (${summary})"
        return 0
    fi
    msg="📋 **TSL** — passage du $(date '+%d/%m/%Y %H:%M')"
    while IFS= read -r line; do
        if [ $(( ${#msg} + ${#line} + 1 )) -gt 1900 ]; then discord_post "$msg"; msg=""; fi
        msg="${msg:+$msg$'\n'}$line"
    done < "$TMP/discord.txt"
    discord_post "$msg"$'\n'"_${summary}_"
    log "Notifications Discord envoyées"
}

# ---------------------------------------------------------------- état
# state.tsv : id  version  sha256(données)  liste  fichier  infos  statut(ok|supprime)
declare -A ST_VER=() ST_SHA=() ST_LIST=() ST_FILE=() ST_INFO=() ST_STATUS=()
while IFS=$'\t' read -r id ver sha lst file info status; do
    [ -n "$id" ] || continue
    [ "$ver" = - ] && ver=""; [ "$sha" = - ] && sha=""; [ "$lst" = - ] && lst=""
    [ "$file" = - ] && file=""; [ "${info:-}" = - ] && info=""
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$file"
    ST_INFO[$id]="${info:-}"; ST_STATUS[$id]="${status:-ok}"
done < "$STATE"
FIRST_RUN=0; [ "${#ST_VER[@]}" -eq 0 ] && FIRST_RUN=1

save_state() {
    local id
    for id in "${!ST_VER[@]}"; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "${ST_VER[$id]:--}" "${ST_SHA[$id]:--}" "${ST_LIST[$id]:--}" \
            "${ST_FILE[$id]:--}" "${ST_INFO[$id]:--}" "${ST_STATUS[$id]:-ok}"
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
        while IFS= read -r line; do
            case "$line" in
                *"ENTRÉE dans la liste"*) notify "➕ ${line}" ;;
                *"SORTIE de la liste"*)   notify "➖ ${line}" ;;
            esac
        done < "$TMP/$lst.moves"
        moved="$(grep -c 'CLASSEMENT' "$TMP/$lst.moves" || true)"
        [ "${moved:-0}" -gt 0 ] && notify "↕️ [$lst] $moved niveau(x) ont changé de place dans le classement"
    fi

    while IFS=$'\t' read -r pos id name author; do
        [ -n "${WANT_LIST[$id]:-}" ] && continue   # déjà présent dans une liste précédente
        WANT_LIST[$id]="$lst"; WANT_POS[$id]="$pos"; WANT_NAME[$id]="$name"; WANT_AUTHOR[$id]="$author"
        ORDER+=("$id")
    done < "$TMP/$lst.tsv"
done

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

# Infos comparées en plus du contenu (données du niveau) : champ GD -> libellé
INFO_FIELDS="2 3 12 35 52 53 15 37 18 19 42 9 17 43 25"
info_label() {
    case "$1" in
        2) echo "nom" ;; 3) echo "description" ;; 12) echo "musique officielle" ;; 35) echo "musique" ;;
        52) echo "musiques" ;; 53) echo "effets sonores" ;; 15) echo "longueur" ;; 37) echo "pièces" ;;
        18) echo "étoiles" ;; 19) echo "featured" ;; 42) echo "epic/legendary/mythic" ;;
        9|17|43|25) echo "difficulté" ;; *) echo "champ $1" ;;
    esac
}
info_diff() {  # info_diff <ancien> <nouveau> -> libellés des champs modifiés, séparés par ", "
    awk -v A="$1" -v B="$2" 'BEGIN {
        na = split(A, a, "\037"); nb = split(B, b, "\037")
        for (i = 1; i <= na; i++) { p = index(a[i], "="); if (p) { k = substr(a[i], 1, p - 1); oa[k] = substr(a[i], p + 1); keys[k] = 1 } }
        for (i = 1; i <= nb; i++) { p = index(b[i], "="); if (p) { k = substr(b[i], 1, p - 1); ob[k] = substr(b[i], p + 1); keys[k] = 1 } }
        for (k in keys) if (oa[k] != ob[k]) print k }' | while read -r k; do info_label "$k"; done | sort -u | paste -sd, - | sed 's/,/, /g'
}
gdlink() { printf '[%s](<https://gdbrowser.com/%s>)' "$1" "$2"; }

new=0; updated=0; infos=0; same=0; failed=0; deleted=0; n_done=0; total=${#ORDER[@]}
log "Vérification complète de $total niveaux (contenu téléchargé et comparé)"
for id in "${ORDER[@]}"; do
    lst="${WANT_LIST[$id]}"; name="${WANT_NAME[$id]}"; author="${WANT_AUTHOR[$id]}"
    rel="$lst/$(safe_name "$name") ($id).gmd"
    old="${ST_FILE[$id]:-}"

    n_done=$((n_done + 1))
    log "Téléchargement [$n_done/$total] $name ($id)"
    rc=0; gd_post downloadGJLevel22.php "levelID=$id" "$TMP/resp" || rc=$?
    [ "$rc" -eq 2 ] && { GD_BLOCKED=1; failed=$((failed + 1)); break; }
    [ "$rc" -eq 0 ] || { failed=$((failed + 1)); continue; }

    if [ "$(head -c 2 "$TMP/resp")" != "1:" ]; then
        # niveau absent des serveurs GD : supprimé ou rendu privé
        if [ "${ST_STATUS[$id]:-ok}" != supprime ]; then
            note "[$lst] SUPPRIMÉ des serveurs GD (ou rendu privé) : $name ($id) — la dernière copie est conservée"
            notify "🗑️ [$lst] **Supprimé** des serveurs GD (ou rendu privé) : $(gdlink "$name" "$id") de $author"
            ST_STATUS[$id]=supprime; ST_LIST[$id]="$lst"; ST_VER[$id]="${ST_VER[$id]:-}"
            ST_SHA[$id]="${ST_SHA[$id]:-}"; ST_FILE[$id]="$old"; ST_INFO[$id]="${ST_INFO[$id]:-}"
            save_state
        fi
        deleted=$((deleted + 1)); continue
    fi

    cut -d'#' -f1 "$TMP/resp" | tr -d '\r\n' > "$TMP/raw"   # partie niveau (avant le premier #)
    if ! make_gmd "$TMP/raw" "$name" "$author" "$TMP/level.gmd"; then
        log "  réponse invalide, ignoré"; failed=$((failed + 1)); continue
    fi
    ver="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "5") print $(j + 1) }' "$TMP/raw")"
    sha="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "4") printf "%s", $(j + 1) }' "$TMP/raw" \
        | sha256sum | cut -d' ' -f1)"
    info="$(awk -F: -v F="$INFO_FIELDS" 'BEGIN { n = split(F, w, " ") }
        { for (j = 1; j < NF; j += 2) f[$j] = $(j + 1) }
        END { for (i = 1; i <= n; i++) printf "%s%s=%s", (i > 1 ? "\037" : ""), w[i], f[w[i]] }' "$TMP/raw")"

    if [ "${ST_STATUS[$id]:-ok}" = supprime ]; then
        note "[$lst] RÉAPPARU sur les serveurs GD : $name ($id)"
        notify "♻️ [$lst] **Réapparu** sur les serveurs GD : $(gdlink "$name" "$id")"
        ST_STATUS[$id]=ok
    fi

    changed=0
    if [ -z "${ST_SHA[$id]:-}" ] || [ -z "$old" ] || [ ! -f "$FILES/$old" ]; then
        if [ -z "${ST_SHA[$id]:-}" ]; then note "[$lst] NOUVEAU : $name ($id) v$ver"; new=$((new + 1)); changed=1; fi
    elif [ "$sha" != "${ST_SHA[$id]}" ]; then
        # contenu du niveau modifié : on archive l'ancienne version
        hist="historique/$(safe_name "$name") ($id)/${DAY}_v${ST_VER[$id]}.gmd"
        mkdir -p "$FILES/$(dirname "$hist")" && cp "$FILES/$old" "$FILES/$hist" && nc_put "$hist"
        extra=""
        if [ -n "${ST_INFO[$id]:-}" ] && [ "$info" != "${ST_INFO[$id]}" ]; then extra="$(info_diff "${ST_INFO[$id]}" "$info")"; fi
        note "[$lst] MODIFIÉ : $name ($id) v${ST_VER[$id]} -> v$ver${extra:+ (infos : $extra)} (ancienne version dans $hist)"
        notify "✏️ [$lst] **Niveau modifié** : $(gdlink "$name" "$id") de $author (v${ST_VER[$id]} → v$ver)${extra:+ — infos aussi modifiées : $extra}"
        updated=$((updated + 1)); changed=1
    elif [ -n "${ST_INFO[$id]:-}" ] && [ "$info" != "${ST_INFO[$id]}" ]; then
        what="$(info_diff "${ST_INFO[$id]}" "$info")"
        note "[$lst] INFOS MODIFIÉES : $name ($id) : $what"
        notify "📝 [$lst] Infos modifiées pour $(gdlink "$name" "$id") : $what"
        infos=$((infos + 1)); changed=1
    else
        same=$((same + 1))
    fi
    if [ "$changed" = 0 ] && [ "$old" = "$rel" ] && [ "$ver" = "${ST_VER[$id]}" ] && [ -n "${ST_INFO[$id]:-}" ]; then
        ST_LIST[$id]="$lst"; continue   # rien n'a changé
    fi

    mkdir -p "$FILES/$lst"
    mv "$TMP/level.gmd" "$FILES/$rel"
    if [ -n "$old" ] && [ "$old" != "$rel" ] && [ -f "$FILES/$old" ]; then
        unlink "$FILES/$old"; nc_delete "$old"
        [ "$changed" = 0 ] && note "[$lst] RENOMMÉ : $old -> $rel"
    fi
    nc_put "$rel"
    [ "$DAILY" = 1 ] && [ "$changed" = 1 ] && nc_copy "$rel" "$DAY/$rel"   # copie dans le dossier du jour
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$rel"; ST_INFO[$id]="$info"
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

summary="$new nouveaux, $updated modifiés, $infos infos modifiées, $deleted supprimés, $same inchangés, $failed en échec"
[ "$FIRST_RUN" = 1 ] && [ "$new" -gt 0 ] && notify "📦 Premier passage : $new niveaux archivés dans Nextcloud."
[ "${GD_BLOCKED:-0}" = 1 ] && notify "⚠️ Les serveurs GD ont limité les requêtes : passage interrompu, la suite sera faite au prochain passage."
discord_send "$summary"
printf '%s — %s\n' "$(date '+%d/%m/%Y %H:%M')" "$summary" > "$DATA_DIR/dernier_passage.txt"
log "Terminé : $summary"
[ "$failed" -eq 0 ]
