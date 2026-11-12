#!/usr/bin/env python3
"""analyze.py — automatic checks of a record video for the Record Checker.

For now: the cheat indicator.
  * in game: a small spot that stays greener (or redder) than its surroundings in most images,
    whatever the background (works with translucent dots, at any place on the screen);
  * on the end screen: the dot(s) in its top-left corner, read again at full size because the end
    screen can be visible for less than a second.

    analyze.py <video.mp4> [output folder]      -> writes analysis.json (+ close-up images)
Needs numpy and ffmpeg.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

VERSION = 1   # bump when the analysis changes: older results are computed again
INGAME_FPS = 2


# --------------------------------------------------------------------------- video reading
def video_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    d = json.loads(out or "{}")
    st = (d.get("streams") or [{}])[0]
    return int(st.get("width") or 1280), int(st.get("height") or 720), float((d.get("format") or {}).get("duration") or 0)


def frames(path, fps, height, start=None, duration=None):
    """Yields (time, RGB image as float32) at `fps` images per second, `height` pixels high."""
    w0, h0, _ = video_size(path)
    width = int(round(w0 * height / h0 / 2)) * 2
    cmd = ["ffmpeg", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n, i = width * height * 3, 0
    try:
        while True:
            b = p.stdout.read(n)
            if len(b) < n:
                break
            yield (start or 0) + i / fps, np.frombuffer(b, np.uint8).reshape(height, width, 3).astype(np.float32)
            i += 1
    finally:
        p.stdout.close()
        p.wait()


def save_png(img, path, zoom=1):
    """Saves an RGB array as PNG (through ffmpeg: no image library needed)."""
    img = np.clip(img, 0, 255).astype(np.uint8)
    h, w = img.shape[:2]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-",
                    "-vf", f"scale={w * zoom}:{h * zoom}:flags=neighbor", str(path)], input=img.tobytes(), check=False)


# --------------------------------------------------------------------------- colour helpers
def hsv(f):
    f = f / 255.0
    mx, mn = f.max(-1), f.min(-1)
    d = mx - mn + 1e-6
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    h = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    return h, d / (mx + 1e-6), mx


def box_mean(a, r):
    """Mean over a (2r+1)x(2r+1) square around each pixel."""
    p = np.pad(a, r + 1, mode="edge")
    c = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k])[:a.shape[0], :a.shape[1]] / (k * k)


def components(mask, min_px, max_px):
    """Small, roughly round blobs of a mask: [(cy, cx, size)]."""
    H, W = mask.shape
    seen = np.zeros_like(mask, bool)
    out = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, pts = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            if len(pts) > max_px:
                break
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        if min_px <= len(pts) <= max_px:
            a = np.array(pts)
            h, w = np.ptp(a[:, 0]) + 1, np.ptp(a[:, 1]) + 1
            if max(h, w) <= 2.2 * min(h, w):
                out.append((float(a[:, 0].mean()), float(a[:, 1].mean()), len(pts)))
    return out


# --------------------------------------------------------------------------- in-game indicator
def excess(f):
    """How much greener / redder each pixel is than its surroundings; 0 where the pixel itself is
    not green-ish / red-ish (white text, gold text and grey labels do not count)."""
    d = [f[..., c] - box_mean(f[..., c], 7) for c in range(3)]
    ge = d[1] - np.maximum(d[0], d[2])
    re = d[0] - np.maximum(d[1], d[2])
    h, s, v = hsv(f)
    coloured = (s >= 0.3) & (v >= 0.1)
    ge = np.where(coloured & (h >= 80) & (h <= 220), ge, 0)          # green, or green over blue (teal)
    re = np.where(coloured & ((h >= 285) | (h <= 20)), re, 0)        # red, or red over blue (purple)
    return ge, re


def ingame_indicator(path, progress, fps=None, thr=9):
    fps = fps or INGAME_FPS
    fl = []
    for t, f in frames(path, fps, 720):
        fl.append((t, f))
        if len(fl) % 20 == 0:
            progress(f"cheat indicator: reading the video ({t:.0f} s)")
    if not fl:
        return []
    n = len(fl)
    cg = np.zeros(fl[0][1].shape[:2], np.int32)
    cr = np.zeros_like(cg)
    for i, (t, f) in enumerate(fl):
        ge, re = excess(f)
        cg += ge > thr
        cr += re > thr
        if i % 40 == 0:
            progress(f"cheat indicator: looking for it ({100 * i // n}%)")
    cand = (np.maximum(cg, cr) >= 0.35 * n) | ((cg + cr) >= 0.5 * n)
    cand[0, :] = cand[-1, :] = cand[:, 0] = cand[:, -1] = False
    spots = []
    for cy, cx, size in components(cand, 3, 120):
        y, x = int(round(cy)), int(round(cx))
        tl = []
        for t, f in fl:
            y0, x0 = max(0, y - 9), max(0, x - 9)
            ge, re = excess(f[y0:y + 10, x0:x + 10])
            yy, xx = y - y0, x - x0
            g0, r0 = ge[yy - 1:yy + 2, xx - 1:xx + 2].mean(), re[yy - 1:yy + 2, xx - 1:xx + 2].mean()
            tl.append("G" if g0 > thr * 0.7 and g0 > r0 else "R" if r0 > thr * 0.7 else ".")
        # a close-up of the spot, from an image where it is clearly visible
        best = next((i for i, c in enumerate(tl) if c != "."), 0)
        spots.append({"x": x, "y": y, "size": size, "timeline": "".join(tl), "fps": fps,
                      "green": tl.count("G"), "red": tl.count("R"), "none": tl.count("."),
                      "_frame": fl[best][1], "_t": fl[best][0]})
    # most consistent first
    spots.sort(key=lambda s: -(s["green"] + s["red"]))
    return spots


# --------------------------------------------------------------------------- end screen
def lime_mask(f):
    h, s, v = hsv(f)
    return (h >= 65) & (h <= 100) & (s >= 0.55) & (v >= 0.4)


def endscreen_box(f):
    """(left, bottom) of the end screen's green frame in an image, or None (any image height)."""
    k = f.shape[0] / 360
    lime = lime_mask(f)
    if lime.sum() < 4000 * k * k:
        return None
    col, row = lime.sum(0), lime.sum(1)
    if (col > 60 * k).sum() < 3 or (row > 130 * k).sum() < 2:
        return None
    left = int(np.argmax(col > 60 * k))
    bottom = int(len(row) - np.argmax(row[::-1] > 130 * k) - 1)
    rx = int(left + 421 * k)                       # the right border of the frame
    if rx >= f.shape[1] or col[max(0, rx - int(8 * k)):rx + int(8 * k)].max() < 60 * k:
        return None
    inside = f[max(0, int(bottom - 200 * k)):int(bottom - 60 * k), int(left + 60 * k):int(left + 360 * k)]
    if inside.size == 0 or inside.mean() > 90:    # the inside of the end screen is dark
        return None
    return left, bottom


def endscreen_corner(f, box):
    k = f.shape[0] / 360
    left, bottom = box
    return int(left + 16 * k), int(left + 50 * k), max(0, int(bottom - 282 * k)), max(0, int(bottom - 255 * k))


def endscreen_dots(f, box):
    """Dots in the end screen's top-left corner, left to right: e.g. 'G', 'GG', 'RR'."""
    k = f.shape[0] / 360
    x0, x1, y0, y1 = endscreen_corner(f, box)
    if y1 <= y0:
        return ""
    h, s, v = hsv(f[y0:y1, x0:x1])
    ok = (s >= 0.5) & (v >= 0.07)
    green = ok & (h >= 100) & (h <= 170)                    # not the end screen's own lime (hue ~70)
    red = ok & ((h <= 15) | (h >= 290))                    # can be a dark purplish red
    found = [(cx, c) for mask, c in ((green, "G"), (red, "R"))
             for cy, cx, size in components(mask, int(2 * k * k), int(40 * k * k))]
    return "".join(c for _, c in sorted(found))


def endscreen(path, progress):
    """Finds the end screen(s) and reads their dots: {seen, start, end, dots, frames, _frame, _box}."""
    progress("end screen: looking for it")
    maybe = [t for t, f in frames(path, 4, 360) if lime_mask(f).sum() > 2500]
    if not maybe:
        return {"seen": False}
    groups = []
    for t in maybe:
        if groups and t - groups[-1][1] <= 1.0:
            groups[-1][1] = t
        else:
            groups.append([t, t])
    seen = []
    for t0, t1 in groups:
        progress(f"end screen: reading {t0:.0f}–{t1:.0f} s")
        start = max(0.0, t0 - 0.5)
        for t, f in frames(path, 15, 720, start=start, duration=t1 - start + 1.5):
            box = endscreen_box(f)
            if box:
                seen.append((t, endscreen_dots(f, box), f, box))
    if not seen:
        return {"seen": False}
    # the dots seen on at least 3 images (0.2 s), the most frequent first
    counts = {}
    for t, d, f, box in seen:
        counts.setdefault(d, []).append((t, f, box))
    stable = sorted(((d, v) for d, v in counts.items() if len(v) >= 3 and d), key=lambda kv: -len(kv[1]))
    res = {"seen": True, "start": round(seen[0][0], 2), "end": round(seen[-1][0], 2),
           "patterns": {d or "-": len(v) for d, v in counts.items()}}
    if stable:
        d, v = stable[0]
        res.update(dots=d, frames=len(v), at=round(v[len(v) // 2][0], 2), _frame=v[len(v) // 2][1], _box=v[len(v) // 2][2])
    else:
        t, d, f, box = seen[len(seen) // 2]
        res.update(dots="", frames=0, at=round(t, 2), _frame=f, _box=box)
    return res


# --------------------------------------------------------------------------- verdict
def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def cheat_indicator(path, outdir, progress):
    spots = ingame_indicator(path, progress)
    end = endscreen(path, progress)
    images = {}
    # close-ups for the staff to see what was found
    for i, s in enumerate(spots[:2]):
        f = s.pop("_frame")
        t = s.pop("_t")
        y0, x0 = max(0, s["y"] - 25), max(0, s["x"] - 45)
        name = f"indicator_{i}.png"
        save_png(f[y0:s["y"] + 25, x0:s["x"] + 45], outdir / name, zoom=4)
        images[name] = f"in game at {fmt(t)}"
        s["image"] = name
    for s in spots[2:]:
        s.pop("_frame", None)
        s.pop("_t", None)
    if end.get("seen"):
        f, box = end.pop("_frame"), end.pop("_box")
        x0, x1, y0, y1 = endscreen_corner(f, box)
        save_png(f[max(0, y0 - 40):y1 + 60, max(0, x0 - 60):x1 + 160], outdir / "endscreen.png", zoom=2)
        end["image"] = "endscreen.png"

    # verdict, following the TSL standard ("cheat indicators must be enabled") and the staff guide
    ind = spots[0] if spots else None
    lines = []
    status = "ok"
    if ind:
        seen = ind["green"] + ind["red"]
        red_s = ind["red"] / ind["fps"]
        where = f"at ({ind['x']}, {ind['y']})"
        if ind["red"] >= 2 * ind["fps"]:           # red for at least 2 seconds during the video
            first = ind["timeline"].index("R") / ind["fps"]
            lines.append({"level": "bad", "text": f"In-game indicator {where} is red for {red_s:.0f} s (first at {fmt(first)})", "t": first})
            status = "bad"
        else:
            lines.append({"level": "ok", "text": f"In-game indicator {where}: green ({seen} of {len(ind['timeline'])} images)", "t": None})
    if end.get("seen"):
        d = end.get("dots", "")
        if "R" in d and "G" in d:
            lines.append({"level": "bad", "text": f"End screen indicator: green and red ({d}), cheats used during the run then turned off", "t": end["at"]})
            status = "bad"
        elif "R" in d:
            lines.append({"level": "bad", "text": f"End screen indicator: red ({d}), cheats still on when the level was completed", "t": end["at"]})
            status = "bad"
        elif "G" in d:
            lines.append({"level": "ok", "text": f"End screen indicator: green ({d})", "t": end["at"]})
        else:
            lines.append({"level": "warn", "text": "End screen seen, but no indicator dot on it", "t": end["at"]})
    if not ind and not (end.get("seen") and end.get("dots")):
        lines.append({"level": "warn", "text": "No cheat indicator found. Required if a mod menu is used (vanilla is exempt): check by eye", "t": None})
        status = "warn" if status == "ok" else status
    elif status == "ok" and any(l["level"] == "warn" for l in lines):
        status = "warn"
    if not end.get("seen"):
        lines.append({"level": "warn", "text": "No end screen found in the video (the end stats must be visible)", "t": None})
        if status == "ok":
            status = "warn"
    return {"status": status, "lines": lines, "ingame": spots[:3], "endscreen": end}


def run(video, outdir=None, progress=print):
    video = Path(video)
    outdir = Path(outdir or video.parent)
    t0 = time.time()
    global INGAME_FPS
    INGAME_FPS = 2 if video_size(video)[2] <= 240 else 1     # long videos: 1 image per second is enough
    result = {"version": VERSION, "cheatIndicator": cheat_indicator(video, outdir, progress)}
    result["seconds"] = round(time.time() - t0, 1)
    (outdir / "analysis.json").write_text(json.dumps(result), encoding="utf-8")
    return result


if __name__ == "__main__":
    r = run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, progress=lambda m: print("  …", m, file=sys.stderr))
    ci = r["cheatIndicator"]
    print(f"{sys.argv[1]}: {ci['status']} ({r['seconds']} s)")
    for line in ci["lines"]:
        print(f"  [{line['level']}] {line['text']}")
