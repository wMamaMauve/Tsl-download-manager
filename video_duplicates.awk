# Finds videos used more than once on the list site.
# Input (videos.tsv): list <TAB> level id <TAB> level name <TAB> kind <TAB> player <TAB> link
# Output: one line per entry of each duplicated video:
#   video key <TAB> reason <TAB> list <TAB> level id <TAB> level name <TAB> kind <TAB> player <TAB> link
# Reasons:
#   players  = the same video is given for different players (one of them is wrong)
#   twice    = the same player is listed twice with the same video on the same level
# One player's compilation video used for several levels is normal and not reported.
# Works with mawk / gawk / busybox awk.

function video_key(link,   k) {   # the same YouTube video can be written in many ways
    if (match(link, /(youtu\.be\/|[?&]v=|\/shorts\/|\/live\/|\/embed\/)[A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-]/)) {
        k = substr(link, RSTART, RLENGTH); sub(/.*[\/=]/, "", k)
        return "youtube:" k
    }
    k = tolower(link); sub(/[?#].*/, "", k); sub(/\/+$/, "", k); sub(/^https?:\/\/(www\.|m\.)?/, "", k)
    return k
}
function player_key(p) { p = tolower(p); gsub(/[^a-z0-9]/, "", p); return p }
BEGIN { FS = OFS = "\t" }
$6 != "" && $4 != "showcase" {   # a showcase may reuse any video
    k = video_key($6)
    n[k]++; row[k, n[k]] = $0
    pk = player_key($5)
    if (!((k, pk) in seenp)) { seenp[k, pk] = 1; players[k]++ }
    lp = $2 SUBSEP pk
    if ((k, lp) in seenlp) twice[k] = 1
    seenlp[k, lp] = 1
}
END {
    for (k in n) {
        if (n[k] < 2) continue
        reason = (players[k] > 1) ? "players" : (k in twice ? "twice" : "")
        if (reason == "") continue
        for (i = 1; i <= n[k]; i++) print k, reason, row[k, i]
    }
}
