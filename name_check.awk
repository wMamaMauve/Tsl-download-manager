# Finds record / verification videos whose title names ANOTHER level of the list than the one
# the record is on (e.g. "Shitty Acheron 100%" given as a record of Shitty NETWORK).
#
#   awk -f name_check.awk <levels> <validated> <state> <videos.tsv> <videos.tsv>   (videos.tsv twice)
#   levels    : the list files (position <TAB> id <TAB> name ...), all lists concatenated
#   validated : level id <TAB> video key ... (entries checked by hand on Discord: never reported)
#   state     : records_state.tsv (link <TAB> status <TAB> time <TAB> since <TAB> title)
#   videos.tsv: list <TAB> level id <TAB> level name <TAB> kind <TAB> player <TAB> link
# Output: list <TAB> level id <TAB> level name <TAB> kind <TAB> player <TAB> link <TAB> title
#         <TAB> levels named in the title (", ")
# A title that names its own level (even with others, e.g. a compilation) or no level at all is fine,
# and so is a compilation: one video given by the same player for several levels.
# "2" and "II" are the same; a level named inside the record's own level name ("Allegiance" for
# "Unnerfed Allegiance") does not count.
# Spellings understood: "Shitty", "Sh*tty", "Sh!tty", "Sh#tty", "Sh**ty", "sh <name>".
# Works with mawk / gawk / busybox awk.

function norm(s) {
    s = tolower(s)
    gsub(/\\u[0-9a-f][0-9a-f][0-9a-f][0-9a-f]/, " ", s)
    gsub(/sh[^a-z0-9 ][^a-z0-9 ]?[^a-z0-9 ]?t?ty/, "shitty", s)
    gsub(/[^a-z0-9]+/, " ", s)
    s = " " s " "
    while (gsub(/ sh /, " shitty ", s)) ;
    gsub(/ ii /, " 2 ", s); gsub(/ iii /, " 3 ", s); gsub(/ iv /, " 4 ", s); gsub(/ vi /, " 6 ", s)
    return s
}
function core(name,   c) {   # "Shitty Acheron (Twoft)" -> "acheron"
    sub(/[ ]*\([^)]*\)[ ]*$/, "", name)
    c = norm(name)
    sub(/^ shitty /, " ", c)
    gsub(/^ +| +$/, "", c)
    return c
}
function video_key(link,   k) {   # same rule as video_duplicates.awk
    if (match(link, /(youtu\.be\/|[?&]v=|\/shorts\/|\/live\/|\/embed\/)[A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-]/)) {
        k = substr(link, RSTART, RLENGTH); sub(/.*[\/=]/, "", k)
        return "youtube:" k
    }
    k = tolower(link); sub(/[?#].*/, "", k); sub(/\/+$/, "", k); sub(/^https?:\/\/(www\.|m\.)?/, "", k)
    return k
}
BEGIN { FS = OFS = "\t" }
FILENAME == ARGV[1] {
    c = core($3)
    if (c != "" && !(c in shown)) { shown[c] = $3; sub(/[ ]*\([^)]*\)[ ]*$/, "", shown[c]); cores[++nc] = c }
    lcore[$2] = c
    next
}
FILENAME == ARGV[2] { ok[$1 SUBSEP $2] = 1; next }
FILENAME == ARGV[3] { if ($5 != "") title[$1] = $5; next }
FNR == 1 { pass++ }
pass == 1 {   # first reading of videos.tsv: levels where each player gives each video
    pk = video_key($6) SUBSEP tolower($5)
    if (!((pk, $2) in seenl)) { seenl[pk, $2] = 1; nlev[pk]++ }
    next
}
($4 == "record" || $4 == "verification") && ($6 in title) {
    if (nlev[video_key($6) SUBSEP tolower($5)] > 1) next   # a compilation
    if (($2 SUBSEP video_key($6)) in ok) next
    own = ($2 in lcore) ? lcore[$2] : core($3)
    t = norm(title[$6])
    if (own != "" && index(t, " " own " ")) next          # names its own level
    m = 0
    for (i = 1; i <= nc; i++) {
        c = cores[i]
        if (c != own && index(t, " shitty " c " ") && !index(" " own " ", " " c " ")) found[++m] = c
    }
    if (m == 0) next                                       # names no level of the list
    names = ""
    for (i = 1; i <= m; i++) {   # "Acheron" is not listed when "Acheron II" is also named
        longer = 0
        for (j = 1; j <= m; j++) if (j != i && length(found[j]) > length(found[i]) && index(" " found[j] " ", " " found[i] " ")) longer = 1
        if (!longer) names = names (names == "" ? "" : ", ") shown[found[i]]
    }
    print $1, $2, $3, $4, $5, $6, title[$6], names
}
