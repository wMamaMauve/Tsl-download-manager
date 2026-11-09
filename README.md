# TSL download manager

Automatically downloads every level of **The Shitty List** ([TSL](https://tsl.pages.dev/list/tsl) and [TSL+](https://tsl.pages.dev/list/tslplus)) as **`.gmd`** files, detects when a level is changed, deleted or moved in the ranking, stores everything in **Nextcloud** and announces the changes on **Discord**.

The sync script is pure bash: it only needs `curl`, `awk`, `sed`, `od` and `sha256sum`, available on almost any Linux. The optional Discord bot uses Python (installed for you by `install-bot.sh`).

## Installation (on the server, as root)

```bash
git clone -b claude/youthful-curie-myzxdg https://github.com/wMamaMauve/Tsl-download-manager.git
cd Tsl-download-manager
./install.sh            # automatic run every night around 4 am (./install.sh 13 for 1 pm)
nano /etc/tsl-sync.conf # fill in NC_URL, NC_USER, NC_PASS
systemctl start --no-block tsl-sync && journalctl -u tsl-sync -f   # first run
```

Every run downloads all ~630 levels with a 3 s pause between requests, which takes about 35 minutes.

**Nextcloud password**: use an *app password* (Nextcloud → Settings → Security → "Create new app password"), not your real password. `/etc/tsl-sync.conf` is only readable by root.

**Updating**: `cd ~/Tsl-download-manager && git pull && ./install.sh` (it never overwrites your config).

## Local storage, then upload

Files are first stored on the server in `/var/lib/tsl-sync/files/`, then **everything is uploaded to Nextcloud at once at the end of the run**. If Nextcloud is unreachable, the files stay in the queue (`/var/lib/tsl-sync/upload_queue.tsv`) and are sent on the next run. To upload as you go instead, set `UPLOAD_AT_END=0`.

## What you get in Nextcloud

```
Geometry Dash/TSL/
├── tsl/
│   ├── Shitty NETWORK (132801785).gmd
│   ├── ...
│   └── index.tsv            ← current ranking (position, id, name, author, file)
├── tslplus/
│   └── ...
├── 2026-11-10/              ← one folder per run day:
│   ├── tsl/...gmd           ←   levels new or changed that day
│   └── changes.log          ←   log of the day ("No changes." otherwise)
├── history/
│   └── Shitty NETWORK (132801785)/
│       └── 2026-11-09_v1.gmd   ← previous versions of changed levels
└── changes.log              ← log of everything that changed
```

The `.gmd` files can be imported in the game with **GDShare** (via Geode): they show up in your editor levels.

The folders of the day are copies made directly in Nextcloud (nothing is re-uploaded). To disable them: `DAILY=0` in the config.

## Discord bot

The bot posts everything that changes to a channel (level modified, info changed, deleted, reappeared, entered/left the list) and answers commands:

| Command | What it does |
|---|---|
| `/level <name or id>` | level info, latest changes, link to the level on the list site, its verification video **and the .gmd file as an attachment** |
| `/diff <name or id>` | what changed in a level the last time it was modified: objects added / removed / moved, where, settings, and the full list as an attachment |
| `/changes [days]` | what changed in the last days |
| `/list <tsl\|tslplus> [page]` | the ranking, 25 levels per page, with First / Previous / Next / Last buttons |
| `/status` | result of the last run |
| `/run` | (admins) start a tracking run now: re-downloads every level and looks for changes (~35 min) |

Setup: `./install-bot.sh` (installs Python and discord.py), then fill in `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` in `/etc/tsl-sync.conf` (the steps are explained in the file) and run `systemctl restart tsl-bot`. Log: `journalctl -u tsl-bot -f`.

## Change detection

On every run, the script:

1. reads both lists on tsl.pages.dev;
2. writes the **entries**, **exits** and **ranking changes** to `changes.log`;
3. downloads **every** level again and compares all its data (SHA-256 of the level data) — the version number is not reliable;
4. also compares the info (name, description, song, stars, difficulty, featured/epic…) and reports which ones changed;
   a level that disappears from the GD servers is reported **DELETED** (its last copy is kept), then **REAPPEARED** if it comes back;
5. if the level data really changed, the previous version goes to `history/` and the new one replaces the file (**MODIFIED** in the log);
6. the two versions are compared **object by object** (`level_diff.sh`): objects added, removed and moved, where in the level (in %), which kinds of objects (blocks, spikes, triggers, portals, orbs…) and which level settings changed (start speed, colors, song offset…). The summary goes to the log and to Discord, and the full list is saved next to the previous version in `history/…/<date>_vX_to_vY_diff.txt`.

Example of a Discord announcement:

```
[tsl] Level modified: Shitty NETWORK by Whitar
- +124 / -38 objects, 5 moved (34,931 -> 35,017) — mostly between 62% and 71% of the level
- Added: 80 blocks, 30 spike/hazards, 14 move triggers
- Removed: 38 blocks
- Settings changed: start speed
```

An object is counted as *moved* when an identical object (same properties except its position) was removed close by (less than 20 blocks away); otherwise it counts as one removal and one addition. You can also compare two files by hand: `/opt/tsl-sync/level_diff.sh old.gmd new.gmd details.txt`.

Log example:

```
2026-11-10 04:21:07 [tsl] ENTERED the list: #12 Shitty Something (123456789)
2026-11-10 04:21:07 [tsl] RANKING: Shitty Nhelv (106481744) #3 -> #4
2026-11-10 04:23:40 [tsl] MODIFIED: Shitty NETWORK (132801785) v1 -> v2 (previous version in history/...)
```

## Useful commands

| Action | Command |
|---|---|
| Run now | `systemctl start --no-block tsl-sync` |
| Re-upload **all** local files to Nextcloud | `RESYNC=1 /opt/tsl-sync/tsl-sync.sh` |
| Show the sync log | `journalctl -u tsl-sync` |
| Next scheduled run | `systemctl list-timers tsl-sync.timer` |
| Show the bot log | `journalctl -u tsl-bot -f` |

## Notes

- Levels that are in both lists are stored only once, in `tsl/`; the `tslplus` index points to that file.
- The Geometry Dash servers block datacenter IPs (Cloudflare, error 1005). The script must run on a machine that can reach them. Test with:
  `curl -s -A "" -X POST https://www.boomlings.com/database/downloadGJLevel22.php -d "levelID=132801785&secret=Wmfd2893gb7" | head -c 100`
- Upgrading from the French version: local and Nextcloud folders (`fichiers`, `historique`, `changements.log`…) are renamed automatically on the first run.
