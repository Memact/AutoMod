[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/WjKDeWuGy5)

# Memact AutoMod

Memact AutoMod is a `nextcord`-powered Discord management bot for the Memact
server. It combines moderation tools, automod protection, queue and ticket
workflows, embed utilities, and optional Bluesky relay posting in one long
running bot service.

## Overview

This repository is focused on running a practical all-in-one server bot rather
than a single-feature integration. The core of the bot is moderation and
community operations:

- slash-command moderation for bans, kicks, timeouts, warnings, purge, locks,
  slowmode, nicknames, and role tools
- automod protections for spam, duplicate messages, invite links, blocked
  words, caps abuse, mention flooding, and raid-mode responses
- SQLite-backed case history, warning points, scheduled actions, queue entries,
  and server configuration
- rules posting, reusable embed templates, and staff-facing logging
- member reports, appeals, and ticket flows with abuse protection
- optional Bluesky relay posting for announcements and social updates

## License

This repository's source code is open source under the Apache-2.0 license. See
[LICENSE](LICENSE).

Memact branding and assets are not open source. The `Memact` name, logos,
icons, artwork, banners, screenshots, and other Memact-owned brand assets are
excluded from the code license unless a file explicitly says otherwise. See
[NOTICE](NOTICE) and [BRANDING.md](BRANDING.md).

## Features

- moderation slash commands for bans, kicks, timeouts, warnings, purge, locks,
  slowmode, nicknames, and role tools
- SQLite-backed case history, warning points, temp-ban scheduling, and server
  config
- automod for spam, duplicate messages, invite links, blocked words, caps, and
  mention flooding
- rules management and rules embed posting
- generic embed creation and reusable embed templates
- member report, appeal, and ticket flows
- optional Bluesky relay with automatic posting to a fixed Discord channel and
  moderator-picked reposts for older posts

## Setup

1. Create a Discord bot in the Discord developer portal.
2. Enable the `SERVER MEMBERS INTENT` and `MESSAGE CONTENT INTENT`.
3. Copy `.env.example` to `.env` and fill in `MEMACT_TOKEN`.
4. Install dependencies with `pip install -r requirements.txt`.
5. Run the bot with `python main.py`.
6. Optional: set `MEMACT_STREAM_TITLE` and `MEMACT_STREAM_URL` to control the
   streaming presence. Default title is `Moderating this server`.

## Bluesky Relay

The Bluesky relay is optional. When enabled, the bot can mirror posts from one
public Bluesky account into the fixed Discord channel `1490277253949558975`.

### Setup

1. Start the bot normally.
2. Make sure the Discord server contains the text channel with ID
   `1490277253949558975`.
3. In Discord, run `/bluesky setup handle:<account>`.
4. The bot saves the current latest Bluesky post as its sync point and starts
   auto-posting only new posts from that moment onward.

### Moderator commands

- `/bluesky view`: show the selected account, relay status, fixed relay
  channel, and last synced post
- `/bluesky sync_now`: immediately catch up on posts that arrived while the
  bot was offline
- `/bluesky history`: open a Discord picker that lets moderators browse and
  manually send older Bluesky posts into the relay channel
- `/bluesky disable`: pause automatic posting
- `/bluesky enable`: resume automatic posting
- `/bluesky remove`: clear the saved Bluesky account configuration

The relay uses Bluesky's public AppView HTTP endpoint, so no extra Bluesky
credentials are required for read-only mirroring.

### Persistence

The catch-up state is stored in the same SQLite database as the rest of the
bot's data. If you want the Bluesky relay to survive deploys and restarts,
store `MEMACT_DATABASE` somewhere that JustRunMy.App keeps between restarts and
deployments.

## JustRunMy.App Git Deployment

This bot is ready for JustRunMy.App Git deployment. The root `Dockerfile`
installs `requirements.txt`, copies the repo, and starts the long-running bot
with `python main.py`.

Recommended settings:

1. Push this repository to GitHub.
2. In JustRunMy.App, create a Discord bot or container app and choose the Git
   deployment method.
3. Connect the repository or add the JustRunMy.App Git remote shown in the
   dashboard, then deploy from the `main` branch.
4. Use the root `Dockerfile` as the build target.
5. Add the environment variables:
   - `MEMACT_TOKEN`
   - `MEMACT_GUILD_ID` (optional but recommended if this bot should stay locked
     to one server)
   - `MEMACT_DATABASE`
   - `MEMACT_STREAM_TITLE` (optional)
   - `MEMACT_STREAM_URL` (optional but required for streaming presence)
6. Start the app and watch the JustRunMy.App logs until the bot prints that it
   logged in and synced commands.

Important JustRunMy.App notes:

- Discord bots do not need a public HTTP port. Add one only if you want to use
  the optional `/healthz` endpoint.
- Keep secrets such as `MEMACT_TOKEN` in JustRunMy.App environment variables,
  not in `.env`.
- Use the dashboard logs, web shell, and auto-restart controls for debugging
  and recovery.
- For durable SQLite data, set `MEMACT_DATABASE` to a path that lives on
  persistent app storage. This preserves moderation cases, queue state, and
  Bluesky sync cursors across restarts and Git deploys.

The included `Dockerfile` is ready for JustRunMy.App and other Docker-based
hosts.

## Optional Keepalive Endpoint

This repo includes a lightweight HTTP endpoint for hosts that need a health
check or public status route. For normal JustRunMy.App Discord bot hosting, no
public port is required. If you do enable the endpoint, it serves `/` and
`/healthz`.

Useful optional environment variables:

- `MEMACT_KEEPALIVE_PORT=10000`
- `MEMACT_KEEPALIVE_HOST=0.0.0.0`
- `MEMACT_ENABLE_KEEPALIVE=true`
