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
import subprocess
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
            levels.append({"list": lst, "position": int(pos) if pos.isdigit() else 0,
                           "id": lid, "name": name, "author": author, "file": rel, "video": video})
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
            for msg in split_messages(lines):
                await channel.send(msg, allowed_mentions=discord.AllowedMentions.none(),
                                   suppress_embeds=True)
                await asyncio.sleep(1)
            path.unlink()


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
    if lv["video"]:
        embed.add_field(name="Verification", value=f"[Watch the verification video]({lv['video']})", inline=False)
    else:
        embed.set_thumbnail(url=f"https://levelthumbs.prevter.me/thumbnail/{lv['id']}")
    if history:
        embed.add_field(name="Latest changes", value="\n".join(history)[:1024], inline=False)

    path = files_dir() / lv["file"] if lv["file"] and not lv["file"].startswith("(") else None
    if path and path.is_file() and path.stat().st_size <= UPLOAD_MAX:
        await interaction.response.send_message(embed=embed, file=discord.File(path, filename=path.name))
    else:
        note = "_(.gmd file too large for Discord, see Nextcloud)_" if path and path.is_file() else None
        await interaction.response.send_message(content=note, embed=embed)
    # The video is sent as a separate message containing only its URL: Discord always
    # shows the YouTube player for such a message (it may not when the URL sits next to
    # a bot embed).
    if lv["video"]:
        await interaction.followup.send(lv["video"])


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


@bot.tree.command(name="list", description="Show the ranking")
@app_commands.describe(name="Which list", page="Page (25 levels per page)")
@app_commands.choices(name=[app_commands.Choice(name=LIST_NAMES.get(x, x), value=x) for x in LISTS])
async def cmd_list(interaction: discord.Interaction, name: app_commands.Choice[str],
                   page: app_commands.Range[int, 1, 100] = 1):
    levels = sorted((lv for lv in load_index() if lv["list"] == name.value), key=lambda x: x["position"])
    chunk = levels[(page - 1) * 25: page * 25]
    if not chunk:
        await interaction.response.send_message("Empty page.", ephemeral=True)
        return
    pages = (len(levels) + 24) // 25
    lines = [f"`#{lv['position']:>3}` **{lv['name']}** — {lv['author']}" for lv in chunk]
    await interaction.response.send_message(
        f"**{name.name}** — page {page}/{pages}\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="status", description="Result of the last tsl-sync run")
async def cmd_status(interaction: discord.Interaction):
    last = last_run_line() or "No run recorded yet."
    step = current_progress()
    state = load_state()
    deleted = sum(1 for s in state.values() if s["status"] == "deleted")
    index = load_index()
    counts = ", ".join(f"{LIST_NAMES.get(x, x)}: {sum(1 for lv in index if lv['list'] == x)}" for x in LISTS)
    await interaction.response.send_message(
        f"**Run in progress**: {step if step else 'no'}\n"
        f"**Last run**: {last}\n**Tracked levels**: {counts}\n"
        f"**Deleted from the GD servers**: {deleted}")


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
