#!/usr/bin/env bash
# level_diff.sh — compares the objects of two versions of a Geometry Dash level.
#
#   level_diff.sh <old.gmd> <new.gmd> [detail.txt]
#
# Prints a summary on stdout:
#   LOG<TAB><one-line summary>        (for the change log)
#   MSG<TAB><line>                    (one or more lines, for Discord)
# and, if a third argument is given, writes the full list of added / removed /
# moved objects to that file.
# Exit code: 0 = compared, 1 = level data could not be decoded.
#
# OLD_LABEL / NEW_LABEL (environment) replace the file names in the detail file.
# Dependencies: awk, sort, comm, base64, gzip.

set -u -o pipefail
export LC_ALL=C

OLD="$1"; NEW="$2"; DETAIL="${3:-}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# --- decoding ---------------------------------------------------------------
# k4 holds the level string: URL-safe base64 of a gzip stream. Once decoded:
#   header;object;object;...     each part is key,value,key,value,...
extract_k4() {  # extract_k4 <gmd> <out>
    awk 'BEGIN { RS = "<k>k4</k><s>" } NR == 2 { sub(/<.*/, ""); printf "%s", $0 }' "$1" > "$2"
    [ -s "$2" ]
}

decode() {  # decode <gmd> <prefix>  ->  <prefix>.obj (normalized objects), <prefix>.hdr (header key<TAB>value)
    local len pad
    extract_k4 "$1" "$2.k4" || return 1
    tr '_-' '/+' < "$2.k4" > "$2.b64"
    len=$(wc -c < "$2.b64"); pad=$(( (4 - len % 4) % 4 ))
    [ "$pad" -gt 0 ] && printf '%*s' "$pad" '' | tr ' ' '=' >> "$2.b64"
    base64 -d "$2.b64" 2>/dev/null | gzip -dc 2>/dev/null > "$2.txt"
    [ -s "$2.txt" ] || return 1
    # objects are normalized (keys sorted numerically) so that the same object always
    # gives the same line, whatever order the editor saved its properties in
    awk -v HDR="$2.hdr" 'BEGIN { RS = ";" }
        NR == 1 { n = split($0, a, ","); for (i = 1; i < n; i += 2) print a[i] "\t" a[i + 1] > HDR; next }
        {
            gsub(/[\r\n]/, "")
            if ($0 == "") next
            n = split($0, a, ","); m = 0
            delete v
            for (i = 1; i < n; i += 2) { if (!(a[i] in v)) k[++m] = a[i]; v[a[i]] = a[i + 1] }
            for (i = 2; i <= m; i++) {            # insertion sort, numeric keys
                x = k[i]; j = i - 1
                while (j > 0 && k[j] + 0 > x + 0) { k[j + 1] = k[j]; j-- }
                k[j + 1] = x
            }
            line = ""
            for (i = 1; i <= m; i++) line = line (i > 1 ? "," : "") k[i] "," v[k[i]]
            print line
        }' "$2.txt" > "$2.obj"
    [ -f "$2.hdr" ] || : > "$2.hdr"
}

decode "$OLD" "$T/old" || exit 1
decode "$NEW" "$T/new" || exit 1

# --- object diff ------------------------------------------------------------
sort "$T/old.obj" > "$T/old.s"
sort "$T/new.obj" > "$T/new.s"
comm -23 "$T/old.s" "$T/new.s" > "$T/removed"   # in the old version only
comm -13 "$T/old.s" "$T/new.s" > "$T/added"     # in the new version only
n_old=$(wc -l < "$T/old.s"); n_new=$(wc -l < "$T/new.s")

# A removed object and an added object that are identical except for their position
# (keys 2 = x, 3 = y) count as one moved object — only if the new position is close
# (MOVE_MAX units, 30 units = 1 block); the nearest candidate is chosen. Removed objects
# are bucketed by (properties, x / MOVE_MAX) so only neighbouring buckets are searched.
# Output: TYPE<TAB>object id<TAB>x<TAB>y<TAB>old x<TAB>old y<TAB>object
awk -F'\t' -v MAX="${MOVE_MAX:-600}" '
    function parse(line,   n, a, i) { delete o; n = split(line, a, ","); for (i = 1; i < n; i += 2) o[a[i]] = a[i + 1] }
    function rest(line,   n, a, i, s) {
        n = split(line, a, ","); s = ""
        for (i = 1; i < n; i += 2) if (a[i] != "2" && a[i] != "3") s = s a[i] "," a[i + 1] ","
        return s
    }
    function bucket(x) { return int((x + 0) / MAX) }
    FILENAME == ARGV[1] {
        r = rest($0); parse($0); nrem++
        rl[nrem] = $0; rx[nrem] = o["2"] + 0; ry[nrem] = o["3"] + 0; rid[nrem] = o["1"]
        b = r SUBSEP bucket(o["2"]); bn[b]++; bk[b, bn[b]] = nrem
        next
    }
    {
        r = rest($0); parse($0); x = o["2"] + 0; y = o["3"] + 0; id = o["1"]
        best = 0; bestd = MAX * MAX + 1
        for (d = -1; d <= 1; d++) {
            b = r SUBSEP (bucket(x) + d)
            for (i = 1; i <= bn[b]; i++) {
                j = bk[b, i]; if (gone[j]) continue
                dd = (rx[j] - x) * (rx[j] - x) + (ry[j] - y) * (ry[j] - y)
                if (dd < bestd) { bestd = dd; best = j }
            }
        }
        if (best) { gone[best] = 1; printf "M\t%s\t%s\t%s\t%s\t%s\t%s\n", id, o["2"], o["3"], rx[best], ry[best], $0 }
        else printf "A\t%s\t%s\t%s\t\t\t%s\n", id, o["2"], o["3"], $0
    }
    END {
        for (j = 1; j <= nrem; j++) if (!gone[j]) printf "R\t%s\t%s\t%s\t\t\t%s\n", rid[j], rx[j], ry[j], rl[j]
    }' "$T/removed" "$T/added" > "$T/changes"

n_add=$(grep -c '^A' "$T/changes" || true)
n_rem=$(grep -c '^R' "$T/changes" || true)
n_mov=$(grep -c '^M' "$T/changes" || true)

# --- where in the level -----------------------------------------------------
max_x=$(awk -F, '{ for (i = 1; i < NF; i += 2) if ($i == "2" && $(i + 1) + 0 > m) m = $(i + 1) + 0 } END { print m + 0 }' "$T/new.obj")
where=""
if [ $((n_add + n_rem + n_mov)) -gt 0 ] && [ "${max_x%.*}" -gt 0 ]; then
    cut -f3 "$T/changes" | awk 'NF' | sort -n > "$T/xs"
    nx=$(wc -l < "$T/xs")
    lo=$(sed -n "$(( nx / 10 + 1 ))p" "$T/xs"); hi=$(sed -n "$(( (nx * 9 + 9) / 10 ))p" "$T/xs")
    p_lo=$(awk -v x="$lo" -v m="$max_x" 'BEGIN { p = int(x * 100 / m); if (p < 0) p = 0; if (p > 100) p = 100; print p }')
    p_hi=$(awk -v x="$hi" -v m="$max_x" 'BEGIN { p = int(x * 100 / m + 0.999); if (p < 0) p = 0; if (p > 100) p = 100; print p }')
    if [ "$p_lo" = "$p_hi" ]; then where="around ${p_lo}% of the level"
    elif [ "$nx" -lt 10 ]; then where="between ${p_lo}% and ${p_hi}% of the level"
    else where="mostly between ${p_lo}% and ${p_hi}% of the level"; fi
fi

# --- object names -----------------------------------------------------------
# Common object IDs; anything else is shown as "object #ID".
type_summary() {  # type_summary <A|R|M>  ->  "80 blocks, 30 spikes, 14 move triggers"
    awk -F'\t' -v T="$1" '
    BEGIN {
        split("1 2 3 4 6 7 40 62 63 64 65 66 68 69 70 71 72 74 75 76 77 78 81 82 83 90 91 92 93 94 95 96 116 117 118 119 120 121 122 146 147 160 161 162 163 164 165 166 167 168 169 170 171 172 173 174 175 176 192 194 195 196 197 204 206 207 208 209 210 212 213 215 219 220 247 248 249 250 251 252 253 254 255 256 257 258 259 260 261 263 264 265 267 268 269 270 271 272 274 275", b, " ")
        for (i in b) name[b[i]] = "block"
        split("8 39 103 392 9 61 243 244 363 364 365 366 367 368 446 447 667 989 991 720 135 177 178 179 216 217 218 458 459", s, " ")
        for (i in s) name[s[i]] = "spike/hazard"
        name[899] = "color trigger"; name[901] = "move trigger"; name[1006] = "pulse trigger"
        name[1007] = "alpha trigger"; name[1049] = "toggle trigger"; name[1268] = "spawn trigger"
        name[1346] = "rotate trigger"; name[1347] = "follow trigger"; name[1520] = "shake trigger"
        name[1585] = "animate trigger"; name[1595] = "touch trigger"; name[1611] = "count trigger"
        name[1616] = "stop trigger"; name[1811] = "instant count trigger"; name[1812] = "on death trigger"
        name[1815] = "collision trigger"; name[1817] = "pickup trigger"; name[1912] = "random trigger"
        name[1913] = "camera zoom trigger"; name[1914] = "static camera trigger"
        name[10] = "gravity portal"; name[11] = "gravity portal"; name[12] = "cube portal"
        name[13] = "ship portal"; name[45] = "mirror portal"; name[46] = "mirror portal"
        name[47] = "ball portal"; name[99] = "size portal"; name[101] = "size portal"
        name[111] = "UFO portal"; name[286] = "dual portal"; name[287] = "dual portal"
        name[660] = "wave portal"; name[745] = "robot portal"; name[747] = "teleport portal"
        name[1331] = "spider portal"; name[1933] = "swing portal"
        name[200] = "speed portal"; name[201] = "speed portal"; name[202] = "speed portal"
        name[203] = "speed portal"; name[1334] = "speed portal"
        name[36] = "orb"; name[84] = "orb"; name[141] = "orb"; name[1022] = "orb"; name[1330] = "orb"
        name[1333] = "orb"; name[1594] = "orb"; name[1704] = "dash orb"; name[1751] = "dash orb"
        name[35] = "pad"; name[67] = "pad"; name[140] = "pad"; name[1332] = "pad"
        name[142] = "secret coin"; name[1329] = "user coin"; name[31] = "start position"
        name[914] = "text"
    }
    $1 == T { n = ($2 in name) ? name[$2] : "object #" $2; c[n]++ }
    END { for (n in c) printf "%d\t%s\n", c[n], n }' "$T/changes" | sort -t"$(printf '\t')" -k1,1nr -k2 | head -4 |
    awk -F'\t' '{ n = $2; if ($1 > 1 && n !~ /^object #/) n = n "s"; printf "%s%d %s", (NR > 1 ? ", " : ""), $1, n }'
}

# --- level settings (header) ------------------------------------------------
settings=$(awk -F'\t' '
    function label(k) {
        if (k == "kS38" || k ~ /^kS[0-9]+$/) return "colors"
        if (k == "kA2") return "start game mode"; if (k == "kA3") return "start mini mode"
        if (k == "kA4") return "start speed"; if (k == "kA6") return "background"
        if (k == "kA7") return "ground"; if (k == "kA8") return "start dual mode"
        if (k == "kA10") return "2-player mode"; if (k == "kA11") return "start flipped gravity"
        if (k == "kA13") return "song offset"; if (k == "kA15" || k == "kA16") return "song fade"
        if (k == "kA17") return "ground line"; if (k == "kA18") return "font"
        return "setting " k
    }
    FILENAME == ARGV[1] { o[$1] = $2; keys[$1] = 1; next }
    { n[$1] = $2; keys[$1] = 1 }
    END { for (k in keys) if (o[k] != n[k]) print label(k) }' "$T/old.hdr" "$T/new.hdr" | sort -u | paste -sd, - | sed 's/,/, /g')

# --- output -----------------------------------------------------------------
fmt() { printf "%'d" "$1" 2>/dev/null || printf '%d' "$1"; }
objs="+$n_add / -$n_rem objects"
[ "$n_mov" -gt 0 ] && objs="$objs, $n_mov moved"
objs="$objs ($(fmt "$n_old") -> $(fmt "$n_new"))"

log_line="$objs"
[ -n "$where" ] && log_line="$log_line, $where"
[ -n "$settings" ] && log_line="$log_line; settings: $settings"
printf 'LOG\t%s\n' "$log_line"

if [ $((n_add + n_rem + n_mov)) -gt 0 ]; then
    printf 'MSG\t**%s**%s\n' "$objs" "${where:+ — $where}"
    added_types=$(type_summary A); removed_types=$(type_summary R); moved_types=$(type_summary M)
    [ -n "$added_types" ] && printf 'MSG\tAdded: %s\n' "$added_types"
    [ -n "$removed_types" ] && printf 'MSG\tRemoved: %s\n' "$removed_types"
    [ -n "$moved_types" ] && printf 'MSG\tMoved: %s\n' "$moved_types"
else
    printf 'MSG\tNo object added, removed or moved\n'
fi
[ -n "$settings" ] && printf 'MSG\tSettings changed: %s\n' "$settings"

if [ -n "$DETAIL" ]; then
    {
        echo "Level diff"
        echo "  old: ${OLD_LABEL:-$OLD}"
        echo "  new: ${NEW_LABEL:-$NEW}"
        echo
        echo "$log_line"
        echo
        echo "Format: object id, x, y (moved: old x, y -> new x, y), then all the object properties."
        echo
        echo "ADDED ($n_add)"
        awk -F'\t' '$1 == "A" { printf "  + id %s at x=%s y=%s   [%s]\n", $2, $3, $4, $7 }' "$T/changes"
        echo; echo "REMOVED ($n_rem)"
        awk -F'\t' '$1 == "R" { printf "  - id %s at x=%s y=%s   [%s]\n", $2, $3, $4, $7 }' "$T/changes"
        echo; echo "MOVED ($n_mov)"
        awk -F'\t' '$1 == "M" { printf "  ~ id %s from x=%s y=%s to x=%s y=%s   [%s]\n", $2, $5, $6, $3, $4, $7 }' "$T/changes"
        if [ -n "$settings" ]; then
            echo; echo "SETTINGS"
            awk -F'\t' 'FILENAME == ARGV[1] { o[$1] = $2; k[$1] = 1; next } { n[$1] = $2; k[$1] = 1 }
                END { for (x in k) if (o[x] != n[x]) printf "  %s: %s -> %s\n", x, o[x], n[x] }' "$T/old.hdr" "$T/new.hdr"
        fi
    } > "$DETAIL"
fi
exit 0
