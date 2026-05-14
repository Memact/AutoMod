[![Memact Discord](https://img.shields.io/badge/Memact_Discord-00011B?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/WjKDeWuGy5)

# Memact AutoMod

Memact AutoMod is a `nextcord`-powered Discord management bot for the Memact
server. It combines moderation tools, automod protection, queue and ticket
workflows, embed utilities, and optional Bluesky relay posting in one long
running bot service.

## Overview

This repository is focused on running a practical all-in-one server bot rather
than a single-feature integration. The core of the bot is moderation and
community operations:

- compact `/staff` moderation console for bans, kicks, timeouts, warnings,
  purge, locks, slowmode, case history, and raid cleanup
- Discord-native AutoMod protection for spam, mention raids, hate-speech
  presets, and scam-link patterns without bot-side profanity policing
- silent Sentinel intelligence for raids, scams, harassment, hate-speech
  patterns, and rolling member risk review
- security guardrails for anti-nuke detection, audit logging, and SQLite
  backups
- SQLite-backed case history, warning points, scheduled actions, queue entries,
  and server configuration
- rules posting, reusable embed templates, and staff-facing logging
- member reports, appeals, and ticket flows with abuse protection
- optional Bluesky relay posting for announcements and social updates

## License

This repository's source code is open source under the MIT license. See
[LICENSE](LICENSE).

## Features

- compact `/staff` slash commands for bans, kicks, timeouts, warnings, purge,
  locks, slowmode, case history, and raid cleanup
- SQLite-backed case history, warning points, temp-ban scheduling, and server
  config
- native Discord AutoMod rules for spam, mention raids, hate-speech presets,
  and known scam-link patterns
- silent Sentinel detection for protected-class hate patterns, self-harm
  harassment, scam links, homoglyph domains, misleading markdown links,
  new-account bursts, and mention raids
- persistent Sentinel event history and member risk profiles for staff review
- anti-nuke protection for destructive server bursts such as mass bans, kicks,
  channel deletes, and role deletes
- richer audit logging for message edits/deletes, role changes, channel
  changes, bans, unbans, and kicks
- automatic and manual SQLite backups with retention controls
- rules management and rules embed posting
- generic embed creation and reusable embed templates
- member report, appeal, and ticket flows
- optional Bluesky relay with automatic posting to a fixed Discord channel and
  moderator-picked reposts for older posts

## Setup

1. Create a Discord bot in the Discord developer portal.
2. Enable the `SERVER MEMBERS INTENT` and `MESSAGE CONTENT INTENT`.
3. Give the bot the permissions it needs for moderation and safety features,
   including View Audit Log, Moderate Members, Manage Messages, Kick Members,
   Ban Members, Manage Roles, and Manage Channels.
4. Copy `.env.example` to `.env` and fill in `MEMACT_TOKEN`.
5. Install dependencies with `pip install -r requirements.txt`.
6. Run the bot with `python main.py`.
7. Run `/automod install` once in Discord to create or refresh native Memact
   Guard rules, including the hate-speech preset.
8. Use `/security sentinel_recent` or `/security sentinel` to review silent
   risk intelligence after the bot has seen real traffic.

## Bluesky Relay

The Bluesky relay is optional. When enabled, the bot can mirror posts from one
public Bluesky account into the fixed Discord channel `1490277253949558975`.

### Setup

1. Start the bot normally.
2. Make sure the Discord server contains the text channel with ID
   `1490277253949558975`.
3. In Discord, run `/bluesky setup handle:<account>`.
4. The bot saves the current latest Bluesky post as its sync point and starts
   auto-posting only new posts from that moment onward. The relay checks for
   new posts every five minutes.

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

## Moderation Model

Memact AutoMod uses a layered moderation model. Discord's own AutoMod handles
the raw hard-block layer, while the bot focuses on staff workflow, cases,
appeals, audit logs, anti-raid behavior, backups, and silent intelligence.

In practice:

- the bot does not warn members for ordinary profanity or casual keywords
- GIFs, all-caps messages, memes, and normal chat are not judged by a custom
  keyword engine
- native Discord AutoMod blocks spam, mention raids, hate-speech slur presets,
  and known scam-link patterns before they become moderation cases
- Sentinel quietly records high-signal suspicious messages without deleting,
  warning, timing out, or publicly interrupting the member
- Sentinel watches for protected-class violent targeting, dehumanization,
  self-harm harassment, scam phrasing, lookalike domains, misleading markdown
  links, mention bursts, and new-account raid patterns
- Sentinel keeps content hashes, clipped excerpts, signals, confidence,
  severity, and decaying member risk scores in SQLite so restarts do not erase
  the review trail
- manual warnings are staff decisions through `/staff warn`
- warning revokes are member-friendly through `/staff unwarn_latest`
- appeals work with or without a case ID

Useful staff commands:

- `/automod install`: create or refresh native Discord AutoMod rules
- `/automod view`: show Memact Guard native-rule status
- `/automod toggle`: enable or disable Memact Guard
- `/automod mention_limit`: tune native mention-raid protection
- `/security sentinel`: show a member's silent risk profile and recent
  Sentinel events
- `/security sentinel_recent`: show the latest high-signal Sentinel events
- `/staff warnings`: show a member's active warning points and active warning
  cases
- `/staff unwarn_latest`: revoke the latest active warning for a member without
  hunting for a case ID
- `/staff clearwarns`: clear all active warnings for a member
- `/appeal reason:<text>`: appeal the user's latest active moderation case
- `/appeal reason:<text> case_id:<id>`: appeal a specific case

### Persistence

Cases, warning points, scheduled actions, security events, Sentinel events,
Sentinel risk profiles, queue state, and Bluesky catch-up cursors are stored in
the same SQLite database. If you want moderation intelligence and relay state
to survive deploys and restarts, store `MEMACT_DATABASE` somewhere that
JustRunMy.App keeps between restarts and deployments.

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
   - `MEMACT_BACKUP_DIR` (optional, recommended on persistent storage)
   - `MEMACT_BACKUP_INTERVAL_HOURS` (optional, default `12`)
   - `MEMACT_BACKUP_RETENTION` (optional, default `14`)
6. Start the app and watch the JustRunMy.App logs until the bot prints that it
   logged in and synced commands.
7. Run `/automod install` after the bot is online if Discord native AutoMod
   rules have not been created yet.

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
- For defense in depth, set `MEMACT_BACKUP_DIR` to persistent app storage too.
  The bot automatically creates SQLite backups and keeps the latest configured
  number of backup files.

## Security And Backups

Memact AutoMod includes built-in safety controls that follow the same
SQLite-backed configuration style as the rest of the bot.

- `/security view`: show anti-nuke, audit logging, and backup status
- `/security settings`: tune anti-nuke thresholds, audit logs, and the master
  security switch
- `/security sentinel`: review one member's silent Sentinel risk profile
- `/security sentinel_recent`: review recent silent Sentinel detections
- `/security backup_create`: create an immediate SQLite backup
- `/security backup_list`: show recent backup files

Anti-nuke protection watches for bursts of destructive manual server actions.
If one actor crosses the configured threshold, the bot enables raid mode, logs
a security case, and attempts to timeout the actor when Discord permissions and
role hierarchy allow it.

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
