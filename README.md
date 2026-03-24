# Memact AutoMod

Memact AutoMod is a `nextcord` moderation bot for a private Discord server.

## License

This repository's source code is open source under the Apache-2.0 license. See
[LICENSE](LICENSE).

Memact branding and assets are not open source. The `Memact` name, logos,
icons, artwork, banners, screenshots, and other Memact-owned brand assets are
excluded from the code license unless a file explicitly says otherwise. See
[NOTICE](NOTICE) and [BRANDING.md](BRANDING.md).

## Features

- moderation slash commands for bans, kicks, timeouts, warnings, purge, locks, slowmode, nicknames, and role tools
- SQLite-backed case history, warning points, temp-ban scheduling, and server config
- automod for spam, duplicate messages, invite links, blocked words, caps, and mention flooding
- rules management and rules embed posting
- generic embed creation and reusable embed templates
- member report and appeal flows

## Setup

1. Create a Discord bot in the Discord developer portal.
2. Enable the `SERVER MEMBERS INTENT` and `MESSAGE CONTENT INTENT`.
3. Copy `.env.example` to `.env` and fill in `MEMACT_TOKEN`.
4. Install dependencies with `pip install -r requirements.txt`.
5. Run the bot with `python main.py`.
6. Optional: set `MEMACT_STREAM_TITLE` and `MEMACT_STREAM_URL` to control the
   streaming presence. Default title is `Moderating this server`.
7. Optional: set `CONTENT_SAFETY_ENDPOINT` and `CONTENT_SAFETY_KEY` to enable
   Azure AI Content Safety image moderation (nudity/inappropriate images).

## Railway Deployment

Railway is a better fit for this bot than a Replit keepalive loop because it
supports persistent long-running services without requiring a public uptime
ping.

Recommended settings:

1. Push this repository to GitHub.
2. In Railway, create a new `Service` from the repository.
3. Let Railway use the root `Dockerfile` automatically.
4. Keep this as a persistent service and leave `Serverless` disabled.
5. Add the environment variables:
   - `MEMACT_TOKEN`
   - `MEMACT_GUILD_ID` (optional but recommended if this bot should stay locked
     to one server)
   - `MEMACT_DATABASE`
   - `MEMACT_STREAM_TITLE` (optional)
   - `MEMACT_STREAM_URL` (optional but required for streaming presence)
6. Skip `Public Networking` unless you specifically want to expose the optional
   `/healthz` endpoint.

Important Railway notes:

- Railway services are persistent by default. `Serverless` is a separate
  opt-in feature that sleeps inactive services, so it should stay off for a
  24/7 Discord bot.
- Every service gets ephemeral storage, but it does not persist across
  deployments. The default `memact_automod.db` path is only safe for testing.
- If you want to keep using SQLite, attach a volume at `/data` and set
  `MEMACT_DATABASE=/data/memact_automod.db`.
- Railway volumes are persistent, but each service can only have one volume and
  replicas cannot be used with attached volumes.
- Railway trial and free accounts do not support the `Always` restart policy.
  On those plans, `On Failure` is limited to 10 restarts.
- Railway's Limited Trial has restricted outbound networking. If your account
  is not fully verified, that can interfere with a Discord bot connecting out
  to Discord.

Practical deployment options:

- Quick test: deploy the service with default ephemeral storage
- Better 24/7 setup: add a Railway volume, set
  `MEMACT_DATABASE=/data/memact_automod.db`, and use a paid plan with restart
  policy set to `Always`

The included `Dockerfile` is ready for Railway and other container-based hosts.

## Replit Workaround

This repo includes a lightweight keepalive HTTP endpoint for Replit-style
hosting workarounds. When the app detects Replit environment variables, or when
`MEMACT_KEEPALIVE_PORT` is set, it opens a tiny HTTP server on `/` and
`/healthz`.

- `.replit` maps internal port `10000` to external port `80`
- the keepalive server listens on `0.0.0.0`
- UptimeRobot can ping the published app URL to help keep an Autoscale app warm

Important caveats:

- this is a workaround, not true always-on bot hosting
- Replit Starter currently includes one free published app, and the published
  app expires after 30 days but can be re-published
- published app storage is not persistent, so SQLite data can reset

Useful optional environment variables:

- `MEMACT_KEEPALIVE_PORT=10000`
- `MEMACT_KEEPALIVE_HOST=0.0.0.0`
- `MEMACT_ENABLE_KEEPALIVE=true`
