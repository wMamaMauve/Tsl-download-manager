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
| `/help` | every command and what it does (only visible to you; click a command to use it) |
| `/video <link>` | which levels and players use a video (paste a YouTube, Medal, Drive… link or a YouTube ID): title, level, creator, list position, player, whether the video still works, whether it is a duplicate or possibly on the wrong level (with a **Validate** button) |
| `/records [level] [show]` | verification, showcase and record videos that can no longer be watched (private, deleted…), with a link and since when; with `show: Duplicate videos`, the videos given for different players or listed twice; with `show: Wrong level`, the records whose video title names another level; with `show: Checked by hand`, who validated what and when |
| `/run [what]` | (admins) start a run now: **levels and record videos** (default), **levels only** (re-downloads every level, ~35 min) or **record videos only** (checks every video again, ~20 min) |

### Pictures of the changed objects in `/diff`

`/diff` can show a picture of the objects that were added, removed or moved, with their count. The pictures are cut out of **your own copy of the game** (they are RobTop's art, so they are not in this repository):

1. copy the game's `Resources` folder to the server (Steam: `steamapps/common/Geometry Dash/Resources`; only `GJ_GameSheet*` and the other `*Sheet*` `.png` + `.plist` files are needed), e.g. to `/var/lib/tsl-sync/gd-resources/`;
2. `/opt/tsl-sync/venv/bin/python /opt/tsl-sync/extract_sprites.py /var/lib/tsl-sync/gd-resources` — saves one `<id>.png` per object in `/var/lib/tsl-sync/objects/` (another folder: `SPRITES_DIR` in the config).

Run it again after a game update. Without these pictures, `/diff` works as before.

**When does the bot post?** The automatic run of the night posts only when something changed (a level modified, deleted, a video gone…). A run started with `/run` (or by hand in a terminal) always posts its result, even "✅ No changes were detected."

Setup: `./install-bot.sh` (installs Python and discord.py), then fill in `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` in `/etc/tsl-sync.conf` (the steps are explained in the file) and run `systemctl restart tsl-bot`. Log: `journalctl -u tsl-bot -f`.

## Second bot: Extreme Demon Gacha

`gacha-bot/` holds a separate Discord bot showing which demongacha.com cards the members of the TSL clan own (`/cards`, `/find`, `/missing`, `/clan`). See [gacha-bot/README.md](gacha-bot/README.md).

## Change detection

On every run, the script:

1. reads both lists on tsl.pages.dev;
2. writes the **entries**, **exits** and **ranking changes** to `changes.log`;
3. downloads **every** level again and compares all its data (SHA-256 of the level data) — the version number is not reliable;
4. also compares the info (name, description, song, stars, featured/epic…; not the difficulty, which follows the players' votes) and reports which ones changed;
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

Object names (3D outline, glow block, gradient, shader trigger…) come from `object_names.tsv`, a table of all ~4,100 object IDs built from the GD 2.208 editor sprite list ([StarryDawn72/open-source-gd-project](https://github.com/StarryDawn72/open-source-gd-project)); an ID missing from it is shown as `object #ID`.

An object is counted as *moved* when an identical object (same properties except its position) was removed close by (less than 20 blocks away); otherwise it counts as one removal and one addition. You can also compare two files by hand: `/opt/tsl-sync/level_diff.sh old.gmd new.gmd details.txt`.

Log example:

```
2026-11-10 04:21:07 [tsl] ENTERED the list: #12 Shitty Something (123456789)
2026-11-10 04:21:07 [tsl] RANKING: Shitty Nhelv (106481744) #3 -> #4
2026-11-10 04:23:40 [tsl] MODIFIED: Shitty NETWORK (132801785) v1 -> v2 (previous version in history/...)
```

## Record videos

Every night, the script also checks that the videos of each level can still be watched: the verification, the showcase and **every record** listed on the site (about 15,000 videos).

- YouTube: a video is **private** or **deleted / unavailable**; a video whose embedding is disabled still counts as watchable;
- other sites (Medal, Google Drive, …): **deleted** when the page is gone (404), **no access** when it asks to sign in;
- a link that is not a link at all is reported as **invalid link**.

When a video becomes unavailable (or available again), it is written to `changes.log` and announced on Discord (the first 10, then a count). `/records` lists every unavailable video, and `unavailable_videos.tsv` in Nextcloud holds the same list. The first check takes about 20 minutes and only posts a count; after that, a working video is checked again every 3 days (`RECORD_CHECK_DAYS`) and unavailable ones every night. A video that could not be checked (timeout, YouTube rate limit) keeps its previous status. To disable the check: `RECORD_CHECK=0`.

**Log of every check**: each run writes `records-log/YYYY-MM-DD_HHMM.tsv` (in Nextcloud, opens as a spreadsheet) with **every** video of the site: list, level, level ID, creator, kind (verification / showcase / record), player, link, result (available, private, deleted / unavailable, no access, invalid link), when it was checked (this run, or N days ago and still valid) and whether it is a duplicate. Nextcloud keeps every log; on the server, logs older than 30 days are removed (`RECORD_LOG_DAYS`).

**Wrong level?**: the title of each YouTube video is read too. When a record's video title names **another level of the list** and not its own (e.g. "Shitty Acheron 100%" given as a record of Shitty NETWORK), it is reported (new ones on Discord, all of them in `/records` › Wrong level and in `wrong_level_videos.tsv`). "Sh*tty", "Sh!tty", "sh <name>" and "2" / "II" are understood; a title naming no level, a compilation (the same player's video for several levels) or a title naming the record's own level is fine. When a report is wrong, `/video <link>` shows a **Validate** button (server managers only): that record is then never reported again. Every validation is traced: who (Discord name **and Discord ID**), when, and exactly what (level, player, link, title, level named). It goes to `changes.log` (so `/changes` shows it), to `/records` › Checked by hand, to `/video` ("checked by hand by @someone on …") and to `checked_by_hand.tsv` in Nextcloud.

**Duplicate videos**: the same video given for **different players** (one of the two records has the wrong link), or the same record **listed twice** on a level, is reported too (new ones on Discord, all of them in `/records` › Duplicate videos and in `duplicate_videos.tsv`). The same YouTube video written in different ways (`youtu.be/…`, `watch?v=…`, `?si=…`) counts as one. A player's compilation video used for several levels is normal and is not reported.

## Useful commands

| Action | Command |
|---|---|
| Run now | `systemctl start --no-block tsl-sync` |
| Levels only / record videos only | `/opt/tsl-sync/tsl-sync.sh levels` / `/opt/tsl-sync/tsl-sync.sh records` |
| Re-upload **all** local files to Nextcloud | `RESYNC=1 /opt/tsl-sync/tsl-sync.sh` |
| Show the sync log | `journalctl -u tsl-sync` |
| Next scheduled run | `systemctl list-timers tsl-sync.timer` |
| Show the bot log | `journalctl -u tsl-bot -f` |

## Notes

- Levels that are in both lists are stored only once, in `tsl/`; the `tslplus` index points to that file.
- The Geometry Dash servers block datacenter IPs (Cloudflare, error 1005). The script must run on a machine that can reach them. Test with:
  `curl -s -A "" -X POST https://www.boomlings.com/database/downloadGJLevel22.php -d "levelID=132801785&secret=Wmfd2893gb7" | head -c 100`
- Upgrading from the French version: local and Nextcloud folders (`fichiers`, `historique`, `changements.log`…) are renamed automatically on the first run.
