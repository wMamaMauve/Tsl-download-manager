#!/usr/bin/env python3
"""Discord bot for tsl-sync.

- Posts the changes detected by tsl-sync.sh to a channel
  (files dropped in DATA_DIR/notifications/).
- Slash commands: /level, /changes, /list, /status, /check.

Configuration: the same file as tsl-sync (/etc/tsl-sync.conf or TSL_CONF):
    DISCORD_BOT_TOKEN="..."      bot token (Discord Developer Portal)
    DISCORD_CHANNEL_ID="..."     channel where announcements are posted
    DISCORD_GUILD_ID="..."       (optional) Discord server, so commands show up immediately
"""

import asyncio
import datetime as dt
import logging
import os
import re
import shlex
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("tsl-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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


def last_run_line():
    path = first_existing(DATA_DIR / "last_run.txt", DATA_DIR / "dernier_passage.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def presence_text():
    """Text shown in the bot's Discord status."""
    step = current_progress()
    if step:
        return step
    last = last_run_line()
    m = re.match(r"\d{4}-(\d\d-\d\d) (\d\d:\d\d) — (.*)", last)
    if not m:
        return "Waiting for the first run"
    counts = dict((k.strip(), int(v)) for v, k in re.findall(r"(\d+) ([a-z ]+?)(?:,|$)", m.group(3)))
    parts = [f"{counts[k]} {k}" for k in ("modified", "info changed", "deleted", "new") if counts.get(k)]
    return f"✅ Last run {m.group(1)} {m.group(2)} · {', '.join(parts) if parts else 'no changes'}"


def short_log_line(line):
    """'2026-11-10 04:21:07 [tsl] MODIFIED: …' -> '`11-10 04:21` [tsl] MODIFIED: …'"""
    m = re.match(r"\d{4}-(\d\d-\d\d) (\d\d:\d\d):\d\d (.*)", line)
    return f"`{m.group(1)} {m.group(2)}` {m.group(3)}" if m else line


# --------------------------------------------------------------------------- bot
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
            await asyncio.sleep(30)

    async def update_presence(self):
        """Shows the run progress / last run in the bot's Discord status."""
        text = presence_text()[:128]
        if text != self.presence:
            await self.change_presence(activity=discord.CustomActivity(name=text))
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


def announcement_embeds(lines):
    """Turns a notification file (title line, change lines, '_summary_' line) into embeds."""
    title, footer, body = "TSL", None, []
    for line in lines:
        line = OLD_EMOJIS.sub("", line.strip())
        if not line:
            continue
        if "run of" in line and line.replace("*", "").startswith("TSL"):
            title = line.replace("*", "")
        elif line.startswith("_") and line.endswith("_") and len(line) > 2:
            footer = line.strip("_")
        else:
            body.append(re.sub(r"\s*\(v\d+\s*→\s*v\d+\)", "", line))   # old "(v2 → v3)"
    if not body:
        body = ["✅ No changes."]
    alarming = any("**Deleted**" in x for x in body)
    colour = 0xE74C3C if alarming else (0x2ECC71 if body == ["✅ No changes."] else 0x3498DB)

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
        embed = discord.Embed(title=name[:256], description=chunk, colour=colour)
        if footer and i == len(chunks) - 1:
            embed.set_footer(text=footer[:2048])
        embeds.append(embed)
    return embeds


bot = TSLBot()


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
                          colour=0xE74C3C if deleted else 0x3498DB)
    embed.add_field(name="Creator", value=lv["author"] or "?", inline=True)
    embed.add_field(name="List", value=f"{LIST_NAMES.get(lv['list'], lv['list'])} — #{lv['position']}", inline=True)
    embed.add_field(name="ID", value=lv["id"], inline=True)
    embed.add_field(name="Status", value="Deleted from the GD servers (copy kept)" if deleted else "✅ online",
                    inline=True)
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
        await send(embed=embed, file=discord.File(path, filename=path.name))
    else:
        note = "_(.gmd file too large for Discord, see Nextcloud)_" if path and path.is_file() else None
        await send(content=note, embed=embed)


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
    matches = find_levels(query)
    if not matches:
        await interaction.response.send_message(f"No level found for “{query}”.", ephemeral=True)
        return
    lv = next((x for x in matches if x["id"] == query), matches[0])
    if len(matches) > 1 and lv["id"] != query and lv["name"].lower() != query.strip().lower():
        lines = [f"• **{x['name']}** by {x['author']} (`{x['id']}`)" for x in matches[:15]]
        await interaction.response.send_message(
            f"{len(matches)} levels match, please be more specific:\n" + "\n".join(lines), ephemeral=True)
        return
    diffs = level_diffs(lv["id"])
    url = f"{LIST_BASE_URL}/{lv['list']}?level={lv['id']}"
    if not diffs:
        # No comparison recorded (level modified before level_diff.sh existed): compare the
        # previous version kept in history/ with the current file, now.
        old_gmd = latest_history_gmd(lv["id"])
        current = files_dir() / lv["file"] if lv["file"] and not lv["file"].startswith("(") else None
        if old_gmd and current and current.is_file() and DIFF_TOOL.is_file():
            await interaction.response.defer(thinking=True)
            made = await asyncio.to_thread(compute_diff, lv, old_gmd, current)
            if made:
                diffs = [made]
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    if not diffs:
        await send(f"No recorded changes for **[{lv['name']}](<{url}>)** yet: a comparison is made the next time "
                   "the level is modified.", ephemeral=True)
        return

    latest = diffs[0]
    summary, sections = parse_diff(latest)
    embed = discord.Embed(title=f"Changes in {lv['name']}", url=url, colour=0x3498DB,
                          description="\n".join(summary) or "No details available.")
    embed.add_field(name="Changed on", value=latest.name[:10], inline=True)
    embed.add_field(name="Creator", value=lv["author"] or "?", inline=True)
    preview = []
    for key in ("ADDED", "REMOVED", "MOVED"):
        preview += [short_object(x) for x in sections.get(key, [])[:4]]
    if preview:
        embed.add_field(name="First changed objects", value="```\n" + "\n".join(preview)[:990] + "\n```",
                        inline=False)
    if sections.get("SETTINGS"):
        embed.add_field(name="Settings", value="```\n" + "\n".join(sections["SETTINGS"][:8])[:990] + "\n```",
                        inline=False)
    if len(diffs) > 1:
        older = ", ".join(sorted({p.name[:10] for p in diffs[1:]}, reverse=True)[:10])
        embed.add_field(name="Earlier changes", value=older, inline=False)
    embed.set_footer(text="The attached file lists every added, removed and moved object.")

    if latest.stat().st_size <= UPLOAD_MAX:
        filename = re.sub(r'[\\/:*?"<>|]', "_", f"{lv['name']} - changes {latest.name[:10]}.txt")
        await send(embed=embed, file=discord.File(latest, filename=filename))
    else:
        await send(content="_(full list too large for Discord, see Nextcloud)_", embed=embed)


@bot.tree.command(name="changes", description="What changed in the last days")
@app_commands.describe(days="Number of days (default: 7)", everything="Also include ranking changes")
async def cmd_changes(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7,
                      everything: bool = False):
    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    lines = [line for line in load_log() if line[:10] >= since
             and (everything or any(f"] {k}" in line for k in IMPORTANT))]
    if not lines:
        await interaction.response.send_message(f"No changes in the last {days} days.")
        return
    msgs = split_messages([short_log_line(x) for x in lines[-200:]],
                          header=f"**Changes in the last {days} days** ({len(lines)})")
    await interaction.response.send_message(msgs[0], allowed_mentions=discord.AllowedMentions.none())
    for msg in msgs[1:5]:
        await interaction.followup.send(msg, allowed_mentions=discord.AllowedMentions.none())
    if len(msgs) > 5:
        await interaction.followup.send("… list truncated, use a smaller number of days.")


PAGE_SIZE = 25


class ListView(discord.ui.View):
    """One page of a list, with buttons to change page (only for the user who asked)."""

    def __init__(self, list_id, list_name, page, owner_id):
        super().__init__(timeout=300)
        self.list_id, self.list_name, self.owner_id = list_id, list_name, owner_id
        self.levels = sorted((lv for lv in load_index() if lv["list"] == list_id), key=lambda x: x["position"])
        self.pages = max(1, (len(self.levels) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(max(page, 1), self.pages)
        self.message = None
        self.refresh_buttons()

    def embed(self):
        chunk = self.levels[(self.page - 1) * PAGE_SIZE: self.page * PAGE_SIZE]
        lines = [f"`#{lv['position']:>3}` [{lv['name']}]({LIST_BASE_URL}/{lv['list']}?level={lv['id']}) "
                 f"— {lv['author']}" for lv in chunk]
        embed = discord.Embed(title=self.list_name, url=f"{LIST_BASE_URL}/{self.list_id}",
                              description="\n".join(lines) or "This list is empty.", colour=0x3498DB)
        embed.set_footer(text=f"Page {self.page}/{self.pages} · {len(self.levels)} levels")
        return embed

    def refresh_buttons(self):
        self.first.disabled = self.previous.disabled = self.page <= 1
        self.next.disabled = self.last.disabled = self.page >= self.pages

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("These buttons belong to whoever used /list — "
                                                "run /list yourself to browse.", ephemeral=True)
        return False

    async def show(self, interaction, page):
        self.page = min(max(page, 1), self.pages)
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
        await self.show(interaction, self.pages)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="list", description="Show the ranking")
@app_commands.describe(name="Which list", page="Page to start on (25 levels per page)")
@app_commands.choices(name=[app_commands.Choice(name=LIST_NAMES.get(x, x), value=x) for x in LISTS])
async def cmd_list(interaction: discord.Interaction, name: app_commands.Choice[str],
                   page: app_commands.Range[int, 1, 100] = 1):
    view = ListView(name.value, name.name, page, interaction.user.id)
    await interaction.response.send_message(embed=view.embed(), view=view,
                                            allowed_mentions=discord.AllowedMentions.none())
    view.message = await interaction.original_response()


def last_run_counts():
    """('2026-11-10 04:52', {'new': 0, 'modified': 1, ...}) from last_run.txt, or (None, {})."""
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d) — (.*)", last_run_line())
    if not m:
        return None, {}
    return m.group(1), {k.strip(): int(v) for v, k in re.findall(r"(\d+) ([a-z ]+?)(?:,|$)", m.group(2))}


def last_run_modified():
    return [row for row in read_tsv(DATA_DIR / "last_run_modified.tsv") if len(row) >= 3]


@bot.tree.command(name="status", description="Result of the last tsl-sync run")
async def cmd_status(interaction: discord.Interaction):
    step = current_progress()
    when, counts = last_run_counts()
    state = load_state()
    index = load_index()

    embed = discord.Embed(title="TSL status", colour=0xF1C40F if step else 0x2ECC71)
    embed.add_field(name="Current run", value=step or "✅ Idle", inline=False)
    if when:
        embed.add_field(name="Last run", value=when, inline=False)
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
    embed.add_field(name="Deleted from the GD servers", inline=False,
                    value=str(sum(1 for s in state.values() if s["status"] == "deleted")))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="check", description="Start a check run now")
@app_commands.default_permissions(manage_guild=True)
async def cmd_check(interaction: discord.Interaction):
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
