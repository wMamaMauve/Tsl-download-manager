# Gacha clan bot

A second Discord bot that shows which **[Extreme Demon Gacha](https://demongacha.com)** cards the members of the **TSL** clan own.

Every 15 minutes (`REFRESH_MINUTES`) it does a **quick refresh**: one request per member for their totals (cards, holos, packs opened), and only the collections whose totals changed are read again (about 80 requests, 1–2 minutes). Once a day (`SYNC_HOUR`) it does a **full refresh** of every collection, in case a 1-for-1 trade left the totals unchanged. It reads the site with its own account (made for this, with the site owner's agreement), reads the clan's member list and each member's collection, and keeps them in `/var/lib/gacha-bot/cache.json`. The commands answer from that file, so they are instant and do not touch the site. **It only reads**: it never opens packs, trades, duels or changes anything.

| Command | What it does |
|---|---|
| `/cards <player> [tier] [holo]` | the cards a clan member owns, rarest first, with copies and holos |
| `/find <level> [holo]` | which clan members own a card (name, rank `#12` or ID) |
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
   nano /etc/gacha-bot.conf     # DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNEL_ID, GACHA_SID
   systemctl restart gacha-bot && journalctl -u gacha-bot -f
   ```
   The first refresh starts right away (about 5–10 minutes for 79 members, one request per second).

**The bot's account logs in with Discord**, so the bot uses its **session cookie** (`sid`) instead of a password. On a computer: log in on demongacha.com with the bot's account, press F12 > *Application* (Chrome/Edge) or *Storage* (Firefox) > *Cookies* > `https://demongacha.com`, and copy the value of `sid` into `GACHA_SID`. A session lasts **30 days**: when the site renews it the bot keeps the new one; otherwise it warns in `DISCORD_CHANNEL_ID` a few days before and when it has expired (also shown by `/gachastatus`). Then copy a fresh `sid` and `systemctl restart gacha-bot`. Logging out of the site with that account ends the session, so just close the tab.

`/etc/gacha-bot.conf` is only readable by root: the session and the bot token never leave it.

Useful: `/opt/gacha-bot/venv/bin/python /opt/gacha-bot/gacha_bot.py --sync` refreshes from the terminal; `… --sample <player>` shows what the site sends for one collection.
