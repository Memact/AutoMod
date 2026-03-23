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

## Deployment

This project is set up to run as a long-lived worker process on hosts such as
Render, Railway, Fly.io, Docker-based VPS setups, and other Python worker
platforms.

- `.python-version` pins Python to `3.12`
- `Procfile` exposes a standard worker entrypoint: `python main.py`
- `render.yaml` configures a Render background worker with a persistent disk
- `MEMACT_DATABASE` can be either a relative local file or an absolute mounted
  path such as `/opt/render/project/src/data/memact_automod.db`

### Render

1. Push this repo to GitHub.
2. In Render, create a new **Background Worker** from the repo or use the
   included `render.yaml` Blueprint.
3. Set `MEMACT_TOKEN` in the Render dashboard when prompted.
4. Keep `MEMACT_GUILD_ID=1404684829785718885` unless you intentionally want a
   different server lock.
5. If you use SQLite, keep the persistent disk attached so moderation data and
   rules survive restarts and redeploys.

### Other hosts

Use the same environment variables from `.env.example` and run:

```bash
python main.py
```
