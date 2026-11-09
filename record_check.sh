#!/usr/bin/env bash
# record_check.sh — checks that the videos of the levels (verification, showcase and
# every record) can still be watched.
#
#   record_check.sh <videos.tsv> <state.tsv> <unavailable.tsv>
#
# videos.tsv : list <TAB> level id <TAB> level name <TAB> kind <TAB> player <TAB> link
# state.tsv  : link <TAB> status <TAB> last check (epoch) <TAB> unavailable since (YYYY-MM-DD or -)
#              <TAB> video title (YouTube only, "" if unknown)
#              (read and rewritten)
# unavailable.tsv : rewritten with the videos.tsv lines whose video is unavailable,
#              followed by <TAB> status <TAB> since
# stdout     : one line per change since the previous check:
#              DOWN|UP <TAB> status <TAB> <the videos.tsv line>
#              and "FIRST" (alone) when there was no previous state (no per-video change then).
#
# Status: ok, private, deleted, noaccess (link needs an account / permission), invalid (not a link).
# YouTube: oEmbed answers 200 (public/unlisted), 401 (embedding disabled = still watchable),
# 403 (private: the thumbnail is gone too) or 404 (deleted / unavailable).
# Other sites: 404/410 = deleted, redirect to a login page = noaccess.
# A video that could not be checked (timeout, rate limit…) keeps its previous status.
#
# Environment: RECORD_CHECK_DAYS (default 3): a working video is checked again after that many
# days (unavailable ones at every run); RECORD_PARALLEL (default 4): checks at the same time.

set -u -o pipefail
export LC_ALL=C

YT_RE='(youtu\.be/|youtube\.com/.*[?&]v=|youtube\.com/(shorts|live|embed)/)([A-Za-z0-9_-]{11})'

check_one() {  # check_one <link>  ->  "<link> TAB <status> TAB <title>"   (status "unknown" = could not tell)
    local link="$1" id code eff body status=unknown title=""
    if [[ ! "$link" =~ ^https?://[^[:space:]]+$ ]]; then
        status=invalid
    elif [[ "$link" =~ $YT_RE ]]; then
        id="${BASH_REMATCH[3]}"
        body=$(curl -s -m 20 -w '\n%{http_code}' \
            "https://www.youtube.com/oembed?format=json&url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D$id")
        code="${body##*$'\n'}"
        # "title":"…" from the JSON answer (only the escapes that matter for reading a level name)
        title=$(printf '%s' "${body%$'\n'*}" | grep -o '"title":"\([^"\\]\|\\.\)*"' | head -1 | cut -c10- | sed 's/"$//' |
            sed 's/\\u0026/\&/g; s/\\u0027/'"'"'/g; s/\\u0022/"/g; s/\\u003c/</g; s/\\u003e/>/g; s/\\"/"/g; s/\\\//\//g; s/\\\\/\\/g' |
            tr '\t\r' '  ')
        case "$code" in
            200|401) status=ok ;;
            400|404) status=deleted ;;
            403)
                code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://i.ytimg.com/vi/$id/hqdefault.jpg")
                case "$code" in 404) status=private ;; 200) status=ok ;; esac ;;
        esac
    else
        read -r code eff < <(curl -s -L -m 25 -r 0-1023 --max-filesize 50000000 -o /dev/null \
            -A 'Mozilla/5.0 (X11; Linux x86_64) tsl-download-manager' \
            -w '%{http_code} %{url_effective}' "$link" 2>/dev/null)
        case "${code:-000}" in
            404|410) status=deleted ;;
            2??|3??)
                case "$eff" in
                    *accounts.google.com/*|*/login*|*/signin*|*/ServiceLogin*) status=noaccess ;;
                    *) status=ok ;;
                esac ;;
            401|403) case "$link" in *drive.google.com*|*dropbox.com*) status=noaccess ;; *) status=ok ;; esac ;;
        esac
    fi
    printf '%s\t%s\t%s\n' "$link" "$status" "$title"
}

if [ "${1:-}" = "--one" ]; then check_one "$2"; exit 0; fi

VIDEOS="$1"; STATE="$2"; UNAVAILABLE="$3"
DAYS="${RECORD_CHECK_DAYS:-3}"
PARALLEL="${RECORD_PARALLEL:-4}"
NOW=$(date +%s); TODAY=$(date +%F)
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
FIRST=0; [ -s "$STATE" ] || FIRST=1
[ -f "$STATE" ] || : > "$STATE"

# links to check now: never checked, unavailable, unknown, checked more than DAYS days ago,
# or a working YouTube video whose title is not known yet
cut -f6 "$VIDEOS" | sort -u > "$T/links"
awk -F'\t' -v now="$NOW" -v max=$((DAYS * 86400)) '
    FILENAME == ARGV[1] { st[$1] = $2; at[$1] = $3; ti[$1] = $5; next }
    !($1 in st) || st[$1] != "ok" || now - at[$1] >= max || ($1 ~ /youtu/ && ti[$1] == "")' "$STATE" "$T/links" > "$T/todo"

tr '\n' '\0' < "$T/todo" | xargs -0 -r -n 1 -P "$PARALLEL" bash "$0" --one > "$T/results"

# new state: results override the previous state, except "unknown"
awk -F'\t' -v OFS='\t' -v now="$NOW" -v today="$TODAY" '
    FILENAME == ARGV[1] { st[$1] = $2; at[$1] = $3; since[$1] = $4; ti[$1] = $5; next }
    FILENAME == ARGV[2] { keep[$1] = 1; next }
    $2 != "unknown" {
        if ($2 == "ok") since[$1] = "-"
        else if (st[$1] != $2 || since[$1] == "" || since[$1] == "-") since[$1] = today
        st[$1] = $2; at[$1] = now
        if ($3 != "") ti[$1] = $3
    }
    END { for (l in st) if (l in keep) print l, st[l], at[l], (since[l] == "" ? "-" : since[l]), ti[l] }
' "$STATE" "$T/links" "$T/results" | sort > "$T/state.new"

# changes, and the list of unavailable videos
awk -F'\t' -v OFS='\t' -v first="$FIRST" -v un="$UNAVAILABLE.tmp" '
    FILENAME == ARGV[1] { old[$1] = $2; next }
    FILENAME == ARGV[2] { new[$1] = $2; since[$1] = $4; next }
    {
        n = ($6 in new) ? new[$6] : "ok"; o = ($6 in old) ? old[$6] : "ok"
        if (n != "ok") print $0, n, since[$6] > un
        if (first) next
        if (n != "ok" && n != o) print "DOWN", n, $0
        else if (n == "ok" && o != "ok") print "UP", o, $0
    }
    END { if (first) print "FIRST"; close(un) }
' "$STATE" "$T/state.new" "$VIDEOS"
[ -f "$UNAVAILABLE.tmp" ] || : > "$UNAVAILABLE.tmp"
mv "$UNAVAILABLE.tmp" "$UNAVAILABLE"
mv "$T/state.new" "$STATE"
