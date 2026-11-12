# Record Checker

A web page for **list staff** to check a record video. Paste a YouTube link: the server downloads the video (yt-dlp), and the page shows it with:

- **frame-by-frame** controls (`←` `→`), **slow motion** (×0.1 to ×1), frame number and exact time;
- the **clicks** found in the sound, on a timeline of the whole video (clicks per second, one tick per click);
- a **close-up of the sound** that follows the video, with the clicks, the ms between them and the video's frames;
- **passages to check**: clicks too regular (a macro), very fast clicking, two clicks almost together;
- the video's title, channel, upload date, fps and resolution.

The page is protected by a password. Videos are kept on the server (up to `CHECKER_MAX_GB`, oldest removed first), so a video opened twice is not downloaded again. Share a video with another staff member with the page's link (`…/?v=<YouTube id>`).

## Install (on the server, as root)

```bash
cd ~/Tsl-download-manager && git pull
./record-checker/install.sh
```

It installs ffmpeg, yt-dlp and the service, and prints the page's password (also in `/etc/record-checker.conf`).

## Opening the page

By default the page only listens on the server itself (`127.0.0.1:8099`), so it is not reachable from the internet. From your PC, open an SSH tunnel and keep that window open:

```powershell
ssh -L 8099:127.0.0.1:8099 root@<your server>
```

then go to **http://localhost:8099**.

To give it to the whole staff, put it behind your web server with HTTPS (e.g. a sub-domain proxied by nginx or Caddy to `127.0.0.1:8099`) and set `CHECKER_SECURE_COOKIE=1`. Do not open port 8099 directly to the internet: the password would travel unencrypted.

## Notes

- yt-dlp is updated at each start of the service (`systemctl restart record-checker`): YouTube changes often.
- yt-dlp may warn that no JavaScript runtime is installed. If some videos fail, install [deno](https://deno.com) (`curl -fsSL https://deno.land/install.sh | sh`).
- If YouTube answers "Sign in to confirm you're not a bot", the server's address is blocked: yt-dlp then needs cookies from a YouTube account (`--cookies`), see the yt-dlp documentation.
- The click analysis only points at moments to look at: it is not proof by itself.
