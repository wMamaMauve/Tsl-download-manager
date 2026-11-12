#!/usr/bin/env python3
"""checker.py — Record Checker: a small web app for list staff to check a record video.

Paste a YouTube link: the server downloads the video (yt-dlp), prepares its sound for the click
analysis (ffmpeg) and serves both to the page, which shows the video with frame-by-frame controls,
slow motion and the clicks on a timeline.

Config: /etc/record-checker.conf (see record-checker.conf.example), or the CHECKER_CONF variable.
Run:    /opt/tsl-sync/venv/bin/python /opt/record-checker/checker.py
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sys
import time
from urllib.parse import quote
from pathlib import Path

from aiohttp import web

import analyze

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("record-checker")


def load_conf(path):
    conf = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        conf[key.strip()] = parts[0] if parts else ""
    return conf


CONF = load_conf(os.environ.get("CHECKER_CONF", "/etc/record-checker.conf"))


def setting(name, default=""):
    return os.environ.get(name) or CONF.get(name) or default


PASSWORD = setting("CHECKER_PASSWORD")
HOST = setting("CHECKER_HOST", "127.0.0.1")
PORT = int(setting("CHECKER_PORT", "8099"))
DATA_DIR = Path(setting("CHECKER_DATA_DIR", "/var/lib/record-checker"))
MAX_GB = float(setting("CHECKER_MAX_GB", "10"))            # videos kept on disk (oldest removed first)
MAX_HEIGHT = int(setting("CHECKER_MAX_HEIGHT", "1080"))     # video quality downloaded: up to 1080p
MAX_FPS = int(setting("CHECKER_MAX_FPS", "60"))             # … 60 fps
MAX_MBPS = float(setting("CHECKER_MAX_MBPS", "13"))         # … and 13 Mbit/s (video)
MAX_MINUTES = int(setting("CHECKER_MAX_MINUTES", "30"))     # longer videos are refused
SECURE_COOKIE = setting("CHECKER_SECURE_COOKIE", "0") == "1"   # 1 behind HTTPS
YTDLP = setting("YTDLP", str(Path(sys.executable).with_name("yt-dlp")) if Path(sys.executable).with_name("yt-dlp").exists()
                else shutil.which("yt-dlp") or "yt-dlp")
FFMPEG = setting("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = setting("FFPROBE", shutil.which("ffprobe") or "ffprobe")
STATIC = Path(__file__).with_name("static")
VIDEOS = DATA_DIR / "videos"

YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([A-Za-z0-9_-]{11})")
JOBS = {}          # video id -> {"state", "progress", "error", "meta", "analysis"}
DOWNLOADS = asyncio.Semaphore(2)
ANALYSES = asyncio.Semaphore(1)     # the analysis uses the CPU: one video at a time
MEDIA_FILES = {"video.mp4", "audio.flac", "analysis.json", "indicator_0.png", "indicator_1.png", "endscreen.png",
               "label_0.png", "label_1.png", "label_2.png", "label_3.png"}


# --------------------------------------------------------------------------- login
def _secret():
    path = DATA_DIR / "secret"
    try:
        return path.read_bytes()
    except OSError:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        os.chmod(path, 0o600)
        return key


def make_session():
    expires = str(int(time.time()) + 30 * 86400)
    sig = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session(value):
    try:
        expires, sig = value.split(".", 1)
        good = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(expires) > time.time()
    except (AttributeError, ValueError):
        return False


@web.middleware
async def auth(request, handler):
    if request.path in ("/login", "/favicon.ico") or valid_session(request.cookies.get("rc_session")):
        return await handler(request)
    if request.path.startswith(("/api/", "/media/")):
        return web.json_response({"error": "Please log in"}, status=401)
    raise web.HTTPFound("/login" if request.path_qs == "/" else "/login?next=" + quote(request.path_qs, safe=""))


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Record Checker</title><style>
:root{--bg:#f7f7fb;--fg:#15161c;--card:#fff;--line:#e3e5ee;--accent:#287dff}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e8e9ef;--card:#1a1d24;--line:#2a2e38;--accent:#5b9bff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;width:min(340px,90vw)}
input,button{font:inherit;width:100%;padding:9px 12px;border-radius:9px;border:1px solid var(--line);margin-top:10px;background:var(--bg);color:var(--fg)}
button{background:var(--accent);color:#fff;border:0;font-weight:600;cursor:pointer}.err{color:#e5484d;margin-top:8px}</style></head>
<body><form method="post"><h2 style="margin:0">Record Checker</h2><div style="opacity:.7">TSL list staff</div>
<input type="password" name="password" placeholder="Password" autofocus required><input type="hidden" name="next" value="__NEXT__">
<button>Log in</button>__ERR__</form></body></html>"""


async def login(request):
    nxt = request.query.get("next", "/")
    err = ""
    if request.method == "POST":
        data = await request.post()
        nxt = str(data.get("next") or "/")
        if PASSWORD and hmac.compare_digest(str(data.get("password", "")), PASSWORD):
            resp = web.HTTPFound(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            resp.set_cookie("rc_session", make_session(), max_age=30 * 86400, httponly=True, samesite="Lax",
                            secure=SECURE_COOKIE)
            raise resp
        await asyncio.sleep(1)   # slows down password guessing
        err = '<div class="err">Wrong password</div>'
    page = LOGIN_PAGE.replace("__NEXT__", nxt.replace('"', "")).replace("__ERR__", err)
    return web.Response(text=page, content_type="text/html")


# --------------------------------------------------------------------------- videos
def paths(vid):
    d = VIDEOS / vid
    return d, d / "video.mp4", d / "audio.flac", d / "meta.json"


async def run(*cmd, progress=None):
    """Runs a command; `progress(line)` gets each output line. Returns (code, output)."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines = []
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if len(lines) < 20000:
            lines.append(line)
        if progress:
            progress(line)
    return await proc.wait(), "\n".join(lines)


def cleanup():
    """Removes the oldest videos when they take more than MAX_GB."""
    dirs = [d for d in VIDEOS.glob("*") if d.is_dir() and JOBS.get(d.name, {}).get("state") != "working"]
    sizes = {d: sum(f.stat().st_size for f in d.glob("*") if f.is_file()) for d in dirs}
    total = sum(sizes.values())
    for d in sorted(dirs, key=lambda d: d.stat().st_mtime):
        if total <= MAX_GB * 1024 ** 3:
            break
        total -= sizes[d]
        shutil.rmtree(d, ignore_errors=True)
        JOBS.pop(d.name, None)
        log.info("removed %s (disk limit)", d.name)


async def prepare(vid):
    job = JOBS[vid]
    d, video, audio, meta = paths(vid)
    d.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    async with DOWNLOADS:
        try:
            job.update(state="working", progress="Reading the video's page", error=None)
            code, out = await run(YTDLP, "--no-playlist", "--skip-download", "--write-info-json", "--no-warnings",
                                  "-o", str(d / "info"), url)
            info_file = d / "info.info.json"
            if code != 0 or not info_file.exists():
                raise RuntimeError(yt_error(out))
            info = json.loads(info_file.read_text(encoding="utf-8"))
            info_file.unlink(missing_ok=True)
            if (info.get("duration") or 0) > MAX_MINUTES * 60:
                raise RuntimeError(f"This video is longer than {MAX_MINUTES} minutes.")

            def dl_progress(line):
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    job["progress"] = f"Downloading the video: {float(m.group(1)):.0f}%"
            # H.264 + AAC first: plays in every browser (and keeps the 60 fps when there is one)
            # best video up to MAX_HEIGHT / MAX_FPS / MAX_MBPS ("?" = also when YouTube does not give the value);
            # sorted by resolution, then fps, then H.264 (plays and seeks frame by frame everywhere) at equal quality
            lim = f"[height<={MAX_HEIGHT}][fps<=?{MAX_FPS}][tbr<=?{MAX_MBPS * 1000:.0f}]"
            fmt = f"bv*{lim}+ba/b{lim}/bv*[height<={MAX_HEIGHT}]+ba/b"
            code, out = await run(YTDLP, "--no-playlist", "--newline", "--no-warnings", "-f", fmt, "-S", "res,fps,vcodec:h264,acodec:aac",
                                  "--merge-output-format", "mp4", "-o", str(d / "video.%(ext)s"), url,
                                  progress=dl_progress)
            got = next((f for f in d.glob("video.*") if f.suffix in (".mp4", ".webm", ".mkv")), None)
            if code != 0 or not got:
                raise RuntimeError(yt_error(out))
            if got.suffix != ".mp4":   # some videos only exist in VP9/AV1: put them in an mp4 box
                job["progress"] = "Converting the video"
                code, out = await run(FFMPEG, "-y", "-loglevel", "error", "-i", str(got), "-c", "copy", str(video))
                if code != 0:
                    code, out = await run(FFMPEG, "-y", "-loglevel", "error", "-i", str(got), "-c:v", "libx264",
                                          "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(video))
                got.unlink(missing_ok=True)
                if code != 0:
                    raise RuntimeError("Could not convert the video: " + out[-300:])
            job["progress"] = "Preparing the sound"
            code, out = await run(FFMPEG, "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1",
                                  "-ar", "48000", "-c:a", "flac", str(audio))
            if code != 0:
                raise RuntimeError("Could not read the sound: " + out[-300:])
            code, out = await run(FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=avg_frame_rate,r_frame_rate,width,height,nb_frames:format=duration",
                                  "-of", "json", str(video))
            probe = json.loads(out) if code == 0 else {}
            st = (probe.get("streams") or [{}])[0]
            fps = frac(st.get("avg_frame_rate")) or frac(st.get("r_frame_rate")) or info.get("fps") or 30
            data = {
                "id": vid, "url": url, "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
                "channelUrl": info.get("channel_url") or info.get("uploader_url"),
                "uploadDate": info.get("upload_date"), "duration": float((probe.get("format") or {}).get("duration") or info.get("duration") or 0),
                "fps": round(fps, 3), "width": st.get("width"), "height": st.get("height"),
                "views": info.get("view_count"), "description": (info.get("description") or "")[:2000],
                "downloaded": int(time.time()),
            }
            meta.write_text(json.dumps(data), encoding="utf-8")
            job.update(state="ready", progress="Ready", meta=data)
            log.info("ready: %s (%s, %.0f fps)", vid, data["title"], data["fps"])
            start_analysis(vid)
        except Exception as e:   # shown on the page
            log.warning("could not prepare %s: %s", vid, e)
            job.update(state="error", error=str(e)[:500])
            shutil.rmtree(d, ignore_errors=True)
    cleanup()


def analysis_done(vid):
    f = paths(vid)[0] / "analysis.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("version") == analyze.VERSION
    except (OSError, ValueError):
        return False


def start_analysis(vid):
    """Runs the automatic checks in the background (once per video and analysis version)."""
    job = JOBS[vid]
    if analysis_done(vid):
        job["analysis"] = {"state": "done", "progress": ""}
        return
    if (job.get("analysis") or {}).get("state") in ("waiting", "running"):
        return
    job["analysis"] = {"state": "waiting", "progress": "waiting for another video's analysis"}

    async def go():
        async with ANALYSES:
            job["analysis"] = {"state": "running", "progress": "starting"}
            d, video, _, _ = paths(vid)

            def progress(text):
                job["analysis"]["progress"] = text
            try:
                await asyncio.to_thread(analyze.run, video, d, progress)
                job["analysis"] = {"state": "done", "progress": ""}
                log.info("analysed: %s", vid)
            except Exception as e:
                log.exception("analysis of %s failed", vid)
                job["analysis"] = {"state": "error", "progress": str(e)[:300]}
    asyncio.create_task(go())


def frac(text):
    try:
        a, _, b = str(text).partition("/")
        return float(a) / float(b or 1) if float(b or 1) else None
    except ValueError:
        return None


def yt_error(out):
    m = re.findall(r"ERROR: (.*)", out)
    msg = m[-1] if m else out[-300:]
    if "Sign in to confirm" in msg:
        msg += " (YouTube blocks this server: see the README to give yt-dlp cookies)"
    return msg


def job_of(vid):
    if vid in JOBS:
        return JOBS[vid]
    _, video, audio, meta = paths(vid)
    if video.exists() and audio.exists() and meta.exists():
        JOBS[vid] = {"state": "ready", "progress": "Ready", "error": None,
                     "meta": json.loads(meta.read_text(encoding="utf-8")), "analysis": None}
        start_analysis(vid)
        return JOBS[vid]
    return None


async def api_load(request):
    data = await request.json()
    m = YT_ID.search(str(data.get("url", ""))) or re.fullmatch(r"([A-Za-z0-9_-]{11})", str(data.get("url", "")).strip())
    if not m:
        return web.json_response({"error": "This is not a YouTube video link."}, status=400)
    vid = m.group(1)
    job = job_of(vid)
    if not job or job["state"] == "error":
        JOBS[vid] = {"state": "working", "progress": "Starting", "error": None, "meta": None, "analysis": None}
        asyncio.create_task(prepare(vid))
    else:
        os.utime(paths(vid)[0])   # recently used: removed last
        if job["state"] == "ready":
            start_analysis(vid)
    return web.json_response({"id": vid, **JOBS[vid]})


async def api_job(request):
    vid = request.match_info["vid"]
    job = job_of(vid)
    if not job:
        return web.json_response({"error": "Unknown video"}, status=404)
    return web.json_response({"id": vid, **job})


async def media(request):
    vid, kind = request.match_info["vid"], request.match_info["kind"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise web.HTTPNotFound()
    if kind not in MEDIA_FILES:
        raise web.HTTPNotFound()
    f = paths(vid)[0] / kind
    if not f.exists():
        raise web.HTTPNotFound()
    if kind != "video.mp4" and kind != "audio.flac":   # analysis results change when the analysis is updated
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})
    return web.FileResponse(f, headers={"Cache-Control": "private, max-age=86400"})   # supports Range (seeking)


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def main():
    if not PASSWORD:
        raise SystemExit("CHECKER_PASSWORD is missing in /etc/record-checker.conf")
    VIDEOS.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[auth], client_max_size=1024 ** 2)
    app.router.add_route("*", "/login", login)
    app.router.add_get("/", index)
    app.router.add_post("/api/load", api_load)
    app.router.add_get("/api/job/{vid}", api_job)
    app.router.add_get("/media/{vid}/{kind}", media)
    log.info("Record Checker on http://%s:%d", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
