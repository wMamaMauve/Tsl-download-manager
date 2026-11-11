# Gacha clan bot

A second Discord bot that shows which **[Extreme Demon Gacha](https://demongacha.com)** cards the members of the **TSL** clan own.

Once a day it logs in with its own account (made for this, with the site owner's agreement), reads the clan's member list and each member's collection, and keeps them in `/var/lib/gacha-bot/cache.json`. The commands answer from that file, so they are instant and do not touch the site. **It only reads**: it never opens packs, trades, duels or changes anything.

| Command | What it does |
|---|---|
| `/cards <player> [tier] [holo]` | the cards a clan member owns, rarest first, with copies and holos |
| `/whohas <level> [holo]` | which clan members own a card (name, rank `#12` or ID) |
| `/missing <player> [tier]` | the cards a member does not have yet |
| `/clan` | the clan members, their score and how many cards they have |
| `/gachastatus` | when the data was last refreshed, and any error |
| `/gachasync` | *(server managers)* refresh now (a few minutes) |
| `/help` | the commands |

## Installation (on the server, as root)

1. Create a second application on https://discord.com/developers/applications (Bot > Reset Token), invite it with "bot" + "applications.commands" and the "Send Messages" + "Embed Links" permissions.
2. Install:
   ```bash
   cd ~/Tsl-download-manager && git pull
   ./gacha-bot/install.sh
   nano /etc/gacha-bot.conf     # DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, GACHA_USERNAME, GACHA_PASSWORD
   systemctl restart gacha-bot && journalctl -u gacha-bot -f
   ```
   The first refresh starts right away (about 5–10 minutes for 79 members, one request per second).

`/etc/gacha-bot.conf` is only readable by root: the account's password and the bot token never leave it.

Useful: `/opt/gacha-bot/venv/bin/python /opt/gacha-bot/gacha_bot.py --sync` refreshes from the terminal; `… --sample <player>` shows what the site sends for one collection.
