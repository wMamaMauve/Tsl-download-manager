#!/usr/bin/env python3
"""Bot Discord pour tsl-sync.

- Publie dans un salon les changements détectés par tsl-sync.sh
  (fichiers déposés dans DATA_DIR/notifications/).
- Commandes : /niveau, /changements, /liste, /statut, /verifier.

Configuration : la même que tsl-sync (/etc/tsl-sync.conf ou TSL_CONF) :
    DISCORD_BOT_TOKEN="..."      jeton du bot (Discord Developer Portal)
    DISCORD_CHANNEL_ID="..."     salon où publier les annonces
    DISCORD_GUILD_ID="..."       (optionnel) serveur Discord, pour que les commandes apparaissent tout de suite
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

DISCORD_MAX = 1900          # limite d'un message Discord (2000) avec une marge
UPLOAD_MAX = 10 * 1024**2   # taille max d'une pièce jointe pour un bot


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


DATA_DIR = Path(setting("DATA_DIR", "/var/lib/tsl-sync"))
FILES = DATA_DIR / "fichiers"
LISTS = setting("LISTS", "tsl tslplus").split()
TOKEN = setting("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(setting("DISCORD_CHANNEL_ID", "0") or 0)
GUILD_ID = int(setting("DISCORD_GUILD_ID", "0") or 0)
NOTIF_DIR = DATA_DIR / "notifications"
LIST_NAMES = {"tsl": "The Shitty List", "tslplus": "The Shitty List Plus"}


# --------------------------------------------------------------------------- lecture des données
def read_tsv(path):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.split("\t") for line in lines if line]


def load_index():
    """Classement actuel : liste de dicts (liste, position, id, nom, auteur, fichier)."""
    levels = []
    for lst in LISTS:
        rows = read_tsv(FILES / lst / "index.tsv")
        for row in rows[1:]:
            if len(row) < 5:
                continue
            pos, lid, name, author, rel = row[:5]
            levels.append({"liste": lst, "position": int(pos) if pos.isdigit() else 0,
                           "id": lid, "nom": name, "auteur": author, "fichier": rel})
    return levels


def load_state():
    state = {}
    for row in read_tsv(DATA_DIR / "state.tsv"):
        row += [""] * (7 - len(row))
        lid, ver, _sha, lst, rel, _info, status = row[:7]
        state[lid] = {"version": ver, "liste": lst, "fichier": rel, "statut": status or "ok"}
    return state


def load_log():
    try:
        return (DATA_DIR / "changements.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_levels(query):
    q = query.strip().lower()
    levels = load_index()
    exact = [lv for lv in levels if lv["id"] == q or lv["nom"].lower() == q]
    if exact:
        return exact
    return [lv for lv in levels if q in lv["nom"].lower() or q in lv["auteur"].lower()]


IMPORTANT = ("MODIFIÉ", "INFOS MODIFIÉES", "SUPPRIMÉ", "RÉAPPARU", "ENTRÉE", "SORTIE", "NOUVEAU")


def split_messages(lines, header=""):
    """Regroupe des lignes en messages de moins de 2000 caractères."""
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


def short_log_line(line):
    """'2026-11-10 04:21:07 [tsl] MODIFIÉ : …' -> '`10/11 04:21` [tsl] MODIFIÉ : …'"""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d) (\d\d:\d\d):\d\d (.*)", line)
    if not m:
        return line
    return f"`{m.group(3)}/{m.group(2)} {m.group(4)}` {m.group(5)}"


# --------------------------------------------------------------------------- bot
class TSLBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.loop.create_task(self.notification_loop())

    async def on_ready(self):
        log.info("Connecté en tant que %s", self.user)

    async def notification_loop(self):
        """Publie les fichiers de notifications déposés par tsl-sync.sh."""
        await self.wait_until_ready()
        NOTIF_DIR.mkdir(parents=True, exist_ok=True)
        while not self.is_closed():
            try:
                await self.flush_notifications()
            except Exception:  # on ne doit jamais arrêter la boucle
                log.exception("erreur lors de l'envoi des notifications")
            await asyncio.sleep(30)

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


async def niveau_autocomplete(_interaction, current):
    levels = find_levels(current) if current else load_index()[:25]
    return [app_commands.Choice(name=f"{lv['nom']} — {lv['liste']} #{lv['position']}"[:100], value=lv["id"])
            for lv in levels[:25]]


@bot.tree.command(name="niveau", description="Infos et fichier .gmd d'un niveau de la liste")
@app_commands.describe(recherche="Nom, auteur ou ID du niveau")
@app_commands.autocomplete(recherche=niveau_autocomplete)
async def cmd_niveau(interaction: discord.Interaction, recherche: str):
    matches = find_levels(recherche)
    if not matches:
        await interaction.response.send_message(f"Aucun niveau trouvé pour « {recherche} ».", ephemeral=True)
        return
    if len(matches) > 1 and not any(lv["id"] == recherche for lv in matches):
        lines = [f"• **{lv['nom']}** de {lv['auteur']} — {lv['liste']} #{lv['position']} (`{lv['id']}`)"
                 for lv in matches[:15]]
        more = f"\n… et {len(matches) - 15} autres" if len(matches) > 15 else ""
        await interaction.response.send_message(
            f"{len(matches)} niveaux correspondent, précise ta recherche :\n" + "\n".join(lines) + more,
            ephemeral=True)
        return
    lv = next((x for x in matches if x["id"] == recherche), matches[0])
    st = load_state().get(lv["id"], {})
    history = [short_log_line(line) for line in load_log() if f"({lv['id']})" in line][-6:]

    embed = discord.Embed(title=lv["nom"], url=f"https://gdbrowser.com/{lv['id']}",
                          colour=0xE74C3C if st.get("statut") == "supprime" else 0x3498DB)
    embed.add_field(name="Créateur", value=lv["auteur"] or "?", inline=True)
    embed.add_field(name="Liste", value=f"{LIST_NAMES.get(lv['liste'], lv['liste'])} — #{lv['position']}", inline=True)
    embed.add_field(name="ID", value=lv["id"], inline=True)
    embed.add_field(name="Version", value=st.get("version") or "?", inline=True)
    embed.add_field(name="Statut", value="🗑️ supprimé des serveurs GD (copie conservée)"
                    if st.get("statut") == "supprime" else "✅ en ligne", inline=True)
    if history:
        embed.add_field(name="Derniers changements", value="\n".join(history)[:1024], inline=False)
    embed.set_thumbnail(url=f"https://levelthumbs.prevter.me/thumbnail/{lv['id']}")

    path = FILES / lv["fichier"] if lv["fichier"] and not lv["fichier"].startswith("(") else None
    if path and path.is_file() and path.stat().st_size <= UPLOAD_MAX:
        await interaction.response.send_message(embed=embed, file=discord.File(path, filename=path.name))
    else:
        note = "" if not path or not path.is_file() else "\n_(fichier .gmd trop gros pour Discord, voir Nextcloud)_"
        await interaction.response.send_message(content=note or None, embed=embed)


@bot.tree.command(name="changements", description="Ce qui a changé ces derniers jours")
@app_commands.describe(jours="Nombre de jours (défaut : 7)", tout="Inclure aussi les changements de classement")
async def cmd_changements(interaction: discord.Interaction, jours: app_commands.Range[int, 1, 365] = 7,
                          tout: bool = False):
    since = (dt.datetime.now() - dt.timedelta(days=jours)).strftime("%Y-%m-%d")
    lines = [line for line in load_log() if line[:10] >= since
             and (tout or any(f"] {k}" in line for k in IMPORTANT if k != "NOUVEAU"))]
    if not lines:
        await interaction.response.send_message(f"Aucun changement ces {jours} derniers jours.")
        return
    msgs = split_messages([short_log_line(x) for x in lines[-200:]],
                          header=f"**Changements des {jours} derniers jours** ({len(lines)})")
    await interaction.response.send_message(msgs[0], allowed_mentions=discord.AllowedMentions.none())
    for msg in msgs[1:5]:
        await interaction.followup.send(msg, allowed_mentions=discord.AllowedMentions.none())
    if len(msgs) > 5:
        await interaction.followup.send("… liste tronquée, utilise un nombre de jours plus petit.")


@bot.tree.command(name="liste", description="Afficher le classement")
@app_commands.describe(liste="Quelle liste", page="Page (25 niveaux par page)")
@app_commands.choices(liste=[app_commands.Choice(name=LIST_NAMES.get(x, x), value=x) for x in LISTS])
async def cmd_liste(interaction: discord.Interaction, liste: app_commands.Choice[str],
                    page: app_commands.Range[int, 1, 100] = 1):
    levels = sorted((lv for lv in load_index() if lv["liste"] == liste.value), key=lambda x: x["position"])
    chunk = levels[(page - 1) * 25: page * 25]
    if not chunk:
        await interaction.response.send_message("Page vide.", ephemeral=True)
        return
    pages = (len(levels) + 24) // 25
    lines = [f"`#{lv['position']:>3}` **{lv['nom']}** — {lv['auteur']}" for lv in chunk]
    await interaction.response.send_message(
        f"**{liste.name}** — page {page}/{pages}\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="statut", description="Résultat du dernier passage de tsl-sync")
async def cmd_statut(interaction: discord.Interaction):
    try:
        last = (DATA_DIR / "dernier_passage.txt").read_text(encoding="utf-8").strip()
    except OSError:
        last = "Aucun passage enregistré pour l'instant."
    state = load_state()
    deleted = sum(1 for s in state.values() if s["statut"] == "supprime")
    counts = ", ".join(f"{LIST_NAMES.get(x, x)} : {sum(1 for lv in load_index() if lv['liste'] == x)}"
                       for x in LISTS)
    running = subprocess.run(["systemctl", "is-active", "--quiet", "tsl-sync"], check=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    await interaction.response.send_message(
        f"**Dernier passage** : {last}\n**Niveaux suivis** : {counts}\n"
        f"**Supprimés des serveurs GD** : {deleted}\n"
        f"**Passage en cours** : {'oui ⏳' if running else 'non'}")


@bot.tree.command(name="verifier", description="Lancer un passage de vérification maintenant")
@app_commands.default_permissions(manage_guild=True)
async def cmd_verifier(interaction: discord.Interaction):
    res = subprocess.run(["systemctl", "start", "--no-block", "tsl-sync"], capture_output=True, text=True,
                         check=False)
    if res.returncode == 0:
        await interaction.response.send_message(
            "⏳ Passage lancé (environ 35 min). Les changements seront annoncés ici à la fin.")
    else:
        await interaction.response.send_message(f"Impossible de lancer le passage : {res.stderr.strip()[:500]}",
                                                ephemeral=True)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN manquant dans /etc/tsl-sync.conf")
    if not CHANNEL_ID:
        log.warning("DISCORD_CHANNEL_ID manquant : les annonces automatiques sont désactivées")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
