# ReelSense

ReelSense is a self-hosted MCP server that lets Claude (or ChatGPT) read, watch and listen to online videos. Paste a link from YouTube, TikTok, Instagram, Facebook, X, Vimeo, a news site or anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports, and you get back a timestamped transcript with speaker labels.

**It makes its own transcripts.** Many tools like this just fetch the platform's captions, such as YouTube's automatic ones. Those often get names and technical terms wrong, don't say who is speaking, and are missing on most other sites. ReelSense ignores them: it downloads the audio and transcribes it with [Deepgram](https://deepgram.com), so you get:
- speaker labels (S1, S2, …) for interviews, podcasts and panels
- punctuated sentences with timestamps that match the video, even when you transcribe only a part or a chapter
- the same quality on TikTok, Instagram, X or a news site as on YouTube, including videos that have no captions at all

It still uses what the platforms do provide, such as chapters, most-replayed moments, comments and channel details, and adds its own on top. It can also:
- look up video and channel details, list a channel's videos (newest or most popular) and search YouTube
- read YouTube comments
- take still frames Claude can look at (slides, on-screen text)
- identify the music in a video and find tracks on Spotify or Apple Music
- tell Claude what it did on each job, step by step with timings, so Claude can explain a failure or a slow call

In clients that support MCP Apps (claude.ai), videos, channels, frames and music show as cards.

> **Before you run it:** downloading from a platform may break its terms, and the videos belong to their creators. Read [Responsible use](#responsible-use): you are responsible for what your server fetches.

```
Claude / ChatGPT ──HTTPS + sign-in──▶ your tunnel or reverse proxy ──▶ container :8010
                     yt-dlp → ffmpeg → Deepgram → transcript
```

## Tools

| Tool | What it does |
|---|---|
| `transcribe_video` | Transcript of a video, or of one part or chapter of it. Long jobs return a `job_id`. |
| `get_transcript` | Gets the result of a long job by `job_id`. |
| `video_info` | Video details only; nothing is downloaded. |
| `channel_info` | Channel or profile details from a channel or video link. |
| `list_videos` | A channel's, playlist's or profile's videos, newest or most popular. |
| `search_videos` | YouTube keyword search, optionally limited to recent uploads. |
| `get_comments` | Top comments on a YouTube video. |
| `get_frames` | Still images at chosen times. |
| `find_on_screen` | What is on screen when a topic is mentioned: the matching transcript lines, each with a frame from that moment, in one call. Matches by keyword, or also by meaning with a `VOYAGE_API_KEY`. |
| `identify_music` | Names the songs in a video by audio fingerprint (AudD). |
| `find_on_spotify` / `find_on_apple_music` | Finds a track by name. |
| `help` | A user guide: what to ask, limits, and usage this hour. |
| `health` | Versions, settings and who you're signed in as. |

`transcribe_video`, `get_transcript`, `find_on_screen` and `identify_music` also return `steps`: what the server did for that job, with timings, e.g. `"downloaded audio: 0.3 MB (1.2s)"` or `"AudD lookup at 0:04: no match (0.6s)"`. Steps cover only the caller's own job; video titles, raw yt-dlp errors and admin advice stay in the server log. While a job runs, the server's progress messages name the current stage ("downloading the audio… 12s"), for clients that show MCP log messages.

## Screenshots

In claude.ai, each prompt was asked in a new chat:

<table>
<tr>
<td width="50%"><b>Transcript and video card</b><br><i>"Who made this video and what do they say?"</i><br><img src="docs/images/claude-video-card.png" alt="Video card and transcript summary in claude.ai"></td>
<td width="50%"><b>A TikTok, with frames</b><br><i>"Summarise this TikTok"</i><br><img src="docs/images/claude-tiktok-frames.png" alt="TikTok video card and frame gallery in claude.ai"></td>
</tr>
<tr>
<td><b>Frames from a talk with slides</b><br><i>"Show me what's on screen at 0:40, 3:20 and 5:45"</i><br><img src="docs/images/claude-frames-slides.png" alt="Frame gallery of slides in claude.ai"></td>
<td><b>Frames on request</b><br><i>"Show me the elephants in this video"</i><br><img src="docs/images/claude-frames-zoo.png" alt="Frame gallery from Me at the zoo in claude.ai"></td>
</tr>
<tr>
<td><b>Name the song</b><br><i>"What song is playing in this video?"</i><br><img src="docs/images/claude-identify-music.png" alt="Music card with Spotify and Apple Music links in claude.ai"></td>
<td><b>Every song in a mix</b><br><i>"List every song in this mix"</i><br><img src="docs/images/claude-music-full-scan.png" alt="Full-scan music card listing tracks with timestamps in claude.ai"></td>
</tr>
<tr>
<td><b>An Instagram reel</b><br><i>"What does this post say?"</i><br><img src="docs/images/claude-instagram.png" alt="Instagram reel card and summary in claude.ai"></td>
<td><b>A Spotify link by name</b><br><i>"Give me a Spotify link for Bohemian Rhapsody by Queen"</i><br><img src="docs/images/claude-find-on-spotify.png" alt="Spotify search card in claude.ai"></td>
</tr>
<tr>
<td><b>A channel's most popular videos</b><br><i>"What are the 5 most popular videos on NASA's YouTube channel?"</i><br><img src="docs/images/claude-list-videos.png" alt="List of NASA's most viewed videos in claude.ai"></td>
<td><b>Recent uploads on a topic</b><br><i>"Find YouTube videos uploaded this month about the Artemis programme"</i><br><img src="docs/images/claude-search-videos.png" alt="Table of recent Artemis videos in claude.ai"></td>
</tr>
</table>

<details>
<summary><b>The same cards in OpenAI's Codex app, and Claude Code in a terminal</b></summary>

<table>
<tr>
<td width="50%"><b>Video card</b> (Codex)<br><img src="docs/images/codex-video-card.png" alt="Video card in the Codex app"></td>
<td width="50%"><b>Music card</b> (Codex)<br><img src="docs/images/codex-identify-music.png" alt="Music card in the Codex app"></td>
</tr>
<tr>
<td><b>Frames from slides</b> (Codex)<br><img src="docs/images/codex-frames-slides.png" alt="Frame gallery of slides in the Codex app"></td>
<td><b>Frames on request</b> (Codex)<br><img src="docs/images/codex-frames-zoo.png" alt="A frame from Me at the zoo in the Codex app"></td>
</tr>
<tr>
<td><b><code>find_on_screen</code> with no match</b> (Codex): evenly spaced frames instead<br><img src="docs/images/codex-find-on-screen-fallback.png" alt="find_on_screen fallback frames in the Codex app"></td>
<td><b>Claude Code</b><br><i>"Transcribe … and list the key points"</i><br><img src="docs/images/claude-code-transcript.png" alt="Transcript and key points in Claude Code"></td>
</tr>
</table>
</details>

## What you need

- **A computer that stays on**, with [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on Windows or Mac). A home or office connection works best: video platforms often block data-centre IP addresses.
- **A [Deepgram](https://console.deepgram.com) API key** for transcription. New accounts get free credit.
- **A Google account** for sign-in. GitHub, any OpenID Connect provider or Microsoft Entra ID work too.
- **A public https address**: your own domain on Cloudflare, or a free Tailscale address if you have no domain (step 2).
- Optional: an [AudD](https://dashboard.audd.io) token (music identification), a [Spotify app](https://developer.spotify.com/dashboard) (Spotify links) and a [Voyage AI](https://dashboard.voyageai.com) key (`find_on_screen` matching by meaning; the first 200M tokens are free).

## Deploy

Do the steps in this order. The public address comes first because the sign-in app needs it.

### 1. Get the code

```
git clone https://github.com/<you>/<repo>.git reelsense
cd reelsense
cp .env.example .env          (Windows: copy .env.example .env)
```

Open `.env` in an editor; the next steps fill it in. Every setting is explained in the file.

### 2. Choose a public address

Claude connects to your server from the internet, so it needs an https address. Pick one of the three options below and write the address down: it's called `<your-host>` from here on, e.g. `mcp.example.com`.

<details open>
<summary><b>A. Your own domain, through Cloudflare Tunnel</b> (recommended, free)</summary>

There's no router setup, your home IP stays hidden, and HTTPS is automatic. Cloudflare's free plan and the free Zero Trust plan (up to 50 users) are enough. If you have no domain, Cloudflare Registrar sells them at cost (about $10 a year for a `.com`).

1. In the [Cloudflare dashboard](https://dash.cloudflare.com), **add your domain** on the Free plan, then change the nameservers at your registrar to the two Cloudflare shows. Wait until the domain shows as *Active*.
2. Open **Zero Trust → Networks → Tunnels → Create a tunnel**, choose **Cloudflared** and name it `reelsense`.
3. On the install page, choose **Docker** and copy the long token after `--token`. Put it in `.env` as `CLOUDFLARE_TUNNEL_TOKEN=`. You don't need to run the command shown: Docker Compose starts the connector in step 5.
4. Add a **public hostname** (newer dashboards call it *Published application route*): subdomain `mcp`, your domain, service type **HTTP**, URL **`reelsense:8010`**. With the Windows `.bat` scripts and `tunnel.bat`, use **`reelsense-server:8010`** instead.
5. In `.env`, set `PUBLIC_URL=https://mcp.example.com` and `ALLOWED_HOSTS=mcp.example.com`, using your hostname.

Optional: in the Cloudflare dashboard, add a rate-limit rule for the hostname (Security → WAF), e.g. 60 requests a minute per IP.
</details>

<details>
<summary><b>B. No domain: Tailscale Funnel</b> (free, permanent address)</summary>

1. Install [Tailscale](https://tailscale.com/download) on the machine and sign in. The free Personal plan is enough.
2. Optional: in the [admin console](https://login.tailscale.com/admin/machines), rename the machine to `reelsense`. The name becomes part of the address.
3. In the admin console under **DNS**, turn on **MagicDNS** and **HTTPS Certificates**.
4. Run `tailscale funnel --bg 8010`. The first time, it prints a link to allow Funnel: open it, approve, and run the command again. It then prints your address, e.g. `https://reelsense.tail1234.ts.net`. The setting survives restarts.
5. In `.env`, set `PUBLIC_URL=https://reelsense.tail1234.ts.net` and `ALLOWED_HOSTS=reelsense.tail1234.ts.net`.

Leave `CLOUDFLARE_TUNNEL_TOKEN` empty, and start without the tunnel profile in step 5.
</details>

<details>
<summary><b>C. Just testing: Cloudflare Quick Tunnel</b> (free, no account, address changes)</summary>

Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) and run `cloudflared tunnel --url http://localhost:8010`. It prints a random `https://….trycloudflare.com` address, valid until you stop it. Use it as above.

This is for a first try only. Every restart gives a new address, so you'd have to update `.env`, the sign-in app and the connector each time. Quick Tunnels also don't support streamed responses, so long transcriptions can fail. Move to A or B to keep the server.
</details>

### 3. Set up sign-in

Users sign in with the provider set in `AUTH_PROVIDER` (Google by default). Signing in only proves who someone is. **`ALLOWED_USERS`** in `.env` decides who gets in: email addresses, whole domains (`@example.com`) or GitHub usernames. To add or remove someone later, edit the list and restart.

Whichever provider you use, the redirect (callback) URL is `https://<your-host>/auth/callback`. Put the app's client ID and secret in `.env` as `AUTH_CLIENT_ID` and `AUTH_CLIENT_SECRET`.

<details open>
<summary><b>Google</b> (default, <code>AUTH_PROVIDER=google</code>)</summary>

In the [Google Cloud console](https://console.cloud.google.com), create or pick a project, then open **Google Auth Platform**:
1. **Branding:** an app name and support email. **Audience:** *External*.
2. **Clients → Create client:** type *Web application*, with the redirect URL above under *Authorized redirect URIs*.
3. **Audience → Publish app.** While the app is in *Testing*, only listed test users can sign in and sign-ins expire after 7 days. The app only asks for basic profile and email, so publishing needs no review by Google.

`ALLOWED_USERS` is required: without it, anyone with a Google account could sign in.
</details>

<details>
<summary><b>GitHub</b> (<code>AUTH_PROVIDER=github</code>)</summary>

GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App.** Set the homepage URL to `https://<your-host>` and the callback URL to the one above. Then **Generate a new client secret**. List GitHub usernames in `ALLOWED_USERS`; it's required for GitHub.
</details>

<details>
<summary><b>Any OpenID Connect provider</b> (<code>AUTH_PROVIDER=oidc</code>): Keycloak, Authentik, Zitadel, Pocket ID, Auth0...</summary>

Choose this if you want an admin console for users: invite, disable, groups, and login with passwords, passkeys or linked Google and Microsoft accounts.
1. Create a confidential (client-secret) OAuth/OIDC client with the redirect URL above and the scopes `openid email profile`.
2. Set `OIDC_CONFIG_URL` to the provider's discovery URL, e.g. `https://sso.example.com/realms/main/.well-known/openid-configuration` on Keycloak.
3. Control who may sign in from the provider itself (users, groups, app assignment). `ALLOWED_USERS` is optional here, as a second check.
</details>

<details>
<summary><b>Microsoft Entra ID</b> (<code>AUTH_PROVIDER=entra</code>)</summary>

In https://entra.microsoft.com, as an admin:
1. **App registrations → New registration.** Choose *Accounts in this organizational directory only*, with redirect URI platform **Web** and the URL above.
2. **Expose an API.** Set the Application ID URI to `api://<client id>`, and add a scope `access_as_user` with consent *Admins and users*.
3. **Manifest.** Set `"requestedAccessTokenVersion": 2` (older manifests: `"accessTokenAcceptedVersion": 2`). Without it, every token is rejected.
4. **API permissions.** Add **APIs my organization uses → your app → access_as_user**, then **Grant admin consent**.
5. **Certificates & secrets.** Create a new client secret and copy its **Value**.
6. **Enterprise apps → your app → Properties.** Set *Assignment required* to Yes, then add users under **Users and groups** (invite outsiders as guests first).

Set `ENTRA_TENANT_ID` to the Directory (tenant) ID. `ALLOWED_USERS` and `ENTRA_ALLOWED_GROUP_ID` are optional extra checks.
</details>

### 4. Finish `.env`

Set the rest of the required values:
- `DEEPGRAM_API_KEY`: in the Deepgram console, open **API Keys → Create a New API Key**.
- `ALLOWED_USERS`: your own email, plus anyone else who should get in.
- `MCP_JWT_SIGNING_KEY`: 32+ random characters, e.g. from `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Keep it: changing it signs everyone out.
- `SERVER_OWNER`: your name, shown on the sign-in page (optional).

If a required value is missing, the server refuses to start and its log names the value.

Optional: some platforms need a logged-in session (Instagram; YouTube for age-restricted or members-only videos, or when it asks for a bot check; private or login-only posts anywhere). Export your browser's cookies in Netscape format to `cookies/cookies.txt`, signed in to each site you need. YouTube is tried without cookies first and with them only if that fails: YouTube invalidates exported cookies quickly, and stale ones would otherwise break public videos too. Other sites get cookies straight away. To make cookies last, export them from a private window and then close it.

### 5. Start

```
docker compose --profile tunnel up -d --build     # option A (Cloudflare Tunnel)
docker compose up -d --build                      # options B and C
```

The first build takes a few minutes. Then check:
- `docker compose logs -f reelsense` should show `transcribe-mcp on :8010 … auth=google allowlist=on`. If it says `FATAL:` instead, fix the value it names in `.env` and run the same command again.
- `https://<your-host>/healthz` in a browser should show `ok`. If it doesn't, the problem is the tunnel or Funnel, not the server.

Sign-in state and the transcript and music caches live in the Docker volume `reelsense-data`. Keep it: deleting it signs everyone out.

On Windows you can also use the scripts instead of Compose: `build.bat`, then `run.bat`, and `tunnel.bat` for the Cloudflare Tunnel (option A; it reads `CLOUDFLARE_TUNNEL_TOKEN` from `.env` and runs cloudflared in its own container). They expect `cookies.txt` next to them. `deploy.bat` pulls, builds and restarts in one step; `update.bat` refreshes yt-dlp. The scripts' container is called `reelsense-server`, and its data lives in the volume `transcribe-mcp-data` (named before the rename; keep it).

### 6. Connect

- **claude.ai:** Settings → Connectors → **Add custom connector**, URL `https://<your-host>/mcp`. Leave the OAuth fields empty. Click **Connect**, then **Allow**, then sign in.
- **Claude Code:** `claude mcp add --transport http reelsense https://<your-host>/mcp`, then run `/mcp` → reelsense → **Authenticate**.
- **ChatGPT:** add a custom MCP server with URL `https://<your-host>/mcp` and authentication set to OAuth.

Then try: *"Transcribe https://www.youtube.com/watch?v=… and summarise it."* If every tool says "Not authorised", the account you signed in with isn't in `ALLOWED_USERS`.

## Testing

- **Unit tests**, no network or keys: `python -m unittest discover tests`.
- **End-to-end**, against your running server: `pip install -r requirements.txt`, then `python test_mcp.py https://mcp.example.com`. It opens a browser to sign in the first time, then calls every tool on short public videos and prints `OK` or the number of failures. It uses real Deepgram minutes and frame grabs. `--quick` skips transcription, `--measure` prints how much text each tool returns, and `--music` runs the `identify_music` checks (about 60 AudD requests the first time, then cached).

## Keep it running

- **Update ReelSense:** `git pull`, then the same `docker compose … up -d --build` command as in step 5.
- **Refresh yt-dlp weekly.** Platforms break it often; an `HTTP Error 403` in the `[yt-dlp]` log lines that keeps coming back is the usual sign (refresh `cookies.txt` too, if you use one). Occasional TikTok 403s are normal, see below. Run `docker compose build --build-arg YTDLP_BUILD=$(date +%s)`, then `docker compose up -d`. Windows users of the scripts can schedule `update.bat` instead. It checks that the new build starts and can still read a real video, and rolls back to the previous one if not.
- **Read the log:** `docker compose logs -f reelsense` (or `docker logs -f reelsense-server` with the scripts). Each job logs its start with the user and the link (without its query string, apart from the video's ID), each stage with its timing, and `failed:` with the reason the user was given, all under one job id that the `[yt-dlp]` lines for that job share. A `[cookies]` line means the server's cookies need refreshing. Because links are logged next to sign-in names, anyone who can read the log can see what each user looked at.
- **Users:** edit `ALLOWED_USERS`, then run `docker compose up -d`. Compose recreates the container when `.env` changes.
- **Python dependencies** are locked to exact versions in `requirements.txt`, so a rebuild installs what was tested. To update them, edit `requirements.in` if needed, run `./lock.sh`, then rebuild and test. Deno and cloudflared are pinned too: change the tag in the `Dockerfile`, `tunnel.bat` or `docker-compose.yml`.
- **The host port** (8010) is published to this machine only, for Tailscale Funnel, a local proxy or a health check. The Cloudflare Tunnel doesn't need it. If a reverse proxy on another machine has to reach the server, set `HOST_BIND=0.0.0.0` in `.env` (Compose).

## TikTok

TikTok's video servers refuse some download links at random (HTTP 403), with or without cookies; in November 2026 it was about half of them. Its music servers don't. So ReelSense:

- **Downloads the post's sound file when it is the creator's own** (the sound's artist is the post's channel or handle). That file is the video's audio, fetched in one small request with no pause, usually in about a second. The steps say "used the post's own sound file". It must match the video's length within 2 seconds, or ReelSense downloads the video instead.
- **Downloads the video for anything else.** A licensed song or someone else's sound is often not what the video plays: a 50-second voice-over can carry a 3-minute song as its sound.
- **Retries a refused video link with a fresh one**, up to 3 tries in total (`TIKTOK_LINK_ATTEMPTS`), 2 seconds apart. Each retry shows in the steps. Only when all of them are refused is the user told "TikTok refused the download (HTTP 403)".

## Limits and costs

Per-user hourly caps, the longest video allowed and a monthly AudD cap are all set in `.env`. Deepgram and AudD charge per use, so check their pricing before you open the server to other people.

ReelSense only fetches links on the public internet; links to private or local network addresses are refused. Inside its container it runs as an unprivileged user.

## Responsible use

ReelSense downloads media and metadata from other websites with [yt-dlp](https://github.com/yt-dlp/yt-dlp). Two separate permissions are involved. yt-dlp's licence lets you use its software. It does not give you permission to access, download, copy, transcribe, store or redistribute anyone's videos. That depends on the site's terms and on the rights in the video, and many sites' terms, YouTube's among them, forbid downloading outside their own apps even for public videos.

If you run a ReelSense server, you are responsible for making sure its use complies with:

- the terms and policies of the sites the videos come from;
- copyright, privacy, publicity and other rights in the content;
- any access restrictions on the content; and
- the laws where you and your users are.

Only process content that you own, are authorised to use, is licensed for your use, or may otherwise lawfully be processed. This README isn't legal advice, and a disclaimer can't make a prohibited use lawful.

- **No media is kept.** ReelSense downloads a video's audio (or a few frames) to analyse it and deletes it when the job ends. It returns transcripts, frames and metadata to the person who asked, and never serves or re-publishes the video.
- **No getting around protections.** Don't use ReelSense to bypass DRM, paywalls, private access controls or other technical protection measures. Private, members-only and login-only content is out of scope.
- **Cookies are for bot checks on public videos only.** If you give the server a `cookies.txt`, use a spare account of your own, never someone else's session, and don't share the file. Having access through an account doesn't mean you may download or transcribe what it can see. The account can also be flagged or suspended.
- **Platforms can block you.** Heavy use can get the server's IP address rate-limited or banned.
- **Respect creators.** Use what ReelSense returns for research, accessibility, summaries and quotes, not to re-upload or redistribute their work or full transcripts of it.
- **The paid services have their own terms.** By using Deepgram, AudD, Voyage AI, Spotify and Apple's search API you accept their terms as the operator. Deepgram and AudD, for example, require you to have the rights to the audio you send them.

ReelSense and its maintainers are not affiliated with or endorsed by YouTube, TikTok, Instagram, Facebook, X, Vimeo or any other supported site. That a site works with ReelSense doesn't mean every use of it through ReelSense is permitted.

## Privacy

What leaves the server, and for which tool:

- **Deepgram** gets the audio of each video you transcribe (`transcribe_video`, and `find_on_screen` when the video isn't transcribed yet). ReelSense asks Deepgram not to use it for model training (`DEEPGRAM_MIP_OPT_OUT=1`, the default).
- **AudD** gets short audio clips from the video (`identify_music`).
- **Voyage AI**, only if `VOYAGE_API_KEY` is set, gets the transcript's lines and your `find_on_screen` query.
- **Spotify** and **Apple Music** get the track and artist names to look up.
- The video platforms see the server's requests, not yours, plus the server's cookies if you gave it a `cookies.txt`.

What the server keeps:

- **Downloaded media and frames** only while a job runs; they are deleted when it ends. Finished job results are kept in memory for an hour (`JOB_RESULT_TTL_S`).
- **Transcripts** for 7 days (`TRANSCRIPT_CACHE_TTL_S`), with their Voyage vectors, in `transcript_cache.sqlite` in the data volume. **Music results** for 90 days (7 days for no match), in `music_cache.sqlite`. Neither records who asked.
- **The log** holds each job's start with the user's sign-in name and the link (query strings and fragments removed, except the parameters that identify the video), plus timings and errors. Docker keeps it until the container is recreated, unless you set a log rotation.

To clear the transcripts, delete their file: `docker compose exec reelsense rm -f /data/transcript_cache.sqlite`. To clear the music results, empty their table but keep the file, which also holds this month's AudD count:

```
docker compose exec reelsense python -c "import sqlite3; c = sqlite3.connect('/data/music_cache.sqlite'); c.execute('DELETE FROM music'); c.commit()"
```

Don't delete the whole volume unless you also want everyone signed out.

## Licence

[MIT](LICENSE), for ReelSense's own code. The tools and libraries it installs keep their own licences: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
