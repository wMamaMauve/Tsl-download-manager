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

VERSION = 2   # bump when the analysis changes: older results are computed again
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


def edges(lum):
    gx = np.abs(np.diff(lum, axis=1, prepend=lum[:, :1]))
    gy = np.abs(np.diff(lum, axis=0, prepend=lum[:1, :]))
    return np.maximum(gx, gy) > 18


def scan_screen(path, progress, fps=None, thr=9):
    """Two passes over the video at 720p, keeping only running totals (low memory):
    1. where pixels are often greener / redder than around (indicator), and where outlines stay
       in place (mod menu labels);
    2. the indicator candidates' colour over time, and which outlines are really unchanged."""
    fps = fps or INGAME_FPS
    n = 0
    cg = cr = edge_n = lum_sum = None
    for t, f in frames(path, fps, 720):
        ge, re = excess(f)
        lum = f.mean(-1)
        if cg is None:
            cg, cr = np.zeros(lum.shape, np.int32), np.zeros(lum.shape, np.int32)
            edge_n, lum_sum = np.zeros(lum.shape, np.int32), np.zeros(lum.shape, np.float64)
        cg += ge > thr
        cr += re > thr
        edge_n += edges(lum)
        lum_sum += lum
        n += 1
        if n % 20 == 0:
            progress(f"screen check 1/2 ({t:.0f} s)")
    if not n:
        return [], [], None
    cand = (np.maximum(cg, cr) >= 0.35 * n) | ((cg + cr) >= 0.5 * n)
    cand[0, :] = cand[-1, :] = cand[:, 0] = cand[:, -1] = False
    spots = [{"x": int(round(cx)), "y": int(round(cy)), "size": size, "tl": []} for cy, cx, size in components(cand, 3, 120)]
    lum_mean = lum_sum / n
    still_n = np.zeros(lum_mean.shape, np.int32)
    sample = None
    for i, (t, f) in enumerate(frames(path, fps, 720)):
        lum = f.mean(-1)
        still_n += edges(lum) & (np.abs(lum - lum_mean) < 12)
        if i == n // 2:
            sample = f.astype(np.uint8)
        for s in spots:
            x, y = s["x"], s["y"]
            y0, x0 = max(0, y - 9), max(0, x - 9)
            ge, re = excess(f[y0:y + 10, x0:x + 10])
            yy, xx = y - y0, x - x0
            g0, r0 = ge[yy - 1:yy + 2, xx - 1:xx + 2].mean(), re[yy - 1:yy + 2, xx - 1:xx + 2].mean()
            c = "G" if g0 > thr * 0.7 and g0 > r0 else "R" if r0 > thr * 0.7 else "."
            s["tl"].append(c)
            if c != "." and "_crop" not in s:   # a close-up where the dot is visible
                s["_crop"], s["_t"] = f[max(0, y - 25):y + 25, max(0, x - 45):x + 45].astype(np.uint8), t
        if i % 20 == 0:
            progress(f"screen check 2/2 ({t:.0f} s)")
    for s in spots:
        tl = s.pop("tl")
        s.update(timeline="".join(tl), fps=fps, green=tl.count("G"), red=tl.count("R"), none=tl.count("."))
    spots.sort(key=lambda s: -(s["green"] + s["red"]))
    # outlines present and unchanged in most images (or present while translucent): still overlays
    still = (still_n > 0.4 * n) | (edge_n > 0.6 * n)
    return spots, still_regions(still), sample


def still_regions(still):
    """Groups of 8x8 cells with still outlines -> [(x0, y0, x1, y1, cells)], classified."""
    H, W = still.shape
    cells = still[:H // 8 * 8, :W // 8 * 8].reshape(H // 8, 8, W // 8, 8).sum((1, 3)) >= 4
    ch, cw = cells.shape
    seen = np.zeros_like(cells)
    regs = []
    for y0, x0 in zip(*np.nonzero(cells)):
        if seen[y0, x0]:
            continue
        st, pts = [(y0, x0)], []
        seen[y0, x0] = True
        while st:
            y, x = st.pop()
            pts.append((y, x))
            for yy in range(y - 1, y + 2):
                for xx in range(x - 2, x + 3):
                    if 0 <= yy < ch and 0 <= xx < cw and cells[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        st.append((yy, xx))
        if len(pts) >= 3:
            a = np.array(pts)
            regs.append([int(a[:, 1].min() * 8), int(a[:, 0].min() * 8), int((a[:, 1].max() + 1) * 8), int((a[:, 0].max() + 1) * 8), len(pts)])
    bar = next((r for r in regs if r[3] <= 56 and r[0] < W * 0.62 and r[2] > W * 0.38 and r[2] - r[0] > W * 0.25), None)
    out = []
    for x0, y0, x1, y1, c in regs:
        if (x0, y0, x1, y1, c) == tuple(bar or ()):
            kind = "progress bar"
        elif y0 >= H - 100 and x1 - x0 > W * 0.3:
            kind = "ground"
        elif y1 - y0 <= 8:
            kind = "line"
        elif y1 <= 64 and ((bar and x0 >= bar[0] - 80 and x1 <= bar[2] + 220) or (not bar and x0 < W * 0.62 and x1 > W * 0.38)):
            kind = "percentage"
        else:
            kind = "label"
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "cells": c, "kind": kind})
    return out


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
    seen = []        # (time, dots)
    reps = {}        # dots -> (time, image, box): one image per pattern, after its first 2 appearances
    for t0, t1 in groups:
        progress(f"end screen: reading {t0:.0f}–{t1:.0f} s")
        start = max(0.0, t0 - 0.5)
        for t, f in frames(path, 15, 720, start=start, duration=t1 - start + 1.5):
            box = endscreen_box(f)
            if box:
                d = endscreen_dots(f, box)
                seen.append((t, d))
                if sum(1 for _, x in seen if x == d) in (1, 3):
                    reps[d] = (t, f.astype(np.uint8), box)
    if not seen:
        return {"seen": False}
    counts = {}
    for t, d in seen:
        counts[d] = counts.get(d, 0) + 1
    # the dots seen on at least 3 images (0.2 s), the most frequent first
    stable = sorted((d for d, c in counts.items() if c >= 3 and d), key=lambda d: -counts[d])
    res = {"seen": True, "start": round(seen[0][0], 2), "end": round(seen[-1][0], 2),
           "patterns": {d or "-": c for d, c in counts.items()}}
    d = stable[0] if stable else max(counts, key=counts.get)
    t, f, box = reps[d]
    res.update(dots=d if stable else "", frames=counts[d] if stable else 0, at=round(t, 2), _frame=f, _box=box)
    return res


# --------------------------------------------------------------------------- verdict
def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def cheat_indicator(path, outdir, progress):
    spots, regions, sample = scan_screen(path, progress)
    end = endscreen(path, progress)
    # close-ups for the staff to see what was found
    for i, s in enumerate(spots):
        crop, t = s.pop("_crop", None), s.pop("_t", None)
        if i < 2 and crop is not None:
            name = f"indicator_{i}.png"
            save_png(crop, outdir / name, zoom=4)
            s["image"], s["imageTime"] = name, t
    labels = sorted((r for r in regions if r["kind"] == "label"), key=lambda r: -r["cells"])[:4]
    for i, r in enumerate(labels):
        if sample is not None:
            name = f"label_{i}.png"
            save_png(sample[max(0, r["y0"] - 4):r["y1"] + 4, max(0, r["x0"] - 4):r["x1"] + 4], outdir / name, zoom=2)
            r["image"] = name
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
        if labels:
            lines.append({"level": "bad", "text": f"No cheat indicator found, but {len(labels)} mod menu label(s) are on the screen (see below): the indicator is required", "t": None})
            status = "bad"
        else:
            lines.append({"level": "warn", "text": "No cheat indicator and no mod menu label found: vanilla game (exempt), or check by eye", "t": None})
            status = "warn" if status == "ok" else status
    elif status == "ok" and any(l["level"] == "warn" for l in lines):
        status = "warn"
    if not end.get("seen"):
        lines.append({"level": "warn", "text": "No end screen found in the video (the end stats must be visible)", "t": None})
        if status == "ok":
            status = "warn"
    return {"status": status, "lines": lines, "ingame": spots[:3], "endscreen": end, "labels": labels}


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
