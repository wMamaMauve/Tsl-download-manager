#!/usr/bin/env python3
"""extract_sprites.py — cuts one image per object ID out of YOUR copy of Geometry Dash.

    extract_sprites.py <GD Resources folder> [output folder]

The game's sprite sheets (GJ_GameSheet*.png + .plist, …) are read from the Resources
folder (Steam: steamapps/common/Geometry Dash/Resources), and every object listed in
object_frames.tsv is saved as <output>/<id>.png (default /var/lib/tsl-sync/objects).
The bot shows these images in /diff. The images are the game's art: keep them on your
server, don't publish them.
Needs Pillow (installed in the bot's venv by install-bot.sh):
    /opt/tsl-sync/venv/bin/python /opt/tsl-sync/extract_sprites.py "<Resources>"
"""
import plistlib
import re
import sys
from pathlib import Path

from PIL import Image

QUALITY = ("-uhd", "-hd", "")   # best first


def numbers(text):
    return [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", str(text))]


def frame_rect(info):
    """-> (x, y, w, h, rotated) for the plist formats used by cocos2d (0 to 3)."""
    if "textureRect" in info:                                  # format 3
        x, y, w, h = numbers(info["textureRect"])[:4]
        return x, y, w, h, bool(info.get("textureRotated"))
    if "frame" in info:                                        # format 1/2
        x, y, w, h = numbers(info["frame"])[:4]
        return x, y, w, h, bool(info.get("rotated"))
    return int(info["x"]), int(info["y"]), int(info["width"]), int(info["height"]), False


def load_sheets(res):
    """-> {frame name: (sheet path, rect)} using the best quality available for each sheet."""
    groups = {}
    for plist in res.glob("*.plist"):
        stem = plist.stem
        quality = next(q for q in QUALITY if not q or stem.endswith(q))
        groups.setdefault(stem[: len(stem) - len(quality)] if quality else stem, {})[quality] = plist
    frames = {}
    for base, versions in sorted(groups.items()):
        plist = next(versions[q] for q in QUALITY if q in versions)
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("frames"), dict):
            continue   # not a sprite sheet
        meta = data.get("metadata", {})
        texture = meta.get("realTextureFileName") or meta.get("textureFileName") or plist.stem + ".png"
        png = plist.with_name(Path(texture).name)
        if not png.exists():
            png = plist.with_suffix(".png")
        if not png.exists():
            continue
        for name, info in data["frames"].items():
            try:
                frames.setdefault(name, (png, frame_rect(info)))
            except (KeyError, ValueError):
                pass
    return frames


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    res = Path(sys.argv[1]).expanduser()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "/var/lib/tsl-sync/objects")
    table = Path(__file__).with_name("object_frames.tsv")
    frames = load_sheets(res)
    if not frames:
        sys.exit(f"No sprite sheet (.plist + .png) found in {res}")
    out.mkdir(parents=True, exist_ok=True)
    sheets, done, missing = {}, 0, 0
    for line in table.read_text(encoding="utf-8").splitlines():
        oid, _, frame = line.partition("\t")
        if not oid.isdigit():
            continue
        if frame not in frames:
            missing += 1
            continue
        png, (x, y, w, h, rotated) = frames[frame]
        if png not in sheets:
            sheets[png] = Image.open(png).convert("RGBA")
        img = sheets[png].crop((x, y, x + h, y + w)).rotate(90, expand=True) if rotated \
            else sheets[png].crop((x, y, x + w, y + h))
        img.thumbnail((128, 128))
        img.save(out / f"{oid}.png")
        done += 1
    print(f"{done} object images saved in {out} ({missing} objects without a sprite in these sheets)")


if __name__ == "__main__":
    main()
