# Extrait les niveaux d'une page tsl.pages.dev (payload Nuxt "__NUXT_DATA__").
# Entrée : le contenu JSON du payload (un tableau JSON où les objets référencent
# leurs valeurs par index). Sortie TSV : position <TAB> id <TAB> nom <TAB> auteur
# Compatible mawk / gawk / busybox awk.

function hexval(h,   i, c, v) {
    v = 0; h = tolower(h)
    for (i = 1; i <= length(h); i++) { c = index("0123456789abcdef", substr(h, i, 1)) - 1; v = v * 16 + c }
    return v
}
function utf8(cp) {
    if (cp < 128) return sprintf("%c", cp)
    if (cp < 2048) return sprintf("%c%c", 192 + int(cp / 64), 128 + cp % 64)
    if (cp < 65536) return sprintf("%c%c%c", 224 + int(cp / 4096), 128 + int(cp / 64) % 64, 128 + cp % 64)
    return sprintf("%c%c%c%c", 240 + int(cp / 262144), 128 + int(cp / 4096) % 64, 128 + int(cp / 64) % 64, 128 + cp % 64)
}
# Décode une chaîne JSON (avec ses guillemets) ; renvoie "" si ce n'est pas une chaîne.
function jstr(s,   out, i, c, n, cp, lo) {
    if (substr(s, 1, 1) != "\"") return s
    s = substr(s, 2, length(s) - 2); out = ""; n = length(s)
    for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (c != "\\") { out = out c; continue }
        c = substr(s, ++i, 1)
        if (c == "n" || c == "t" || c == "r") out = out " "
        else if (c == "b" || c == "f") ;
        else if (c == "u") {
            cp = hexval(substr(s, i + 1, 4)); i += 4
            if (cp >= 55296 && cp < 56320 && substr(s, i + 1, 2) == "\\u") {
                lo = hexval(substr(s, i + 3, 4)); i += 6
                cp = 65536 + (cp - 55296) * 1024 + (lo - 56320)
            }
            out = out utf8(cp)
        } else out = out c
    }
    gsub(/\t/, " ", out)
    return out
}
{ buf = buf $0 }
END {
    n = length(buf); depth = 0; inq = 0; esc = 0; k = 0; start = 0
    for (i = 1; i <= n; i++) {
        c = substr(buf, i, 1)
        if (inq) {
            if (esc) esc = 0
            else if (c == "\\") esc = 1
            else if (c == "\"") inq = 0
            continue
        }
        if (c == "\"") { inq = 1; continue }
        if (c == "[" || c == "{") {
            depth++
            if (depth == 1) start = i + 1
        } else if (c == "]" || c == "}") {
            if (depth == 1) { el[k++] = substr(buf, start, i - start) }
            depth--
        } else if (c == "," && depth == 1) {
            el[k++] = substr(buf, start, i - start); start = i + 1
        }
    }
    count = 0
    for (j = 0; j < k; j++) {
        e = el[j]
        if (substr(e, 1, 1) != "{" || index(e, "\"verifier\":") == 0 || index(e, "\"id\":") == 0) continue
        if (index(substr(e, 2), "{") > 0) continue
        inner = substr(e, 2, length(e) - 2)
        m = split(inner, kv, ",")
        delete f
        for (x = 1; x <= m; x++) {
            p = index(kv[x], ":"); if (p == 0) continue
            key = substr(kv[x], 2, p - 3); f[key] = substr(kv[x], p + 1)
        }
        id = jstr(el[f["id"]])
        if (id !~ /^[0-9]+$/) continue
        if (id in seen) continue
        seen[id] = 1
        count++
        pos = ("position" in f) ? el[f["position"]] : count
        if (pos !~ /^[0-9]+$/) pos = count
        printf "%s\t%s\t%s\t%s\n", pos, id, jstr(el[f["name"]]), jstr(el[f["author"]])
    }
}
