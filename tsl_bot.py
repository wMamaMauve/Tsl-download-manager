#!/usr/bin/env python3
"""Discord bot for tsl-sync.

- Posts the changes detected by tsl-sync.sh to a channel
  (files dropped in DATA_DIR/notifications/).
- Slash commands: /level, /diff, /changes, /list, /status, /run.

Configuration: the same file as tsl-sync (/etc/tsl-sync.conf or TSL_CONF):
    DISCORD_BOT_TOKEN="..."      bot token (Discord Developer Portal)
    DISCORD_CHANNEL_ID="..."     channel where announcements are posted
    DISCORD_GUILD_ID="..."       (optional) Discord server, so commands show up immediately
"""

import asyncio
import datetime as dt
import logging
import os
import random
import re
import shlex
import json
import subprocess
import time
import urllib.request
import io
from pathlib import Path

import discord
from discord import app_commands

try:   # optional: pictures of the changed objects in /diff
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

log = logging.getLogger("tsl-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

EMBED_COLOUR = 0x3083F9    # side bar colour of every embed
DISCORD_MAX = 1900          # Discord message limit (2000) with a margin
UPLOAD_MAX = 10 * 1024**2   # max attachment size for a bot


# --------------------------------------------------------------------------- configuration
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


CONF = load_conf(os.environ.get("TSL_CONF", "/etc/tsl-sync.conf"))


def setting(key, default=""):
    return os.environ.get(key) or CONF.get(key) or default


def first_existing(*paths):
    for p in paths:
        if p.exists():
            return p
    return paths[0]


DATA_DIR = Path(setting("DATA_DIR", "/var/lib/tsl-sync"))
LISTS = setting("LISTS", "tsl tslplus").split()
LIST_BASE_URL = setting("LIST_BASE_URL", "https://tsl.pages.dev/list").rstrip("/")
TOKEN = setting("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(setting("DISCORD_CHANNEL_ID", "0") or 0)
GUILD_ID = int(setting("DISCORD_GUILD_ID", "0") or 0)
NOTIF_DIR = DATA_DIR / "notifications"
LIST_NAMES = {"tsl": "The Shitty List", "tslplus": "The Shitty List Plus"}


def files_dir():
    return first_existing(DATA_DIR / "files", DATA_DIR / "fichiers")


# --------------------------------------------------------------------------- data access
def read_tsv(path):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.split("\t") for line in lines if line]


def load_index():
    """Current ranking: list of dicts (list, position, id, name, author, file)."""
    levels = []
    for lst in LISTS:
        rows = read_tsv(files_dir() / lst / "index.tsv")
        for row in rows[1:]:
            if len(row) < 5:
                continue
            pos, lid, name, author, rel = row[:5]
            video = row[5] if len(row) > 5 and row[5].startswith("http") else ""
            showcase = row[6] if len(row) > 6 and row[6].startswith("http") else ""
            levels.append({"list": lst, "position": int(pos) if pos.isdigit() else 0,
                           "id": lid, "name": name, "author": author, "file": rel,
                           "video": video, "showcase": showcase, "has_videos": len(row) > 6})
    return levels


def load_state():
    state = {}
    for row in read_tsv(DATA_DIR / "state.tsv"):
        row += [""] * (7 - len(row))
        lid, ver, _sha, lst, rel, _info, status = row[:7]
        state[lid] = {"version": "" if ver == "-" else ver, "list": lst, "file": rel,
                      "status": "deleted" if status in ("deleted", "supprime") else "ok"}
    return state


def load_log():
    path = first_existing(DATA_DIR / "changes.log", DATA_DIR / "changements.log")
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_levels(query):
    q = query.strip().lower()
    levels = load_index()
    exact = [lv for lv in levels if lv["id"] == q or lv["name"].lower() == q]
    if exact:
        return exact
    return [lv for lv in levels if q in lv["name"].lower() or q in lv["author"].lower()]


# log keywords (English, plus the French ones written by older versions)
IMPORTANT = ("MODIFIED", "INFO CHANGED", "DELETED", "REAPPEARED", "ENTERED", "LEFT",
             "MODIFIÉ", "INFOS MODIFIÉES", "SUPPRIMÉ", "RÉAPPARU", "ENTRÉE", "SORTIE")


def split_messages(lines, header=""):
    """Groups lines into messages under 2000 characters."""
    messages, current = [], header
    for line in lines:
        if len(line) > DISCORD_MAX:
            line = line[: DISCORD_MAX - 1] + "…"
        if current and len(current) + len(line) + 1 > DISCORD_MAX:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        messages.append(current)
    return messages


def strip_versions(line):
    """Removes version numbers from a log line: 'v1 -> v2', a trailing 'v1', and the
    '(previous version in history/…)' note (English and old French lines)."""
    line = re.sub(r"\s*\((?:previous version in|ancienne version dans) .*\)\s*$", "", line)
    line = re.sub(r"\s+v\d+\s*(?:->|→)\s*v\d+", "", line)
    line = re.sub(r"\s+v\d+\s*$", "", line)
    return line


def current_progress():
    """Current step of a running tsl-sync (None if no run in progress)."""
    path = DATA_DIR / "progress.txt"
    try:
        if dt.datetime.now().timestamp() - path.stat().st_mtime > 15 * 60:
            return None   # stale file (run killed): ignore it
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


IDLE_ROTATE = 60   # seconds before the idle activity shows another random level
_IDLE = {"text": "", "until": 0.0}


def idle_activity_text():
    """'Shitty NETWORK on TSL': a random level of any list, changed every IDLE_ROTATE seconds."""
    if _IDLE["text"] and time.time() < _IDLE["until"]:
        return _IDLE["text"]
    levels = load_index()
    if levels:
        lv = random.choice(levels)
        label = {"tsl": "TSL", "tslplus": "TSL+"}.get(lv["list"], lv["list"])
        _IDLE["text"] = f"{lv['name']} on {label}"[:128]
    else:
        _IDLE["text"] = "The Shitty List"
    _IDLE["until"] = time.time() + IDLE_ROTATE
    return _IDLE["text"]


def discord_time(text, style="f"):
    """'2026-11-10 04:21[:07]' or '2026-11-10' (server local time) -> Discord timestamp markup,
    shown to every user in their own time zone and language. Unparsable text is returned as is.
    Styles: f = date and time, D = date, R = relative ("2 hours ago")."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return f"<t:{int(dt.datetime.strptime(text.strip(), fmt).timestamp())}:{style}>"
        except ValueError:
            pass
    return text


def short_log_line(line):
    """'2026-11-10 04:21:07 [tsl] MODIFIED: …' -> '<t:…:f> [tsl] MODIFIED: …'"""
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)", line)
    return f"{discord_time(m.group(1))} {m.group(2)}" if m else line


# --------------------------------------------------------------------------- bot
class CopyIdButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tsl:copyid:(?P<level_id>[0-9]+)"):
    """'Copy ID' button: replies (only to the user who clicks) with the bare level ID, easy to copy.
    Bots cannot write to the clipboard, so this is the closest Discord allows."""

    def __init__(self, level_id: str):
        super().__init__(discord.ui.Button(label="Copy ID", style=discord.ButtonStyle.secondary,
                                           custom_id=f"tsl:copyid:{level_id}"))
        self.level_id = level_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["level_id"])

    async def callback(self, interaction):
        await interaction.response.send_message(self.level_id, ephemeral=True)


def level_view(level_id):
    view = discord.ui.View(timeout=None)
    view.add_item(CopyIdButton(str(level_id)))
    return view


class TSLBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.presence = None

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.add_dynamic_items(CopyIdButton)   # buttons keep working after a restart
        self.loop.create_task(self.notification_loop())

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def notification_loop(self):
        """Posts the notification files dropped by tsl-sync.sh."""
        await self.wait_until_ready()
        NOTIF_DIR.mkdir(parents=True, exist_ok=True)
        while not self.is_closed():
            try:
                await self.flush_notifications()
            except Exception:  # never stop the loop
                log.exception("error while posting notifications")
            try:
                await self.update_presence()
            except Exception:
                log.exception("error while updating the status")
            await asyncio.sleep(5)   # status and announcements checked every 5 s

    async def update_presence(self):
        """During a run: its progress as custom status. Otherwise: "Watching <#1 of TSL> on TSL"."""
        step = current_progress()
        if step:
            text, activity = step[:128], discord.CustomActivity(name=step[:128])
        else:
            text = idle_activity_text()
            activity = discord.Activity(type=discord.ActivityType.watching, name=text)
        if text != self.presence:
            await self.change_presence(activity=activity)
            self.presence = text

    async def flush_notifications(self):
        files = sorted(NOTIF_DIR.glob("*.txt"))
        if not files or not CHANNEL_ID:
            return
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        for path in files:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for embed in announcement_embeds(lines):
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                await asyncio.sleep(1)
            path.unlink()


# emojis written by older versions of tsl-sync.sh (only the check mark is kept)
OLD_EMOJIS = re.compile(r"^(?:📋|✏️|🗑️|♻️|📝|➕|➖|↕️|📦|⚠️|🔄|📜|⬆️)\s*")
EMBED_MAX = 4000   # embed description limit (4096) with a margin


ANNOUNCE_TITLE = "TSL Tracking result"
NO_CHANGES = "✅ No changes were detected."


def announcement_embeds(lines):
    """Turns a notification file (title line, change lines, '_summary_' line) into embeds.
    The title and summary lines written by tsl-sync.sh are not shown."""
    body = []
    for line in lines:
        line = OLD_EMOJIS.sub("", line.strip())
        if not line:
            continue
        if "run of" in line and line.replace("*", "").startswith("TSL"):
            continue   # "TSL — run of <date>": replaced by a fixed title
        if line.startswith("_") and line.endswith("_") and len(line) > 2:
            continue   # summary line ("0 new, 1 modified, ...")
        if line in ("✅ No changes.", "No changes.", NO_CHANGES):
            continue
        body.append(re.sub(r"\s*\(v\d+\s*→\s*v\d+\)", "", line))   # old "(v2 → v3)"
    if not body:
        body = [NO_CHANGES]
    title = ANNOUNCE_TITLE

    chunks, current = [], ""
    for line in body:
        if len(line) > EMBED_MAX:
            line = line[: EMBED_MAX - 1] + "…"
        if current and len(current) + len(line) + 1 > EMBED_MAX:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    chunks.append(current)

    embeds = []
    for i, chunk in enumerate(chunks):
        name = title if len(chunks) == 1 else f"{title} ({i + 1}/{len(chunks)})"
        embeds.append(discord.Embed(title=name[:256], description=chunk, colour=EMBED_COLOUR))
    return embeds


bot = TSLBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Any error in a command: log it and tell the user, instead of 'The application did not respond'."""
    log.error("error in /%s", interaction.command.name if interaction.command else "?", exc_info=error)
    message = "Something went wrong with this command. The details are in the bot's log (journalctl -u tsl-bot)."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def level_autocomplete(_interaction, current):
    levels = find_levels(current) if current else load_index()[:25]
    return [app_commands.Choice(name=f"{lv['name']} — {lv['list']} #{lv['position']}"[:100], value=lv["id"])
            for lv in levels[:25]]


@bot.tree.command(name="level", description="Info and .gmd file of a level from the list")
@app_commands.describe(query="Name, creator or ID of the level")
@app_commands.autocomplete(query=level_autocomplete)
async def cmd_level(interaction: discord.Interaction, query: str):
    matches = find_levels(query)
    if not matches:
        await interaction.response.send_message(f"No level found for “{query}”.", ephemeral=True)
        return
    if len(matches) > 1 and not any(lv["id"] == query for lv in matches):
        lines = [f"• **{lv['name']}** by {lv['author']} — {lv['list']} #{lv['position']} (`{lv['id']}`)"
                 for lv in matches[:15]]
        more = f"\n… and {len(matches) - 15} more" if len(matches) > 15 else ""
        await interaction.response.send_message(
            f"{len(matches)} levels match, please be more specific:\n" + "\n".join(lines) + more,
            ephemeral=True)
        return
    lv = next((x for x in matches if x["id"] == query), matches[0])
    st = load_state().get(lv["id"], {})
    history = [strip_versions(short_log_line(line)) for line in load_log() if f"({lv['id']})" in line][-6:]
    deleted = st.get("status") == "deleted"

    embed = discord.Embed(title=lv["name"], url=f"{LIST_BASE_URL}/{lv['list']}?level={lv['id']}",
                          colour=EMBED_COLOUR)
    embed.add_field(name="Creator", value=lv["author"] or "?", inline=True)
    embed.add_field(name="List", value=f"{LIST_NAMES.get(lv['list'], lv['list'])} — #{lv['position']}", inline=True)
    embed.add_field(name="ID", value=lv["id"], inline=True)
    if deleted:   # the status is only shown when it is worth knowing
        embed.add_field(name="Status", value="Deleted from the GD servers (copy kept)", inline=True)
    if not lv["has_videos"]:   # index written before videos were recorded: ask the list site
        await interaction.response.defer(thinking=True)   # the site may take a few seconds
        lv["video"], lv["showcase"] = await asyncio.to_thread(videos_from_site, lv["list"], lv["id"])
    if lv["video"]:
        embed.add_field(name="Verification", value=f"[Watch the verification video]({lv['video']})", inline=False)
    if lv["showcase"]:
        embed.add_field(name="Showcase", value=f"[Watch the showcase video]({lv['showcase']})", inline=False)
    yt = youtube_id(lv["video"]) or youtube_id(lv["showcase"])
    if yt:   # the video's thumbnail, inside the embed (bots cannot put a video player in an embed)
        embed.set_image(url=f"https://i.ytimg.com/vi/{yt}/hqdefault.jpg")
    else:
        embed.set_thumbnail(url=f"https://levelthumbs.prevter.me/thumbnail/{lv['id']}")
    if history:
        embed.add_field(name="Latest changes", value="\n".join(history)[:1024], inline=False)

    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    path = files_dir() / lv["file"] if lv["file"] and not lv["file"].startswith("(") else None
    if path and path.is_file() and path.stat().st_size <= UPLOAD_MAX:
        await send(embed=embed, file=discord.File(path, filename=path.name), view=level_view(lv["id"]))
    else:
        note = "_(.gmd file too large for Discord, see Nextcloud)_" if path and path.is_file() else None
        await send(content=note, embed=embed, view=level_view(lv["id"]))


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


_SITE_CACHE = {}   # list -> (time, {level id: video url})


def clean_url(url):
    """Cleans up a video URL typed by hand on the site; '' if it is not a URL."""
    url = (url or "").strip() if isinstance(url, str) else ""
    if url.startswith("ttp"):
        url = "h" + url
    if re.match(r"^(www\.|m\.)?(youtube\.com|youtu\.be)/", url):
        url = "https://" + url
    url = url.replace("://www.youtu.be/", "://youtu.be/")
    return url if re.match(r"^https?://\S+$", url) else ""


def videos_from_site(lst, level_id):
    """(verification, showcase) video URLs of a level, read from the list site (cached 1 hour)."""
    cached = _SITE_CACHE.get(lst)
    if not cached or time.time() - cached[0] > 3600:
        videos = {}
        try:
            req = urllib.request.Request(f"{LIST_BASE_URL}/{lst}", headers={"User-Agent": "tsl-bot"})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
            m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            data = json.loads(m.group(1)) if m else []
            # Nuxt payload: objects reference their values by index in the top-level array
            for obj in data:
                if isinstance(obj, dict) and "verification" in obj and "id" in obj:
                    videos[str(data[obj["id"]])] = (clean_url(data[obj["verification"]]),
                                                    clean_url(data[obj["showcase"]]) if "showcase" in obj else "")
        except Exception:
            log.exception("could not read the videos from %s/%s", LIST_BASE_URL, lst)
        _SITE_CACHE[lst] = (time.time(), videos)
        cached = _SITE_CACHE[lst]
    return cached[1].get(str(level_id), ("", ""))


def level_diffs(level_id):
    """Object diff files of a level (history/<name> (<id>)/<date>_vX_to_vY_diff.txt), newest first."""
    paths = []
    for hist in (files_dir() / "history", files_dir() / "historique"):
        if hist.is_dir():
            paths += [p for d in hist.iterdir() if d.is_dir() and d.name.endswith(f"({level_id})")
                      for p in d.glob("*_diff.txt")]
    return sorted(paths, key=lambda p: (p.name[:10], p.stat().st_mtime), reverse=True)


def parse_diff(path):
    """-> (summary lines, {'ADDED': [...], 'REMOVED': [...], 'MOVED': [...]})"""
    summary, sections, current = [], {}, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(SUMMARY|ADDED|REMOVED|MOVED|SETTINGS)\b", line)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current == "SUMMARY":
            if line.strip():
                summary.append(line.strip())
            else:
                current = None
        elif current and line.startswith("  "):
            sections[current].append(line.strip())
        elif current is None and not summary and re.match(r"^\+\d+ / -\d+ objects", line):
            summary.append(line.strip())   # diff files written before the SUMMARY section existed
    return summary, sections


DIFF_TOOL = Path(setting("DIFF_TOOL", str(Path(__file__).with_name("level_diff.sh"))))


def latest_history_gmd(level_id):
    """Most recent previous version of a level kept in history/, or None."""
    paths = []
    for hist in (files_dir() / "history", files_dir() / "historique"):
        if hist.is_dir():
            paths += [p for d in hist.iterdir() if d.is_dir() and d.name.endswith(f"({level_id})")
                      for p in d.glob("*.gmd")]
    return max(paths, key=lambda p: (p.name[:10], p.stat().st_mtime)) if paths else None


def compute_diff(lv, old_gmd, current):
    """Runs level_diff.sh between a previous version and the current file; returns the detail file."""
    version = load_state().get(lv["id"], {}).get("version") or "current"
    out = old_gmd.with_name(f"{old_gmd.stem}_to_v{version}_diff.txt")
    env = dict(os.environ, OLD_LABEL=f"{lv['name']} ({lv['id']}) {old_gmd.stem}",
               NEW_LABEL=f"{lv['name']} ({lv['id']}) current version")
    try:
        subprocess.run([str(DIFF_TOOL), str(old_gmd), str(current), str(out)], env=env, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.SubprocessError):
        log.exception("level_diff.sh failed for %s", lv["id"])
        return None
    return out if out.is_file() else None


def short_object(line):
    """'+ id 1 at x=54077 y=255   [1,1,...]' -> '+ id 1 at x=54077 y=255'"""
    return re.sub(r"\s+\[.*\]$", "", line)


@bot.tree.command(name="diff", description="What changed in a level the last time it was modified")
@app_commands.describe(query="Name, creator or ID of the level")
@app_commands.autocomplete(query=level_autocomplete)
async def cmd_diff(interaction: discord.Interaction, query: str):
    # answer Discord right away ("thinking…"): reading or computing the diff may take a moment
    await interaction.response.defer(thinking=True)
    send = interaction.followup.send
    matches = find_levels(query)
    if not matches:
        await send(f"No level found for “{query}”.")
        return
    lv = next((x for x in matches if x["id"] == query), matches[0])
    if len(matches) > 1 and lv["id"] != query and lv["name"].lower() != query.strip().lower():
        lines = [f"• **{x['name']}** by {x['author']} (`{x['id']}`)" for x in matches[:15]]
        await send(f"{len(matches)} levels match, please be more specific:\n" + "\n".join(lines))
        return
    diffs = level_diffs(lv["id"])
    url = f"{LIST_BASE_URL}/{lv['list']}?level={lv['id']}"
    if not diffs:
        # No comparison recorded (level modified before level_diff.sh existed): compare the
        # previous version kept in history/ with the current file, now.
        old_gmd = latest_history_gmd(lv["id"])
        current = files_dir() / lv["file"] if lv["file"] and not lv["file"].startswith("(") else None
        if old_gmd and current and current.is_file() and DIFF_TOOL.is_file():
            made = await asyncio.to_thread(compute_diff, lv, old_gmd, current)
            if made:
                diffs = [made]
    if not diffs:
        await send(f"No recorded changes for **[{lv['name']}](<{url}>)** yet: a comparison is made the next time "
                   "the level is modified.")
        return

    view = DiffView(lv, diffs, interaction.user.id)
    embed, files = view.render()
    kwargs = {"embed": embed, "view": view} if len(diffs) > 1 else {"embed": embed}
    kwargs["files"] = files
    if not any(f.filename.endswith(".txt") for f in files):
        kwargs["content"] = "_(full list too large for Discord, see Nextcloud)_"
    view.message = await send(wait=True, **kwargs)


def num(n):
    """1234567 -> '1,234,567'"""
    try:
        return f"{int(str(n).replace(',', '')):,}"
    except ValueError:
        return str(n)


def parse_summary(summary):
    """Splits the text summary of level_diff.sh into its parts (None if it has another shape)."""
    if not summary:
        return None
    m = re.match(r"^\*\*\+(\d+) / -(\d+) objects(?:, (\d+) moved)? \(([\d,]+) -> ([\d,]+)\)\*\*(?: — (.*))?$",
                 summary[0])
    if not m:
        return None
    info = {"added": m[1], "removed": m[2], "moved": m[3] or "0", "before": m[4], "after": m[5],
            "where": m[6] or "", "types": {}, "settings": ""}
    for line in summary[1:]:
        key, _, value = line.partition(": ")
        if key in ("Added", "Removed", "Moved"):
            info["types"][key] = [x.strip() for x in value.split(", ") if x.strip()]
        elif key == "Settings changed":
            info["settings"] = value
    return info


_OBJECT_NAMES = None


def object_name(object_id):
    """Readable name of an object ID from object_names.tsv (None if unknown)."""
    global _OBJECT_NAMES
    if _OBJECT_NAMES is None:
        _OBJECT_NAMES = {}
        try:
            for line in DIFF_TOOL.with_name("object_names.tsv").read_text(encoding="utf-8").splitlines():
                oid, _, name = line.partition("\t")
                if oid.isdigit() and name:
                    _OBJECT_NAMES[oid] = name
        except OSError:
            pass
    return _OBJECT_NAMES.get(str(object_id))


def type_lines(items):
    """['9303 object #516', '4334 blocks'] -> '9,303 × 3D outline (#516)\n4,334 × blocks'"""
    out = []
    for item in items:
        count, _, name = item.partition(" ")
        known = re.fullmatch(r"object #(\d+)", name)
        if known and object_name(known.group(1)):   # diffs saved before the names table existed
            name = f"{object_name(known.group(1))} (#{known.group(1)})"
        out.append(f"{num(count)} × {name}" if count.isdigit() else item)
    return "\n".join(out)[:1024] or "—"


SPRITES_DIR = Path(setting("SPRITES_DIR", str(DATA_DIR / "objects")))   # made by extract_sprites.py
_IMAGE_CACHE = {}


def _font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def objects_image(path, sections):
    """PNG (bytes) showing the most added / removed / moved objects with their count, or None."""
    if Image is None or not SPRITES_DIR.is_dir():
        return None
    key = (str(path), path.stat().st_mtime)
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]
    rows = []
    for section, title in (("ADDED", "Added"), ("REMOVED", "Removed"), ("MOVED", "Moved")):
        counts = {}
        for line in sections.get(section, []):
            m = re.search(r"\bid (\d+)\b", line)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], int(kv[0])))[:6]
        if top:
            rows.append((title, top))
    if not any((SPRITES_DIR / f"{oid}.png").exists() for _, top in rows for oid, _ in top):
        _IMAGE_CACHE[key] = None
        return None
    cell_w, icon, pad, title_h = 130, 72, 14, 30
    row_h = title_h + icon + 58
    img = Image.new("RGBA", (pad * 2 + cell_w * 6, pad + row_h * len(rows)), (43, 45, 49, 255))
    draw = ImageDraw.Draw(img)
    f_title, f_count, f_name = _font(20), _font(17), _font(13)
    y = pad
    for title, top in rows:
        draw.text((pad, y), title, font=f_title, fill=(255, 255, 255))
        for i, (oid, count) in enumerate(top):
            x = pad + i * cell_w
            box = (x + (cell_w - icon) // 2, y + title_h)
            draw.rounded_rectangle((box[0] - 4, box[1] - 4, box[0] + icon + 4, box[1] + icon + 4),
                                   radius=8, fill=(30, 31, 34))
            sprite = SPRITES_DIR / f"{oid}.png"
            if sprite.exists():
                try:
                    pic = Image.open(sprite).convert("RGBA")
                    pic.thumbnail((icon, icon))
                    img.alpha_composite(pic, (box[0] + (icon - pic.width) // 2, box[1] + (icon - pic.height) // 2))
                except OSError:
                    pass
            else:
                draw.text((box[0] + icon // 2, box[1] + icon // 2), "?", font=f_title, fill=(150, 150, 150),
                          anchor="mm")
            cx, ty = x + cell_w // 2, box[1] + icon + 8
            draw.text((cx, ty), f"× {count:,}", font=f_count, fill=(255, 255, 255), anchor="mt")
            name = object_name(oid) or "object"
            name = name if len(name) <= 18 else name[:17] + "…"
            draw.text((cx, ty + 22), name, font=f_name, fill=(200, 200, 200), anchor="mt")
            draw.text((cx, ty + 38), f"#{oid}", font=f_name, fill=(140, 140, 140), anchor="mt")
        y += row_h
    buf = io.BytesIO()
    img.save(buf, "PNG")
    if len(_IMAGE_CACHE) > 50:
        _IMAGE_CACHE.clear()
    _IMAGE_CACHE[key] = buf.getvalue()
    return _IMAGE_CACHE[key]


def diff_embed(lv, path, index, total):
    """Embed and attachments for one diff file of a level (index 0 = newest)."""
    summary, sections = parse_diff(path)
    url = f"{LIST_BASE_URL}/{lv['list']}?level={lv['id']}"
    info = parse_summary(summary)
    embed = discord.Embed(title=f"Changes in {lv['name']}", url=url, colour=EMBED_COLOUR,
                          description=None if info else ("\n".join(summary) or "No details available."))
    if info:
        embed.add_field(name="Objects", value=f"{num(info['before'])} → {num(info['after'])}", inline=True)
        embed.add_field(name="Added", value=f"+{num(info['added'])}", inline=True)
        embed.add_field(name="Removed", value=f"−{num(info['removed'])}", inline=True)
        if info["moved"] != "0":
            embed.add_field(name="Moved", value=num(info["moved"]), inline=True)
        if info["where"]:
            where = info["where"].replace(" of the level", "")
            embed.add_field(name="Where in the level", value=where[:1].upper() + where[1:], inline=False)
        for key in ("Added", "Removed", "Moved"):
            if info["types"].get(key):
                embed.add_field(name=f"{key} objects", value=type_lines(info["types"][key]), inline=True)
        if info["settings"]:
            embed.add_field(name="Settings changed", value=info["settings"][:1].upper() + info["settings"][1:],
                            inline=False)
    embed.add_field(name="Changed on", value=discord_time(path.name[:10], "D"), inline=True)
    embed.add_field(name="Creator", value=lv["author"] or "?", inline=True)
    preview = []
    for key in ("ADDED", "REMOVED", "MOVED"):
        preview += [short_object(x) for x in sections.get(key, [])[:4]]
    if preview:
        embed.add_field(name="First changed objects", value="```\n" + "\n".join(preview)[:990] + "\n```",
                        inline=False)
    where = "" if total == 1 else (" (newest)" if index == 0 else (" (oldest)" if index == total - 1 else ""))
    embed.set_footer(text=f"Change {index + 1}/{total}{where} · "
                          "the attached file lists every added, removed and moved object")
    files = []
    picture = objects_image(path, sections)
    if picture:
        files.append(discord.File(io.BytesIO(picture), filename="objects.png"))
        embed.set_image(url="attachment://objects.png")
    if path.stat().st_size <= UPLOAD_MAX:
        filename = re.sub(r'[\\/:*?"<>|]', "_", f"{lv['name']} - changes {path.name[:10]}.txt")
        files.append(discord.File(path, filename=filename))
    return embed, files


class DiffView(discord.ui.View):
    """Buttons to browse the successive changes of a level (only for the user who ran /diff)."""

    def __init__(self, lv, diffs, owner_id):
        super().__init__(timeout=300)
        self.lv, self.diffs, self.owner_id = lv, diffs, owner_id
        self.index = 0   # 0 = newest
        self.message = None
        self.refresh_buttons()

    def render(self):
        return diff_embed(self.lv, self.diffs[self.index], self.index, len(self.diffs))

    def refresh_buttons(self):
        self.newest.disabled = self.newer.disabled = self.index <= 0
        self.older.disabled = self.oldest.disabled = self.index >= len(self.diffs) - 1

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("These buttons belong to whoever used /diff — "
                                                "run /diff yourself to browse.", ephemeral=True)
        return False

    async def show(self, interaction, index):
        self.index = min(max(index, 0), len(self.diffs) - 1)
        self.refresh_buttons()
        embed, files = self.render()
        too_big = not any(f.filename.endswith(".txt") for f in files)
        await interaction.response.edit_message(
            embed=embed, view=self, attachments=files,
            content="_(full list too large for Discord, see Nextcloud)_" if too_big else None)

    @discord.ui.button(label="Newest", style=discord.ButtonStyle.secondary)
    async def newest(self, interaction, _button):
        await self.show(interaction, 0)

    @discord.ui.button(label="Newer", style=discord.ButtonStyle.primary)
    async def newer(self, interaction, _button):
        await self.show(interaction, self.index - 1)

    @discord.ui.button(label="Older", style=discord.ButtonStyle.primary)
    async def older(self, interaction, _button):
        await self.show(interaction, self.index + 1)

    @discord.ui.button(label="Oldest", style=discord.ButtonStyle.secondary)
    async def oldest(self, interaction, _button):
        await self.show(interaction, len(self.diffs) - 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PagedView(discord.ui.View):
    """An embed split in pages, with First / Previous / Next / Last buttons.
    Only the user who ran the command can use the buttons; they are disabled after 5 minutes."""

    def __init__(self, title, pages, owner_id, command, footer="", url=None):
        super().__init__(timeout=300)
        self.title, self.pages, self.owner_id, self.command = title, pages or ["Nothing to show."], owner_id, command
        self.footer, self.url = footer, url
        self.page = 1
        self.message = None
        self.refresh_buttons()

    def embed(self):
        embed = discord.Embed(title=self.title, url=self.url, description=self.pages[self.page - 1],
                              colour=EMBED_COLOUR)
        footer = f"Page {self.page}/{len(self.pages)}"
        embed.set_footer(text=f"{footer} · {self.footer}" if self.footer else footer)
        return embed

    def refresh_buttons(self):
        self.first.disabled = self.previous.disabled = self.page <= 1
        self.next.disabled = self.last.disabled = self.page >= len(self.pages)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(f"These buttons belong to whoever used {self.command} — "
                                                f"run {self.command} yourself to browse.", ephemeral=True)
        return False

    async def show(self, interaction, page):
        self.page = min(max(page, 1), len(self.pages))
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, _button):
        await self.show(interaction, 1)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous(self, interaction, _button):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction, _button):
        await self.show(interaction, self.page + 1)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, _button):
        await self.show(interaction, len(self.pages))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def send(self, interaction, start_page=1):
        self.page = min(max(start_page, 1), len(self.pages))
        self.refresh_buttons()
        await interaction.response.send_message(embed=self.embed(), view=self,
                                                allowed_mentions=discord.AllowedMentions.none())
        self.message = await interaction.original_response()


def paginate(lines, per_page, max_chars=3800):
    """Groups lines into pages of at most per_page lines and max_chars characters."""
    pages, current = [], []
    for line in lines:
        line = line if len(line) <= max_chars else line[: max_chars - 1] + "…"
        if current and (len(current) >= per_page or len("\n".join(current + [line])) > max_chars):
            pages.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        pages.append("\n".join(current))
    return pages


@bot.tree.command(name="changes", description="What changed in the last days")
@app_commands.describe(days="Number of days (default: 7)", everything="Also include ranking changes")
async def cmd_changes(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7,
                      everything: bool = False):
    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    lines = [line for line in load_log() if line[:10] >= since
             and (everything or any(f"] {k}" in line for k in IMPORTANT))]
    if not lines:
        await interaction.response.send_message(
            "No changes in the last day." if days == 1 else f"No changes in the last {days} days.")
        return
    lines = [strip_versions(short_log_line(x)) for x in reversed(lines)]   # newest first
    period = "the last day" if days == 1 else f"the last {days} days"
    noun = "change" if len(lines) == 1 else "changes"
    view = PagedView(f"Changes in {period}", paginate(lines, 15), interaction.user.id, "/changes",
                     footer=f"{len(lines)} {noun} in {period}")
    await view.send(interaction)


@bot.tree.command(name="list", description="Show the ranking")
@app_commands.describe(name="Which list", page="Page to start on (25 levels per page)")
@app_commands.choices(name=[app_commands.Choice(name=LIST_NAMES.get(x, x), value=x) for x in LISTS])
async def cmd_list(interaction: discord.Interaction, name: app_commands.Choice[str],
                   page: app_commands.Range[int, 1, 100] = 1):
    levels = sorted((lv for lv in load_index() if lv["list"] == name.value), key=lambda x: x["position"])
    lines = [f"`#{lv['position']:>3}` [{lv['name']}]({LIST_BASE_URL}/{lv['list']}?level={lv['id']}) "
             f"— {lv['author']}" for lv in levels]
    view = PagedView(name.name, paginate(lines, 25) or ["This list is empty."], interaction.user.id, "/list",
                     footer=f"{len(levels)} levels", url=f"{LIST_BASE_URL}/{name.value}")
    await view.send(interaction, page)


@bot.tree.command(name="status", description="Result of the last tsl-sync run")
async def cmd_status(interaction: discord.Interaction):
    step = current_progress()
    when, counts = last_run_counts()
    state = load_state()
    index = load_index()

    embed = discord.Embed(title="Server & Tracking status", colour=EMBED_COLOUR)
    embed.add_field(name="Current run", value=step or "✅ Idle", inline=False)
    if when:
        embed.add_field(name="Last run", value=f"{discord_time(when)} ({discord_time(when, 'R')})", inline=False)
        for key, label in (("new", "New"), ("modified", "Modified"), ("info changed", "Info changed"),
                           ("deleted", "Deleted"), ("unchanged", "Unchanged"), ("failed", "Failed")):
            embed.add_field(name=label, value=str(counts.get(key, 0)), inline=True)
    else:
        embed.add_field(name="Last run", value="No run recorded yet.", inline=False)

    modified = last_run_modified()
    if when:
        if modified:
            lines = [f"[{name}]({LIST_BASE_URL}/{lst}?level={lid}) ({LIST_NAMES.get(lst, lst)})"
                     for lst, lid, name in (row[:3] for row in modified)]
            value = ""
            for i, line in enumerate(lines):
                rest = len(lines) - i
                if len(value) + len(line) + 1 > 1000:
                    value += f"\n… and {rest} more"
                    break
                value += ("\n" if value else "") + line
        elif not (DATA_DIR / "last_run_modified.tsv").exists():
            value = "Shown after the next run"
        else:
            value = "None"
        embed.add_field(name="Modified levels (last run)", value=value, inline=False)

    embed.add_field(name="Tracked levels", inline=False, value="\n".join(
        f"{LIST_NAMES.get(x, x)}: {sum(1 for lv in index if lv['list'] == x)}" for x in LISTS))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="run", description="Start a tracking run now: re-download every level and look for changes (~35 min)")
@app_commands.default_permissions(manage_guild=True)
async def cmd_run(interaction: discord.Interaction):
    res = subprocess.run(["systemctl", "start", "--no-block", "tsl-sync"], capture_output=True, text=True,
                         check=False)
    if res.returncode == 0:
        await interaction.response.send_message(
            "Run started (about 35 min). Changes will be announced here when it is done.")
    else:
        await interaction.response.send_message(f"Could not start the run: {res.stderr.strip()[:500]}",
                                                ephemeral=True)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is missing in /etc/tsl-sync.conf")
    if not CHANNEL_ID:
        log.warning("DISCORD_CHANNEL_ID is missing: automatic announcements are disabled")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
