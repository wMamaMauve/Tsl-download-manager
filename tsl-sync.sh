#!/usr/bin/env bash
# tsl-sync.sh — Downloads the levels of The Shitty List (TSL / TSL+) as .gmd files,
# detects level changes and uploads everything to Nextcloud.
#
# Dependencies: bash 4+, curl, awk, sed, sha256sum (available on almost any Linux).
# Configuration: /etc/tsl-sync.conf (see tsl-sync.conf.example) or the TSL_CONF variable.

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
DELAY="${DELAY:-3}"              # pause (seconds) between two requests to the GD servers
NC_URL="${NC_URL:-}"             # e.g. https://cloud.example.com  (empty = no upload)
NC_USER="${NC_USER:-}"
NC_PASS="${NC_PASS:-}"           # Nextcloud app password
NC_DIR="${NC_DIR:-TSL}"          # destination folder in Nextcloud
UPLOAD_AT_END="${UPLOAD_AT_END:-1}"  # 1 = upload everything to Nextcloud at the end; 0 = as we go
DAILY="${DAILY:-1}"              # 1 = one folder per day (YYYY-MM-DD) with the levels new/changed that day
RESYNC="${RESYNC:-0}"            # 1 = re-upload every local file to Nextcloud
GMD_BINARY_VERSION="${GMD_BINARY_VERSION:-47}"  # game save format version (key k50, 47 = GD 2.2)
AWK_PARSER="${AWK_PARSER:-$SCRIPT_DIR/nuxt_levels.awk}"
DIFF_TOOL="${DIFF_TOOL:-$SCRIPT_DIR/level_diff.sh}"   # object-level comparison of two versions
RECORD_CHECK="${RECORD_CHECK:-1}"   # 1 = check that the verification / showcase / record videos still work
RECORD_TOOL="${RECORD_TOOL:-$SCRIPT_DIR/record_check.sh}"
RECORD_ANNOUNCE_MAX="${RECORD_ANNOUNCE_MAX:-10}"   # videos listed one by one on Discord (then a count)

STATE="$DATA_DIR/state.tsv"
LOG="$DATA_DIR/changes.log"
FILES="$DATA_DIR/files"
QUEUE="$DATA_DIR/upload_queue.tsv"
TMP="$(mktemp -d)"
DAY="${TSL_DAY:-$(date +%F)}"
PROGRESS="$DATA_DIR/progress.txt"   # current step, read by the Discord bot (status)
OWN_PROGRESS=0                       # 1 once this run holds the lock and owns $PROGRESS
trap 'rm -rf "$TMP"; [ "$OWN_PROGRESS" = 1 ] && rm -f "$PROGRESS"' EXIT
trap 'log "Interrupted (state is saved, the next run will resume from here)"; exit 130' INT TERM

# All messages go to stderr so they never get mixed with data.
log()  { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }
note() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG" "$TMP/run.notes" >&2; }
die()  { log "ERROR: $*"; exit 1; }
progress() { [ "$OWN_PROGRESS" = 1 ] && printf '%s\n' "$*" > "$PROGRESS" 2>/dev/null; true; }   # progress <text>


for cmd in curl awk sed sha256sum od; do command -v "$cmd" >/dev/null || die "missing command: $cmd"; done
[ -f "$AWK_PARSER" ] || die "parser not found: $AWK_PARSER"
mkdir -p "$DATA_DIR" || die "cannot create $DATA_DIR"

# Only one run at a time (cron/systemd)
if command -v flock >/dev/null; then
    exec 9>"$DATA_DIR/.lock"
    flock -n 9 || { log "already running, giving up"; exit 0; }
fi
OWN_PROGRESS=1

# What to do: all (default), levels (levels only), records (videos only).
# Given as first argument, as RUN_MODE, or left by the Discord bot's /run in next_run_mode.
# A run started by hand (/run on Discord, or in a terminal) always posts its result on Discord;
# the automatic run of the night only posts when something changed.
MANUAL="${RUN_MANUAL:-0}"
{ [ -f "$DATA_DIR/next_run_mode" ] || [ $# -gt 0 ] || [ -t 0 ]; } && MANUAL=1
DISCORD_ALWAYS="$MANUAL"
RUN_MODE="${1:-${RUN_MODE:-}}"
if [ -z "$RUN_MODE" ] && [ -f "$DATA_DIR/next_run_mode" ]; then
    RUN_MODE="$(head -c 20 "$DATA_DIR/next_run_mode" | tr -dc 'a-z')"
fi
rm -f "$DATA_DIR/next_run_mode"
case "${RUN_MODE:-all}" in
    all|levels|records) RUN_MODE="${RUN_MODE:-all}" ;;
    *) die "unknown run mode '$RUN_MODE' (all, levels or records)" ;;
esac
[ "$RUN_MODE" = levels ] && RECORD_CHECK=0
[ "$RUN_MODE" = records ] && export RECORD_CHECK_DAYS=0   # asked on purpose: check every video again
case "$RUN_MODE" in
    levels)  progress "Starting a run (levels only)" ;;
    records) progress "Starting a run (record videos only)" ;;
    *)       progress "Starting a run" ;;
esac
log "Run mode: $RUN_MODE ($([ "$MANUAL" = 1 ] && echo "started by hand: result always posted" || echo "automatic: posted only if something changed"))"

# ---------------------------------------------------------------- migration from the French version
# Earlier versions used French file and folder names: rename them once.
MIGRATE_REMOTE=0
if [ -d "$DATA_DIR/fichiers" ] && [ ! -d "$FILES" ]; then mv "$DATA_DIR/fichiers" "$FILES"; MIGRATE_REMOTE=1; fi
[ -f "$DATA_DIR/changements.log" ] && [ ! -f "$LOG" ] && mv "$DATA_DIR/changements.log" "$LOG"
[ -f "$DATA_DIR/a_envoyer.tsv" ] && [ ! -f "$QUEUE" ] && mv "$DATA_DIR/a_envoyer.tsv" "$QUEUE"
[ -f "$DATA_DIR/dernier_passage.txt" ] && mv "$DATA_DIR/dernier_passage.txt" "$DATA_DIR/last_run.txt"
mkdir -p "$FILES" || die "cannot create $FILES"
if [ -d "$FILES/historique" ] && [ ! -d "$FILES/history" ]; then mv "$FILES/historique" "$FILES/history"; MIGRATE_REMOTE=1; fi
[ -f "$FILES/changements.log" ] && mv "$FILES/changements.log" "$FILES/changes.log"
for d in "$FILES"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
    [ -f "$d/changements.log" ] && mv "$d/changements.log" "$d/changes.log"
done
[ "$MIGRATE_REMOTE" = 1 ] && touch "$DATA_DIR/.migrate_remote"
touch "$STATE" "$LOG"

# ---------------------------------------------------------------- utilities
xml_escape() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

safe_name() {  # file name without forbidden characters (Windows / Nextcloud)
    local s="$1"
    s="$(printf '%s' "$s" | tr '/\\:*?"<>|' '_________' | tr -d '\000-\037')"
    s="${s#"${s%%[![:space:].]*}"}"; s="${s%"${s##*[![:space:].]}"}"
    [ -n "$s" ] || s="level"
    printf '%s' "$s"
}

urlencode() {  # percent-encodes every byte outside [A-Za-z0-9.~_/-]
    printf '%s' "$1" | od -An -v -tx1 | tr -s ' \n' '  ' | awk '{
        for (i = 1; i <= NF; i++) {
            c = $i; v = index("0123456789abcdef", substr(c, 1, 1)) * 16 + index("0123456789abcdef", substr(c, 2, 1)) - 17
            if ((v >= 48 && v <= 57) || (v >= 65 && v <= 90) || (v >= 97 && v <= 122) || v == 45 || v == 46 || v == 47 || v == 95 || v == 126)
                printf "%c", v
            else printf "%%%s", toupper(c)
        } }'
}

GD_BLOCKED=0
gd_post() {  # gd_post <endpoint> <data> <output file>
    # Responses (sometimes several MB) stay in files: bash string operations are far
    # too slow on large texts.
    # return code: 0 = ok, 1 = error for this level, 2 = GD servers overloaded / rate limited
    local ep="$1" data="$2" out="$3" head err try wait
    for try in 1 2 3; do
        sleep "$DELAY"
        curl -sS -m 180 -A "" -X POST "$GD_SERVER/$ep" -d "$data&secret=$GD_SECRET" -o "$out" 2>"$TMP/curl.err" || : > "$out"
        head="$(head -c 120 "$out" | tr -d '\r\n')"
        case "$head" in
            ""|"error code: 1015"*|"error code: 429"*|"error code: 1020"*)
                err="${head:-$(tr '\n' ' ' < "$TMP/curl.err")}"; err="${err:-empty response}"
                wait=$((try * 60))
                log "  GD servers: $err — retrying in $wait s ($try/3)"
                sleep "$wait" ;;
            "error code:"*) log "  GD servers: $head"; return 1 ;;
            *) return 0 ;;
        esac
    done
    log "  The GD servers are refusing requests (rate limited). Stopping downloads for this run;"
    log "  what was already downloaded is kept and the next run will continue."
    return 2
}

# ---------------------------------------------------------------- Nextcloud
declare -A NC_DIRS_OK=()
nc_enabled() { [ -n "$NC_URL" ] && [ -n "$NC_USER" ] && [ -n "$NC_PASS" ]; }
nc_dav() { printf '%s/remote.php/dav/files/%s/%s' "${NC_URL%/}" "$(urlencode "$NC_USER")" "$(urlencode "${NC_DIR%/}/$1")"; }

nc_mkdirs() {  # recursively creates the remote folder <rel>
    local rel="$1" acc="" part code
    IFS='/' read -ra parts <<< "${NC_DIR%/}/$rel"
    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        acc="${acc:+$acc/}$part"
        [ -n "${NC_DIRS_OK[$acc]:-}" ] && continue
        code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X MKCOL \
            "${NC_URL%/}/remote.php/dav/files/$(urlencode "$NC_USER")/$(urlencode "$acc")")" || code=000
        case "$code" in 201|405) NC_DIRS_OK[$acc]=1 ;; *) log "  Nextcloud: cannot create $acc (HTTP $code)"; return 1 ;; esac
    done
}

nc_put_now() {  # uploads <path relative to FILES>
    local rel="$1" dir code
    dir="$(dirname "$rel")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -T "$FILES/$rel" "$(nc_dav "$rel")")" || code=000
    case "$code" in 200|201|204) return 0 ;; *) log "  Nextcloud: upload of $rel failed (HTTP $code)"; return 1 ;; esac
}

nc_delete_now() {
    local code
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X DELETE "$(nc_dav "$1")")" || code=000
    case "$code" in 200|204|404) return 0 ;; *) log "  Nextcloud: deletion of $1 failed (HTTP $code)"; return 1 ;; esac
}

nc_copy_now() {  # server-side Nextcloud copy <source> -> <destination>, without re-uploading
    local src="$1" dst="$2" dir code
    dir="$(dirname "$dst")"; [ "$dir" = "." ] && dir=""
    nc_mkdirs "$dir" || return 1
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X COPY \
        -H "Destination: $(nc_dav "$dst")" -H "Overwrite: T" "$(nc_dav "$src")")" || code=000
    case "$code" in 201|204) return 0 ;; *) log "  Nextcloud: copy of $src to $dst failed (HTTP $code)"; return 1 ;; esac
}

nc_move_now() {  # server-side Nextcloud rename <source> -> <destination> (missing source = ok)
    local code
    code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -u "$NC_USER:$NC_PASS" -X MOVE \
        -H "Destination: $(nc_dav "$2")" -H "Overwrite: F" "$(nc_dav "$1")")" || code=000
    case "$code" in 201|204|404|412) return 0 ;; *) log "  Nextcloud: rename of $1 failed (HTTP $code)"; return 1 ;; esac
}

# Uploads are queued ($QUEUE) and done all at once at the end (UPLOAD_AT_END=1, default).
# Whatever fails stays in the queue for the next run.
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

nc_flush() {  # processes the upload queue
    nc_enabled || return 0
    [ -s "$QUEUE" ] || return 0
    local op rel dst total cnt=0 ok=0 ko=0
    # keep only the last operation per file, in order
    awk -F'\t' '{ k = ($1 == "CPY") ? $3 : $2; key[NR] = k; last[k] = NR; line[NR] = $0 }
        END { for (i = 1; i <= NR; i++) if (last[key[i]] == i) print line[i] }' \
        "$QUEUE" > "$TMP/queue"
    total="$(wc -l < "$TMP/queue")"
    log "Uploading to Nextcloud ($total files)"
    : > "$TMP/queue.fail"
    while IFS=$'\t' read -r op rel dst; do
        cnt=$((cnt + 1)); [ $((cnt % 50)) -eq 0 ] && log "  $cnt / $total"
        [ $((cnt % 10)) -eq 0 ] && progress "Uploading to Nextcloud $cnt/$total"
        if [ "$op" = CPY ]; then nc_copy_now "$rel" "$dst" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\t%s\n' "$op" "$rel" "$dst" >> "$TMP/queue.fail"; }
        elif [ "$op" = DEL ]; then nc_delete_now "$rel" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\n' "$op" "$rel" >> "$TMP/queue.fail"; }
        elif [ -f "$FILES/$rel" ]; then nc_put_now "$rel" && ok=$((ok + 1)) || { ko=$((ko + 1)); printf '%s\t%s\n' "$op" "$rel" >> "$TMP/queue.fail"; }
        fi
    done < "$TMP/queue"
    mv "$TMP/queue.fail" "$QUEUE"
    log "Nextcloud: $ok uploaded, $ko failed$([ "$ko" -gt 0 ] && echo " (retried on the next run)")"
    [ "$ko" -eq 0 ]
}

nc_resync() {  # queues every local file (RESYNC=1)
    nc_enabled || return 0
    (cd "$FILES" && find . -type f ! -name '.*' | sed 's#^\./##' | sort) | while IFS= read -r rel; do
        printf 'PUT\t%s\n' "$rel"
    done >> "$QUEUE"
}

# Renames the French folder names in Nextcloud once (see migration above).
if [ -f "$DATA_DIR/.migrate_remote" ] && nc_enabled; then
    log "Renaming the old French folders in Nextcloud"
    mig_ok=1
    nc_move_now "historique" "history" || mig_ok=0
    nc_move_now "changements.log" "changes.log" || mig_ok=0
    for d in "$FILES"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
        [ -d "$d" ] && { nc_move_now "${d##*/}/changements.log" "${d##*/}/changes.log" || mig_ok=0; }
    done
    [ "$mig_ok" = 1 ] && unlink "$DATA_DIR/.migrate_remote"
fi

# ---------------------------------------------------------------- Discord
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"      # Discord webhook URL (empty = disabled)
DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"  # if set, announcements are posted by the bot (tsl_bot.py)
notify() { printf '%s\n' "$*" >> "$TMP/discord.txt"; }

json_string() {  # escapes text for JSON (stdin -> stdout, with quotes)
    awk 'BEGIN { printf "\"" }
        { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, " "); gsub(/\r/, "")
          printf "%s%s", (NR > 1 ? "\\n" : ""), $0 }
        END { printf "\"" }'
}

discord_post() {  # discord_post <text>
    local code try
    { printf '{"content":'; printf '%s' "$1" | json_string; printf ',"allowed_mentions":{"parse":[]}}'; } > "$TMP/discord.json"
    for try in 1 2 3; do
        code="$(curl -sS -m 120 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -X POST \
            --data-binary "@$TMP/discord.json" "$DISCORD_WEBHOOK")" || code=000
        case "$code" in
            2??) sleep 1; return 0 ;;
            429) sleep 5 ;;
            *) log "  Discord: sending failed (HTTP $code)"; return 1 ;;
        esac
    done
    return 1
}

bot_queue() {  # leaves the announcements for the Discord bot
    [ -n "$DISCORD_BOT_TOKEN" ] || return 0
    [ -s "$TMP/discord.txt" ] || [ "$DISCORD_ALWAYS" = 1 ] || return 0
    mkdir -p "$DATA_DIR/notifications"
    local f="$DATA_DIR/notifications/$(date +%Y%m%d-%H%M%S)-$$"
    { printf '**TSL** — run of %s\n' "$(date '+%Y-%m-%d %H:%M')"
      if [ -s "$TMP/discord.txt" ]; then cat "$TMP/discord.txt"; else echo "✅ No changes were detected."; fi
      printf '_%s_\n' "$1"; } > "$f.tmp" && mv "$f.tmp" "$f.txt"
}

discord_send() {  # sends the collected announcements, in messages under 2000 characters
    bot_queue "$1"
    [ -n "$DISCORD_WEBHOOK" ] || return 0
    local summary="$1" msg line
    if [ ! -s "$TMP/discord.txt" ]; then
        [ "$DISCORD_ALWAYS" = 1 ] && discord_post "**TSL Tracking result**"$'\n'"✅ No changes were detected."
        return 0
    fi
    msg="**TSL Tracking result**"
    while IFS= read -r line; do
        if [ $(( ${#msg} + ${#line} + 1 )) -gt 1900 ]; then discord_post "$msg"; msg=""; fi
        msg="${msg:+$msg$'\n'}$line"
    done < "$TMP/discord.txt"
    discord_post "$msg"
    log "Discord announcements sent"
}

# ---------------------------------------------------------------- state
# state.tsv: id  version  sha256(level data)  list  file  info  status(ok|deleted)
# Empty fields are written "-": read with IFS=tab merges consecutive tabs.
declare -A ST_VER=() ST_SHA=() ST_LIST=() ST_FILE=() ST_INFO=() ST_STATUS=()
while IFS=$'\t' read -r id ver sha lst file info status; do
    [ -n "$id" ] || continue
    [ "$ver" = - ] && ver=""; [ "$sha" = - ] && sha=""; [ "$lst" = - ] && lst=""
    [ "$file" = - ] && file=""; [ "${info:-}" = - ] && info=""
    case "${status:-ok}" in supprime|deleted) status=deleted ;; *) status=ok ;; esac
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$file"
    ST_INFO[$id]="${info:-}"; ST_STATUS[$id]="$status"
done < "$STATE"
FIRST_RUN=0; [ "${#ST_VER[@]}" -eq 0 ] && FIRST_RUN=1

save_state() {
    local id
    for id in "${!ST_VER[@]}"; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "${ST_VER[$id]:--}" "${ST_SHA[$id]:--}" "${ST_LIST[$id]:--}" \
            "${ST_FILE[$id]:--}" "${ST_INFO[$id]:--}" "${ST_STATUS[$id]:-ok}"
    done | sort -n > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

# ---------------------------------------------------------------- 0. upgrade of old .gmd files
# The first versions of the script did not write the k50 key (format version):
# without it, the game imports an empty level. Add it without re-downloading.
fixed=0
[ "$RUN_MODE" = records ] || while IFS= read -r -d '' f; do
    if ! grep -q '<k>k50</k>' "$f"; then
        sed -i "s#<k>k21</k><i>2</i>#<k>k21</k><i>2</i><k>k50</k><i>$GMD_BINARY_VERSION</i>#" "$f"
        nc_put "${f#"$FILES/"}"; fixed=$((fixed + 1))
    fi
done < <(find "$FILES" -name '*.gmd' -print0)
[ "$fixed" -gt 0 ] && log "Upgraded the format of $fixed .gmd files (added k50)"

# ---------------------------------------------------------------- 1. lists
declare -A WANT_LIST=() WANT_POS=() WANT_NAME=() WANT_AUTHOR=()
ORDER=()
for lst in $LISTS; do
    log "Reading list $lst"
    progress "Reading list $lst"
    if ! curl -sS -m 60 -f "$LIST_BASE_URL/$lst" -o "$TMP/$lst.html"; then
        die "cannot read $LIST_BASE_URL/$lst (nothing was changed)"
    fi
    sed -n 's/.*id="__NUXT_DATA__"[^>]*>\(.*\)/\1/p' "$TMP/$lst.html" | sed 's#</script>.*##' \
        | awk -v RECORDS="$TMP/$lst.rec" -f "$AWK_PARSER" > "$TMP/$lst.tsv"
    nb="$(wc -l < "$TMP/$lst.tsv")"
    [ "$nb" -gt 0 ] || die "no level found in list $lst (the site may have changed)"
    log "  $nb levels"

    old_index="$FILES/$lst/index.tsv"
    if [ -f "$old_index" ]; then  # ranking changes
        awk -F'\t' -v L="$lst" '
            FILENAME == ARGV[1] { if (FNR > 1) { op[$2] = $1; on[$2] = $3 } next }
            FNR > 1 { seen[$2] = 1
                if (!($2 in op)) printf "[%s] ENTERED the list: #%s %s (%s)\n", L, $1, $3, $2
                else if (op[$2] != $1) printf "[%s] RANKING: %s (%s) #%s -> #%s\n", L, $3, $2, op[$2], $1 }
            END { for (i in op) if (!(i in seen)) printf "[%s] LEFT the list: %s (%s), was #%s\n", L, on[i], i, op[i] }
        ' "$old_index" <(printf 'position\tid\tname\tauthor\n'; cat "$TMP/$lst.tsv") > "$TMP/$lst.moves"
        while IFS= read -r line; do note "$line"; done < "$TMP/$lst.moves"
        while IFS= read -r line; do
            case "$line" in
                *"ENTERED the list"*) notify "${line}" ;;
                *"LEFT the list"*)    notify "${line}" ;;
            esac
        done < "$TMP/$lst.moves"
        moved="$(grep -c 'RANKING' "$TMP/$lst.moves" || true)"
        [ "${moved:-0}" -gt 0 ] && notify "[$lst] $moved level(s) moved in the ranking"
    fi

    while IFS=$'\t' read -r pos id name author _video _showcase; do
        [ -n "${WANT_LIST[$id]:-}" ] && continue   # already in a previous list
        WANT_LIST[$id]="$lst"; WANT_POS[$id]="$pos"; WANT_NAME[$id]="$name"; WANT_AUTHOR[$id]="$author"
        ORDER+=("$id")
    done < "$TMP/$lst.tsv"
done

# ---------------------------------------------------------------- 2. downloads
make_gmd() {  # make_gmd <response file (level part)> <name> <author> <output>
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

# Info compared in addition to the level data: GD field -> label
INFO_FIELDS="2 3 12 35 52 53 15 37 18 19 42 9 17 43 25"
info_label() {
    case "$1" in
        2) echo "name" ;; 3) echo "description" ;; 12) echo "official song" ;; 35) echo "song" ;;
        52) echo "songs" ;; 53) echo "sound effects" ;; 15) echo "length" ;; 37) echo "coins" ;;
        18) echo "stars" ;; 19) echo "featured" ;; 42) echo "epic/legendary/mythic" ;;
        9|17|43|25) echo "difficulty" ;; *) echo "field $1" ;;
    esac
}
info_diff() {  # info_diff <old> <new> -> labels of the changed fields, separated by ", "
    awk -v A="$1" -v B="$2" 'BEGIN {
        na = split(A, a, "\037"); nb = split(B, b, "\037")
        for (i = 1; i <= na; i++) { p = index(a[i], "="); if (p) { k = substr(a[i], 1, p - 1); oa[k] = substr(a[i], p + 1); keys[k] = 1 } }
        for (i = 1; i <= nb; i++) { p = index(b[i], "="); if (p) { k = substr(b[i], 1, p - 1); ob[k] = substr(b[i], p + 1); keys[k] = 1 } }
        for (k in keys) if (oa[k] != ob[k]) print k }' | while read -r k; do info_label "$k"; done | sort -u | paste -sd, - | sed 's/,/, /g'
}
# link to the level's page on the list site: <LIST_BASE_URL>/<list>?level=<id>
gdlink() { printf '[%s](<%s/%s?level=%s>)' "$1" "${LIST_BASE_URL%/}" "$lst" "$2"; }

[ "$RUN_MODE" = records ] && ORDER=()   # videos only: nothing to download
new=0; updated=0; infos=0; same=0; failed=0; deleted=0; n_done=0; total=${#ORDER[@]}
log "Full check of $total levels (data downloaded and compared)"
for id in "${ORDER[@]}"; do
    lst="${WANT_LIST[$id]}"; name="${WANT_NAME[$id]}"; author="${WANT_AUTHOR[$id]}"
    rel="$lst/$(safe_name "$name") ($id).gmd"
    old="${ST_FILE[$id]:-}"

    n_done=$((n_done + 1))
    log "Downloading [$n_done/$total] $name ($id)"
    progress "Checking levels $n_done/$total"
    rc=0; gd_post downloadGJLevel22.php "levelID=$id" "$TMP/resp" || rc=$?
    [ "$rc" -eq 2 ] && { GD_BLOCKED=1; failed=$((failed + 1)); break; }
    [ "$rc" -eq 0 ] || { failed=$((failed + 1)); continue; }

    if [ "$(head -c 2 "$TMP/resp")" != "1:" ]; then
        # level missing from the GD servers: deleted or made private
        if [ "${ST_STATUS[$id]:-ok}" != deleted ]; then
            note "[$lst] DELETED from the GD servers (or made private): $name ($id) — the last copy is kept"
            notify "[$lst] **Deleted** from the GD servers (or made private): $(gdlink "$name" "$id") by $author"
            ST_STATUS[$id]=deleted; ST_LIST[$id]="$lst"; ST_VER[$id]="${ST_VER[$id]:-}"
            ST_SHA[$id]="${ST_SHA[$id]:-}"; ST_FILE[$id]="$old"; ST_INFO[$id]="${ST_INFO[$id]:-}"
            save_state
        fi
        deleted=$((deleted + 1)); continue
    fi

    cut -d'#' -f1 "$TMP/resp" | tr -d '\r\n' > "$TMP/raw"   # level part (before the first #)
    if ! make_gmd "$TMP/raw" "$name" "$author" "$TMP/level.gmd"; then
        log "  invalid response, skipped"; failed=$((failed + 1)); continue
    fi
    ver="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "5") print $(j + 1) }' "$TMP/raw")"
    sha="$(awk -F: '{ for (j = 1; j < NF; j += 2) if ($j == "4") printf "%s", $(j + 1) }' "$TMP/raw" \
        | sha256sum | cut -d' ' -f1)"
    info="$(awk -F: -v F="$INFO_FIELDS" 'BEGIN { n = split(F, w, " ") }
        { for (j = 1; j < NF; j += 2) f[$j] = $(j + 1) }
        END { for (i = 1; i <= n; i++) printf "%s%s=%s", (i > 1 ? "\037" : ""), w[i], f[w[i]] }' "$TMP/raw")"

    if [ "${ST_STATUS[$id]:-ok}" = deleted ]; then
        note "[$lst] REAPPEARED on the GD servers: $name ($id)"
        notify "[$lst] **Reappeared** on the GD servers: $(gdlink "$name" "$id")"
        ST_STATUS[$id]=ok
    fi

    changed=0
    if [ -z "${ST_SHA[$id]:-}" ] || [ -z "$old" ] || [ ! -f "$FILES/$old" ]; then
        if [ -z "${ST_SHA[$id]:-}" ]; then note "[$lst] NEW: $name ($id) v$ver"; new=$((new + 1)); changed=1; fi
    elif [ "$sha" != "${ST_SHA[$id]}" ]; then
        # level data changed: archive the previous version
        hist="history/$(safe_name "$name") ($id)/${DAY}_v${ST_VER[$id]}.gmd"
        mkdir -p "$FILES/$(dirname "$hist")" && cp "$FILES/$old" "$FILES/$hist" && nc_put "$hist"
        extra=""
        if [ -n "${ST_INFO[$id]:-}" ] && [ "$info" != "${ST_INFO[$id]}" ]; then extra="$(info_diff "${ST_INFO[$id]}" "$info")"; fi
        # object-level comparison of the two versions (level_diff.sh)
        diff_log=""; : > "$TMP/diff.msg"
        diffrel="history/$(safe_name "$name") ($id)/${DAY}_v${ST_VER[$id]}_to_v${ver}_diff.txt"
        if [ -x "$DIFF_TOOL" ] && OLD_LABEL="$name ($id) v${ST_VER[$id]}" NEW_LABEL="$name ($id) v$ver, downloaded $DAY" \
            "$DIFF_TOOL" "$FILES/$old" "$TMP/level.gmd" "$FILES/$diffrel" > "$TMP/diff.out" 2>/dev/null; then
            diff_log="$(awk -F'\t' '$1 == "LOG" { print $2; exit }' "$TMP/diff.out")"
            awk -F'\t' '$1 == "MSG" { print "- " $2 }' "$TMP/diff.out" > "$TMP/diff.msg"
            nc_put "$diffrel"
        fi
        note "[$lst] MODIFIED: $name ($id) v${ST_VER[$id]} -> v$ver${diff_log:+ — $diff_log}${extra:+ (info: $extra)} (previous version in $hist)"
        notify "[$lst] **Level modified**: $(gdlink "$name" "$id") by $author${extra:+ — info also changed: $extra}"
        while IFS= read -r line; do notify "$line"; done < "$TMP/diff.msg"
        printf '%s\t%s\t%s\n' "$lst" "$id" "$name" >> "$TMP/modified.tsv"   # for the bot's /status and /diff hint
        updated=$((updated + 1)); changed=1
    elif [ -n "${ST_INFO[$id]:-}" ] && [ "$info" != "${ST_INFO[$id]}" ]; then
        what="$(info_diff "${ST_INFO[$id]}" "$info")"
        note "[$lst] INFO CHANGED: $name ($id): $what"
        notify "[$lst] Info changed for $(gdlink "$name" "$id"): $what"
        infos=$((infos + 1)); changed=1
    else
        same=$((same + 1))
    fi
    if [ "$changed" = 0 ] && [ "$old" = "$rel" ] && [ "$ver" = "${ST_VER[$id]}" ] && [ -n "${ST_INFO[$id]:-}" ]; then
        ST_LIST[$id]="$lst"; continue   # nothing changed
    fi

    mkdir -p "$FILES/$lst"
    mv "$TMP/level.gmd" "$FILES/$rel"
    if [ -n "$old" ] && [ "$old" != "$rel" ] && [ -f "$FILES/$old" ]; then
        unlink "$FILES/$old"; nc_delete "$old"
        [ "$changed" = 0 ] && note "[$lst] RENAMED: $old -> $rel"
    fi
    nc_put "$rel"
    [ "$DAILY" = 1 ] && [ "$changed" = 1 ] && nc_copy "$rel" "$DAY/$rel"   # copy into the folder of the day
    ST_VER[$id]="$ver"; ST_SHA[$id]="$sha"; ST_LIST[$id]="$lst"; ST_FILE[$id]="$rel"; ST_INFO[$id]="$info"
    save_state   # saved as we go: an interruption loses nothing
done
save_state

# ---------------------------------------------------------------- 2b. videos of the levels
video_label() {  # video_label <kind> <player>
    case "$1" in
        verification) printf 'verification%s' "${2:+ by $2}" ;;
        showcase)     printf 'showcase' ;;
        *)            printf 'record%s' "${2:+ of $2}" ;;
    esac
}
status_label() {
    case "$1" in
        private) echo "private" ;; deleted) echo "deleted / unavailable" ;;
        noaccess) echo "no access" ;; invalid) echo "invalid link" ;; *) echo "$1" ;;
    esac
}
if [ "$RECORD_CHECK" = 1 ] && [ -f "$RECORD_TOOL" ]; then
    log "Checking the videos of the levels"
    progress "Checking the record videos"
    # list <TAB> id <TAB> name <TAB> kind <TAB> player <TAB> link   (a level in two lists: first list only)
    for lst in $LISTS; do
        [ -f "$TMP/$lst.rec" ] && awk -F'\t' -v OFS='\t' -v L="$lst" '{ print L, $0 }' "$TMP/$lst.rec"
    done | awk -F'\t' '!($2 in owner) { owner[$2] = $1 } owner[$2] == $1' > "$TMP/videos.tsv"
    RECORDS_UNAV="$DATA_DIR/records_unavailable.tsv"
    if bash "$RECORD_TOOL" "$TMP/videos.tsv" "$DATA_DIR/records_state.tsv" "$RECORDS_UNAV" > "$TMP/videos.changes"; then
        total_videos=$(cut -f6 "$TMP/videos.tsv" | sort -u | wc -l); unav=$(wc -l < "$RECORDS_UNAV")
        log "  $total_videos videos, $unav unavailable"
        if grep -qx FIRST "$TMP/videos.changes"; then
            note "VIDEOS: first check, $unav of $total_videos level videos are unavailable"
            [ "$unav" -gt 0 ] && notify "First check of the level videos: **$unav** verification / showcase / record videos are unavailable. Use \`/records\` to see them."
        fi
        for kind in DOWN UP; do
            n=0
            while IFS=$'\t' read -r what status vlst vid vname vkind vuser vlink; do
                [ "$what" = "$kind" ] || continue
                n=$((n + 1))
                if [ "$kind" = DOWN ]; then
                    note "[$vlst] VIDEO UNAVAILABLE ($(status_label "$status")): $vname ($vid) — $(video_label "$vkind" "$vuser") $vlink"
                    [ "$n" -le "$RECORD_ANNOUNCE_MAX" ] && notify "[$vlst] **Video unavailable** ($(status_label "$status")): [$vname](<${LIST_BASE_URL%/}/$vlst?level=$vid>) — $(video_label "$vkind" "$vuser") · [link](<$vlink>)"
                else
                    note "[$vlst] VIDEO AVAILABLE AGAIN: $vname ($vid) — $(video_label "$vkind" "$vuser") $vlink"
                    [ "$n" -le "$RECORD_ANNOUNCE_MAX" ] && notify "[$vlst] **Video available again**: [$vname](<${LIST_BASE_URL%/}/$vlst?level=$vid>) — $(video_label "$vkind" "$vuser") · [link](<$vlink>)"
                fi
            done < "$TMP/videos.changes"
            if [ "$n" -gt "$RECORD_ANNOUNCE_MAX" ]; then
                if [ "$kind" = DOWN ]; then notify "…and $((n - RECORD_ANNOUNCE_MAX)) more unavailable videos (\`/records\` lists them all)."
                else notify "…and $((n - RECORD_ANNOUNCE_MAX)) more videos available again."; fi
            fi
        done
        { printf 'list\tid\tlevel\tkind\tplayer\tlink\tstatus\tsince\n'; cat "$RECORDS_UNAV"; } > "$FILES/unavailable_videos.tsv"
        nc_put "unavailable_videos.tsv"
    else
        log "  video check failed (will be retried on the next run)"
    fi
fi

# ---------------------------------------------------------------- 3. index + log
for lst in $LISTS; do
    mkdir -p "$FILES/$lst"
    { printf 'position\tid\tname\tauthor\tfile\tvideo\tshowcase\n'
      while IFS=$'\t' read -r pos id name author video showcase; do
          printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$pos" "$id" "$name" "$author" "${ST_FILE[$id]:-(not downloaded)}" \
              "${video:--}" "${showcase:--}"
      done < "$TMP/$lst.tsv"; } > "$FILES/$lst/index.tsv"
    nc_put "$lst/index.tsv"
done
cp "$LOG" "$FILES/changes.log" && nc_put "changes.log"
if [ "$DAILY" = 1 ]; then   # log of the day (cumulative if several runs the same day)
    mkdir -p "$FILES/$DAY"
    { printf '=== Run of %s ===\n' "$(date '+%F %T')"
      if [ -s "$TMP/run.notes" ]; then cat "$TMP/run.notes"; else echo "No changes."; fi
      echo; } >> "$FILES/$DAY/changes.log"
    nc_put "$DAY/changes.log"
fi
[ "$RESYNC" = 1 ] && nc_resync
nc_flush || failed=$((failed + 1))

summary="$new new, $updated modified, $infos info changed, $deleted deleted, $same unchanged, $failed failed"
[ "$FIRST_RUN" = 1 ] && [ "$new" -gt 0 ] && notify "First run: $new levels archived to Nextcloud."
[ "${GD_BLOCKED:-0}" = 1 ] && notify "The GD servers rate-limited the requests: run interrupted, the rest will be done on the next run."
# how to see the details of a modified level (bot only)
if [ -n "$DISCORD_BOT_TOKEN" ] && [ -s "$TMP/modified.tsv" ]; then
    notify ""
    notify "Use \`/diff $(head -1 "$TMP/modified.tsv" | cut -f3)\` to see what changed in a level."
fi
if [ "$RUN_MODE" = records ]; then
    # videos only: /status keeps showing the numbers of the last level run
    summary="record videos only: ${total_videos:-0} videos checked, ${unav:-0} unavailable"
    discord_send "$summary"
else
    discord_send "$summary"
    printf '%s — %s\n' "$(date '+%Y-%m-%d %H:%M')" "$summary" > "$DATA_DIR/last_run.txt"
    # levels modified during this run: list <TAB> id <TAB> name (shown by the bot's /status)
    if [ -s "$TMP/modified.tsv" ]; then cp "$TMP/modified.tsv" "$DATA_DIR/last_run_modified.tsv"; else : > "$DATA_DIR/last_run_modified.tsv"; fi
fi
log "Done: $summary"
[ "$failed" -eq 0 ]
