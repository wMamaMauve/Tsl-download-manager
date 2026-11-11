#!/usr/bin/env python3
"""gacha_bot.py — Discord bot showing which Extreme Demon Gacha (demongacha.com) cards the members
of a clan (TSL by default) own.

Read-only: once a day it logs in with the account given in the config (made for this, with the
site owner's agreement), reads the clan's member list and every member's collection, and keeps them
in a cache file. It never opens packs, trades, duels or changes anything.

Commands: /cards /whohas /missing /clan /gachastatus /gachasync /help
Config: /etc/gacha-bot.conf (see gacha-bot.conf.example), or the GACHA_CONF variable.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import shlex
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gacha-bot")


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


CONF = load_conf(os.environ.get("GACHA_CONF", "/etc/gacha-bot.conf"))


def setting(name, default=""):
    return os.environ.get(name) or CONF.get(name) or default


TOKEN = setting("DISCORD_BOT_TOKEN")
GUILD_ID = int(setting("DISCORD_GUILD_ID", "0") or 0)
SITE = setting("GACHA_SITE", "https://demongacha.com").rstrip("/")
GACHA_USER = setting("GACHA_USERNAME")
GACHA_PASS = setting("GACHA_PASSWORD")
CLAN = setting("GACHA_CLAN", "TSL")
DATA_DIR = Path(setting("DATA_DIR", "/var/lib/gacha-bot"))
SYNC_HOUR = int(setting("SYNC_HOUR", "5") or 5)          # daily refresh, server local time
REQUEST_DELAY = float(setting("REQUEST_DELAY", "1") or 1)  # seconds between two requests to the site
CACHE = DATA_DIR / "cache.json"
COLOUR = 0x287DFF
TIERS = ["Apex", "Mythic", "Legendary", "Epic", "Rare", "Uncommon", "Common", "Limited"]
TIER_ICON = {"Apex": "🟥", "Mythic": "🟪", "Legendary": "🟨", "Epic": "🟦", "Rare": "🟩",
             "Uncommon": "⬜", "Common": "⬛", "Limited": "🔶"}
USER_AGENT = "TSL clan Discord bot (read-only, once a day)"


# --------------------------------------------------------------------------- the site
class SiteError(Exception):
    pass


class Gacha:
    """Read-only client for demongacha.com: logs in with the configured account (cookie 'sid')
    and logs in again by itself when the session has expired."""

    def __init__(self):
        self.session = None

    async def open(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT},
                                                 cookie_jar=aiohttp.CookieJar(unsafe=True),   # keeps the 'sid' cookie
                                                 timeout=aiohttp.ClientTimeout(total=60))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def login(self):
        if not GACHA_USER or not GACHA_PASS:
            raise SiteError("GACHA_USERNAME / GACHA_PASSWORD are missing in the config")
        await self.open()
        async with self.session.post(f"{SITE}/api/login", json={"username": GACHA_USER, "password": GACHA_PASS}) as r:
            if r.status != 200:
                raise SiteError(f"login refused (HTTP {r.status}): {(await r.text())[:200]}")
        log.info("logged in to %s as %s", SITE, GACHA_USER)

    async def get(self, path, auth=True, _retry=True):
        await self.open()
        await asyncio.sleep(REQUEST_DELAY)
        async with self.session.get(f"{SITE}{path}") as r:
            if r.status == 401 and auth and _retry:
                await self.login()
                return await self.get(path, auth, _retry=False)
            if r.status == 429 and _retry:
                await asyncio.sleep(30)
                return await self.get(path, auth, _retry=False)
            if r.status != 200:
                raise SiteError(f"{path}: HTTP {r.status} {(await r.text())[:200]}")
            return await r.json(content_type=None)

    async def pages(self, path, key="items", page_size=200):
        """Every item of a paged list (?page=…&pageSize=…)."""
        items, page, pages = [], 1, 1
        sep = "&" if "?" in path else "?"
        while page <= pages:
            d = await self.get(f"{path}{sep}page={page}&pageSize={page_size}")
            items += d.get(key) or []
            pages = int(d.get("pages") or 1)
            page += 1
        return items


def card_counts(c):
    """(copies, holo copies) of one collection entry, whatever shape the site sends."""
    count = int(c.get("count") or c.get("total") or 1)
    holo = c.get("holoCount")
    if holo is None:
        holo = c.get("totalHolo")
    if holo is None:
        holo = count if c.get("holo") else 0
    return count, int(holo)


async def sync(gacha):
    """Reads the levels, the clan and every member's collection; writes the cache. Returns a summary."""
    t0 = time.time()
    levels = await gacha.pages("/api/levels?sort=rank_asc")
    clan = await gacha.get(f"/api/clans/{quote(CLAN)}", auth=False)
    members = clan.get("members") or []
    collections, failed = {}, []
    await gacha.login()
    for m in members:
        name = m["username"]
        try:
            items = await gacha.pages(f"/api/collection?user={quote(name)}&sort=rank_asc")
        except SiteError as e:
            log.warning("collection of %s: %s", name, e)
            failed.append(name)
            continue
        cards = {}
        for c in items:
            lid = c.get("id") or c.get("levelId")
            if not lid:
                continue
            count, holo = card_counts(c)
            prev = cards.get(lid, {"count": 0, "holo": 0})   # one level may come once per variant
            cards[lid] = {"count": prev["count"] + count, "holo": prev["holo"] + holo}
        collections[name] = cards
    data = {
        "updated": time.time(),
        "clan": {k: clan.get(k) for k in ("name", "tag", "color", "score", "rank", "max")},
        "members": [{k: m.get(k) for k in ("username", "clanRole", "score", "joinedAt")} for m in members],
        "levels": {lv["id"]: {k: lv.get(k) for k in ("rank", "name", "tier", "creator", "verifier", "thumb",
                                                     "video", "value", "points", "gdId")} for lv in levels},
        "collections": collections,
        "failed": failed,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE)
    summary = (f"{len(collections)}/{len(members)} collections, {len(levels)} cards, "
               f"{time.time() - t0:.0f} s" + (f", failed: {', '.join(failed)}" if failed else ""))
    log.info("sync done: %s", summary)
    return summary


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- Discord helpers
class PagedView(discord.ui.View):
    """An embed split in pages, with First / Previous / Next / Last buttons (only for the user
    who ran the command; disabled after 5 minutes)."""

    def __init__(self, title, pages, owner_id, footer="", thumb=None):
        super().__init__(timeout=300)
        self.title, self.pages, self.owner_id = title, pages or ["Nothing to show."], owner_id
        self.footer, self.thumb, self.page, self.message = footer, thumb, 1, None
        self.refresh()

    def embed(self):
        e = discord.Embed(title=self.title[:256], description=self.pages[self.page - 1], colour=COLOUR)
        if self.thumb:
            e.set_thumbnail(url=self.thumb)
        f = f"Page {self.page}/{len(self.pages)}"
        e.set_footer(text=f"{f} · {self.footer}" if self.footer else f)
        return e

    def refresh(self):
        self.first.disabled = self.previous.disabled = self.page <= 1
        self.next.disabled = self.last.disabled = self.page >= len(self.pages)
        if len(self.pages) <= 1:
            self.clear_items()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("These buttons belong to whoever used the command.", ephemeral=True)
        return False

    async def show(self, interaction, page):
        self.page = min(max(page, 1), len(self.pages))
        self.refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, _b):
        await self.show(interaction, 1)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous(self, interaction, _b):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction, _b):
        await self.show(interaction, self.page + 1)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, _b):
        await self.show(interaction, len(self.pages))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def send(self, interaction):
        kwargs = {"embed": self.embed(), "allowed_mentions": discord.AllowedMentions.none()}
        if self.children:
            kwargs["view"] = self
        await interaction.response.send_message(**kwargs)
        self.message = await interaction.original_response()


def paginate(lines, per_page=20, max_chars=3800):
    pages, cur = [], []
    for line in lines:
        if cur and (len(cur) >= per_page or len("\n".join(cur + [line])) > max_chars):
            pages.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        pages.append("\n".join(cur))
    return pages


def num(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def level_line(lv, count=None, holo=0):
    tier = lv.get("tier") or "Common"
    rank = "-" if tier == "Limited" else lv.get("rank")
    line = f"{TIER_ICON.get(tier, '▫️')} `#{rank:>4}` **{lv.get('name')}** · {tier}" if isinstance(rank, int) \
        else f"{TIER_ICON.get(tier, '▫️')} `#{rank}` **{lv.get('name')}** · {tier}"
    if count is not None:
        line += f" · ×{count}" + (f" (✦ {holo} holo)" if holo else "")
    return line


def tier_order(lv):
    t = lv.get("tier") or "Common"
    return (TIERS.index(t) if t in TIERS else 99, lv.get("rank") or 99999)


def find_member(data, name):
    names = [m["username"] for m in data["members"]]
    exact = [n for n in names if n.lower() == name.lower()]
    if exact:
        return exact[0]
    part = [n for n in names if name.lower() in n.lower()]
    return part[0] if len(part) == 1 else None


def find_level(data, query):
    q = query.strip().lower().lstrip("#")
    levels = data["levels"]
    if q in levels:
        return q
    if q.isdigit():
        for lid, lv in levels.items():
            if str(lv.get("rank")) == q or str(lv.get("gdId")) == q:
                return lid
    exact = [lid for lid, lv in levels.items() if (lv.get("name") or "").lower() == q]
    if exact:
        return min(exact, key=lambda i: levels[i].get("rank") or 99999)
    part = [lid for lid, lv in levels.items() if q in (lv.get("name") or "").lower()]
    return min(part, key=lambda i: levels[i].get("rank") or 99999) if part else None


def updated_text(data):
    return f"Data from {dt.datetime.fromtimestamp(data['updated']).strftime('%Y-%m-%d %H:%M')}"


async def no_data(interaction):
    await interaction.response.send_message("No data yet: the first refresh is in progress (a few minutes), "
                                            "try again soon.", ephemeral=True)


# --------------------------------------------------------------------------- the bot
class GachaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.gacha = Gacha()
        self.sync_lock = asyncio.Lock()
        self.last_error = None

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.loop.create_task(self.daily_sync())

    async def run_sync(self):
        async with self.sync_lock:
            try:
                summary = await sync(self.gacha)
                self.last_error = None
                return summary
            except Exception as e:   # keep the bot running; shown by /gachastatus
                self.last_error = f"{dt.datetime.now():%Y-%m-%d %H:%M}: {e}"
                log.exception("sync failed")
                raise

    async def daily_sync(self):
        await self.wait_until_ready()
        data = load_cache()
        if not data or time.time() - data.get("updated", 0) > 23 * 3600:
            try:
                await self.run_sync()
            except Exception:
                pass
        while not self.is_closed():
            now = dt.datetime.now()
            nxt = now.replace(hour=SYNC_HOUR, minute=7, second=0, microsecond=0)
            if nxt <= now:
                nxt += dt.timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            try:
                await self.run_sync()
            except Exception:
                await asyncio.sleep(1800)   # try again in 30 minutes
                try:
                    await self.run_sync()
                except Exception:
                    pass

    async def close(self):
        await self.gacha.close()
        await super().close()


bot = GachaBot()


@bot.tree.error
async def on_command_error(interaction, error):
    log.error("error in /%s", interaction.command.name if interaction.command else "?", exc_info=error)
    msg = "Something went wrong with this command (details in journalctl -u gacha-bot)."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def member_autocomplete(_interaction, current):
    data = load_cache()
    if not data:
        return []
    names = sorted((m["username"] for m in data["members"]), key=str.lower)
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


async def level_autocomplete(_interaction, current):
    data = load_cache()
    if not data:
        return []
    q = current.lower().lstrip("#")
    levels = sorted(data["levels"].items(), key=lambda kv: kv[1].get("rank") or 99999)
    hits = [(lid, lv) for lid, lv in levels if not q or q in (lv.get("name") or "").lower() or str(lv.get("rank")) == q]
    return [app_commands.Choice(name=f"#{lv.get('rank')} {lv.get('name')} ({lv.get('tier')})"[:100], value=lid)
            for lid, lv in hits[:25]]


TIER_CHOICES = [app_commands.Choice(name=t, value=t) for t in TIERS]


@bot.tree.command(name="cards", description="The Gacha cards a clan member owns")
@app_commands.describe(player="Clan member", tier="Only this rarity", holo="Only holo cards")
@app_commands.autocomplete(player=member_autocomplete)
@app_commands.choices(tier=TIER_CHOICES)
async def cmd_cards(interaction: discord.Interaction, player: str, tier: app_commands.Choice[str] = None,
                    holo: bool = False):
    data = load_cache()
    if not data:
        return await no_data(interaction)
    name = find_member(data, player)
    if not name:
        return await interaction.response.send_message(f"**{player}** is not in the {CLAN} clan.", ephemeral=True)
    coll = data["collections"].get(name)
    if coll is None:
        return await interaction.response.send_message(f"The collection of **{name}** could not be read at the "
                                                       "last refresh.", ephemeral=True)
    levels = data["levels"]
    owned = [(levels[lid], c) for lid, c in coll.items() if lid in levels]
    total_unique = len(owned)
    total_holo = sum(1 for _, c in owned if c["holo"])
    copies = sum(c["count"] for _, c in owned)
    if tier:
        owned = [(lv, c) for lv, c in owned if lv.get("tier") == tier.value]
    if holo:
        owned = [(lv, c) for lv, c in owned if c["holo"]]
    owned.sort(key=lambda x: tier_order(x[0]))
    by_tier = {}
    for lv, _ in owned:
        by_tier[lv.get("tier")] = by_tier.get(lv.get("tier"), 0) + 1
    head = f"**{num(total_unique)}** / {num(len(levels))} cards · **{total_holo}** holo · {num(copies)} copies"
    if tier or holo:
        head += f"\nShowing **{len(owned)}**" + (f" {tier.value}" if tier else "") + (" holo" if holo else "") + " cards"
    else:
        head += "\n" + " · ".join(f"{TIER_ICON.get(t, '')} {by_tier[t]}" for t in TIERS if by_tier.get(t))
    lines = [level_line(lv, c["count"], c["holo"]) for lv, c in owned] or ["No card here."]
    pages = [head + "\n\n" + p for p in paginate(lines, 25, 3500)]
    view = PagedView(f"{name}'s cards", pages, interaction.user.id, footer=updated_text(data))
    await view.send(interaction)


@bot.tree.command(name="whohas", description="Which clan members own a card")
@app_commands.describe(level="Level name, rank (#12) or ID", holo="Only holo copies")
@app_commands.autocomplete(level=level_autocomplete)
async def cmd_whohas(interaction: discord.Interaction, level: str, holo: bool = False):
    data = load_cache()
    if not data:
        return await no_data(interaction)
    lid = find_level(data, level)
    if not lid:
        return await interaction.response.send_message(f"No card found for “{level}”.", ephemeral=True)
    lv = data["levels"][lid]
    owners = [(n, c[lid]) for n, c in data["collections"].items() if lid in c and (not holo or c[lid]["holo"])]
    owners.sort(key=lambda x: (-x[1]["holo"], -x[1]["count"], x[0].lower()))
    lines = [f"• **{n}** · ×{c['count']}" + (f" (✦ {c['holo']} holo)" if c["holo"] else "") for n, c in owners]
    head = (f"{level_line(lv)}\nBy {lv.get('creator') or '?'} · verified by {lv.get('verifier') or '?'}\n"
            f"**{len(owners)}** of {len(data['collections'])} {CLAN} members own it"
            + (" as a holo" if holo else "") + "\n\n")
    pages = [head + p for p in paginate(lines or ["Nobody in the clan has it."], 25, 3500)]
    view = PagedView(f"Who has {lv.get('name')}", pages, interaction.user.id, footer=updated_text(data),
                     thumb=lv.get("thumb"))
    await view.send(interaction)


@bot.tree.command(name="missing", description="The cards a clan member does not have yet")
@app_commands.describe(player="Clan member", tier="Only this rarity")
@app_commands.autocomplete(player=member_autocomplete)
@app_commands.choices(tier=TIER_CHOICES)
async def cmd_missing(interaction: discord.Interaction, player: str, tier: app_commands.Choice[str] = None):
    data = load_cache()
    if not data:
        return await no_data(interaction)
    name = find_member(data, player)
    if not name or name not in data["collections"]:
        return await interaction.response.send_message(f"No collection for **{player}** in the {CLAN} clan.",
                                                       ephemeral=True)
    coll = data["collections"][name]
    missing = [lv for lid, lv in data["levels"].items() if lid not in coll and (not tier or lv.get("tier") == tier.value)]
    missing.sort(key=tier_order)
    head = f"**{len(missing)}** missing" + (f" {tier.value}" if tier else "") + " cards\n\n"
    pages = [head + p for p in paginate([level_line(lv) for lv in missing] or ["None: every card is owned!"], 20, 3500)]
    view = PagedView(f"Cards {name} is missing", pages, interaction.user.id, footer=updated_text(data))
    await view.send(interaction)


@bot.tree.command(name="clan", description=f"The members of the {CLAN} clan on Extreme Demon Gacha")
async def cmd_clan(interaction: discord.Interaction):
    data = load_cache()
    if not data:
        return await no_data(interaction)
    total = len(data["levels"])
    members = sorted(data["members"], key=lambda m: -(m.get("score") or 0))
    lines = []
    for i, m in enumerate(members, 1):
        coll = data["collections"].get(m["username"])
        role = {"owner": " 👑", "co": " ⭐"}.get(m.get("clanRole") or "", "")
        cards = f" · {len(coll)}/{total} cards" if coll is not None else ""
        lines.append(f"`{i:>2}.` **{m['username']}**{role} · {num(m.get('score'))} pts{cards}")
    c = data["clan"]
    head = f"Clan rank **#{c.get('rank')}** · {num(c.get('score'))} pts · {len(members)}/{c.get('max')} members\n\n"
    pages = [head + p for p in paginate(lines, 20, 3500)]
    view = PagedView(f"{c.get('name')} [{c.get('tag')}]", pages, interaction.user.id, footer=updated_text(data))
    await view.send(interaction)


@bot.tree.command(name="gachastatus", description="When the Gacha data was last refreshed")
async def cmd_status(interaction: discord.Interaction):
    data = load_cache()
    e = discord.Embed(title="Gacha data status", colour=COLOUR)
    if data:
        e.add_field(name="Last refresh", value=f"<t:{int(data['updated'])}:f> (<t:{int(data['updated'])}:R>)",
                    inline=False)
        e.add_field(name="Collections", value=f"{len(data['collections'])}/{len(data['members'])} members", inline=True)
        e.add_field(name="Cards in the game", value=num(len(data["levels"])), inline=True)
        if data.get("failed"):
            e.add_field(name="Could not be read", value=", ".join(data["failed"])[:1024], inline=False)
    else:
        e.description = "No data yet."
    e.add_field(name="Next refresh", value=f"Every day around {SYNC_HOUR:02d}:07 (server time)", inline=False)
    if bot.sync_lock.locked():
        e.add_field(name="Now", value="A refresh is in progress.", inline=False)
    if bot.last_error:
        e.add_field(name="Last error", value=bot.last_error[:1024], inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="gachasync", description="Refresh the clan's collections now (a few minutes)")
@app_commands.default_permissions(manage_guild=True)
async def cmd_sync(interaction: discord.Interaction):
    if bot.sync_lock.locked():
        return await interaction.response.send_message("A refresh is already in progress.", ephemeral=True)
    await interaction.response.send_message("Refreshing the collections… (a few minutes)")
    try:
        summary = await bot.run_sync()
        await interaction.followup.send(f"✅ Refresh done: {summary}.")
    except Exception as e:
        await interaction.followup.send(f"❌ Refresh failed: {e}")


@bot.tree.command(name="help", description="What this bot does and its commands")
async def cmd_help(interaction: discord.Interaction):
    e = discord.Embed(title="Gacha clan bot — help", colour=COLOUR, description=(
        f"Shows the **Extreme Demon Gacha** cards of the members of the **{CLAN}** clan. "
        "The collections are refreshed once a day (read-only).\n"
        "`<…>` = required, `[…]` = optional."))
    e.add_field(name="Commands", inline=False, value="\n".join([
        "`/cards <player> [tier] [holo]` — the cards a member owns, rarest first, with copies and holos",
        "`/whohas <level> [holo]` — which members own a card",
        "`/missing <player> [tier]` — the cards a member does not have yet",
        f"`/clan` — the {CLAN} members, their score and how many cards they have",
        "`/gachastatus` — when the data was last refreshed",
        "`/gachasync` — *(server managers)* refresh now",
    ]))
    e.set_footer(text="Unofficial. Data from demongacha.com, read with the site owner's agreement.")
    await interaction.response.send_message(embed=e, ephemeral=True)


def main():
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--sample":   # show what the site sends for one collection
        async def sample():
            g = Gacha()
            try:
                await g.login()
                d = await g.get(f"/api/collection?user={quote(sys.argv[2])}&sort=rank_asc&page=1&pageSize=3")
                print(json.dumps({k: v for k, v in d.items() if k != "items"}, indent=1))
                for c in d.get("items", [])[:3]:
                    print(json.dumps({k: c.get(k) for k in ("id", "levelId", "name", "rank", "tier", "count", "total",
                                                            "holo", "holoCount", "totalHolo") if k in c}),
                          "-> counted as (copies, holo):", card_counts(c))
            finally:
                await g.close()
        asyncio.run(sample())
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--sync":   # refresh from the command line, without Discord
        async def once():
            g = Gacha()
            try:
                print(await sync(g))
            finally:
                await g.close()
        asyncio.run(once())
        return
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is missing in /etc/gacha-bot.conf")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
