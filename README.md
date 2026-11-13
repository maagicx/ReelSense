# ReelSense

ReelSense is a self-hosted MCP server that lets Claude (or ChatGPT) read, watch and listen to online videos. Paste a link from YouTube, TikTok, Instagram, Facebook, X, Vimeo, a news site or anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports, and you get back a timestamped transcript with speaker labels.

It can also:
- look up video and channel details, list a channel's videos (newest or most popular) and search YouTube
- read YouTube comments
- take still frames Claude can look at (slides, on-screen text)
- identify the music in a video and find tracks on Spotify or Apple Music

In clients that support MCP Apps (claude.ai), videos, channels, frames and music show as cards.

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
| `find_on_screen` | What is on screen when a topic is mentioned: the matching transcript lines, each with a frame from that moment, in one call. |
| `identify_music` | Names the songs in a video by audio fingerprint (AudD). |
| `find_on_spotify` / `find_on_apple_music` | Finds a track by name. |
| `help` | A user guide: what to ask, limits, and usage this hour. |
| `health` | Versions, settings and who you're signed in as. |

## What you need

- **A computer that stays on**, with [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on Windows or Mac). A home or office connection works best: video platforms often block data-centre IP addresses.
- **A [Deepgram](https://console.deepgram.com) API key** for transcription. New accounts get free credit.
- **A Google account** for sign-in. GitHub, any OpenID Connect provider or Microsoft Entra ID work too.
- **A public https address**: your own domain on Cloudflare, or a free Tailscale address if you have no domain (step 2).
- Optional: an [AudD](https://dashboard.audd.io) token (music identification) and a [Spotify app](https://developer.spotify.com/dashboard) (Spotify links).

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
4. Add a **public hostname** (newer dashboards call it *Published application route*): subdomain `mcp`, your domain, service type **HTTP**, URL **`reelsense:8010`**. If you use the Windows `.bat` scripts with cloudflared installed on Windows, use `localhost:8010` instead.
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

Optional: some platforms need a logged-in session (Instagram, and YouTube when it asks for a bot check). Export your browser's cookies in Netscape format to `cookies/cookies.txt`.

### 5. Start

```
docker compose --profile tunnel up -d --build     # option A (Cloudflare Tunnel)
docker compose up -d --build                      # options B and C
```

The first build takes a few minutes. Then check:
- `docker compose logs -f reelsense` should show `transcribe-mcp on :8010 … auth=google allowlist=on`. If it says `FATAL:` instead, fix the value it names in `.env` and run the same command again.
- `https://<your-host>/healthz` in a browser should show `ok`. If it doesn't, the problem is the tunnel or Funnel, not the server.

Sign-in state and the music cache live in the Docker volume `reelsense-data`. Keep it: deleting it signs everyone out.

On Windows you can also use the scripts instead of Compose: `build.bat`, then `run.bat`. They expect `cookies.txt` next to them, and cloudflared installed separately.

### 6. Connect

- **claude.ai:** Settings → Connectors → **Add custom connector**, URL `https://<your-host>/mcp`. Leave the OAuth fields empty. Click **Connect**, then **Allow**, then sign in.
- **Claude Code:** `claude mcp add --transport http reelsense https://<your-host>/mcp`, then run `/mcp` → reelsense → **Authenticate**.
- **ChatGPT:** add a custom MCP server with URL `https://<your-host>/mcp` and authentication set to OAuth.

Then try: *"Transcribe https://www.youtube.com/watch?v=… and summarise it."* If every tool says "Not authorised", the account you signed in with isn't in `ALLOWED_USERS`.

## Keep it running

- **Update ReelSense:** `git pull`, then the same `docker compose … up -d --build` command as in step 5.
- **Refresh yt-dlp weekly.** Platforms break it often; an `HTTP Error 403` in the `[yt-dlp]` log lines is the usual sign. Run `docker compose build --build-arg YTDLP_BUILD=$(date +%s)`, then `docker compose up -d`. Windows users of the scripts can schedule `update.bat` instead, which also rolls back a bad build.
- **Users:** edit `ALLOWED_USERS`, then run `docker compose up -d`. Compose recreates the container when `.env` changes.

## Limits and costs

Per-user hourly caps, the longest video allowed and a monthly AudD cap are all set in `.env`. Deepgram and AudD charge per use, so check their pricing before you open the server to other people.

## Licence

[MIT](LICENSE)
