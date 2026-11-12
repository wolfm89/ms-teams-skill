# ms-teams-skill

**Read and write Microsoft Teams from AI coding agents — using your own Power Platform Microsoft Teams connection and `az login`, with no custom Microsoft Entra app registration, Graph admin consent, or configuration file required.**

[![CI](https://github.com/wolfm89/ms-teams-skill/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wolfm89/ms-teams-skill/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-3f6b45)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B%20(stdlib%20only)-3f6b45)](skills/ms-teams/scripts/teams.py)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20WSL-3f6b45)](#prerequisites--onboarding)
[![skills.sh](https://img.shields.io/badge/skills.sh-install-3f6b45)](https://skills.sh/wolfm89/ms-teams-skill)

An [Agent Skill](https://skills.sh) for **OpenCode**, **Claude Code**, **OpenAI Codex**, **GitHub Copilot**, **Cursor**, **Gemini CLI**, and any `SKILL.md`-compatible coding agent. It ships a single zero-dependency Python 3 script (`skills/ms-teams/scripts/teams.py`) that lets your agent read, search, and sync channels and chats, inspect tabs, files, apps, and meetings, generate daily digests, and post, reply, edit, delete, or react to messages **as you** — with dry-run previews by default.

> **Blog post:** [When Microsoft Graph says "Need admin approval": reaching Teams via a Power Platform connector](https://blog.wolfgangmoser.eu/posts/26-11-ms-teams-power-platform-skill/)

<p align="center">
  <img src="docs/assets/demo.gif" alt="Terminal demo: sending a rich Teams message with HTML, a subject, high importance, an emoji and a mention" width="1000">
</p>

---

## Why this exists

Most Microsoft Teams CLI tools and MCP servers ask Microsoft Entra ID for delegated Microsoft Graph scopes (`Chat.ReadWrite`, `ChannelMessage.Send`, `ChannelMessage.Read.All`) on a client app registration. In enterprise Microsoft 365 tenants, that hits two standard walls:

1. **Conditional Access (`AADSTS53003`)** blocking device-code flows or unapproved client application IDs.
2. **"Need admin approval"** when user consent for third-party or unverified multi-tenant applications is disabled.

At the same time, almost every corporate Microsoft 365 license includes **Power Automate for Office 365** and access to the tenant's `Default-<tenantId>` Power Platform environment, where the first-party **Microsoft Teams connector (`shared_teams`)** is pre-authorized by Microsoft.

`ms-teams-skill` bridges your local `az login` session to your personal `shared_teams` connection runtime and its built-in [Microsoft Graph HTTP passthrough](https://learn.microsoft.com/en-us/connectors/teams/#send-a-microsoft-graph-http-request) — no Power Automate cloud flow, custom app registration, or admin consent required.

```text
┌──────────────────────┐     1. az account get-access-token      ┌──────────────────────────────┐
│  Coding Agent / CLI  │ ──────────────────────────────────────► │  Azure CLI (browser sign-in) │
│   (scripts/teams.py) │                                         └──────────────┬───────────────┘
└──────────┬───────────┘                                                        │
           │ 2. Discover / repair connection (getConsentLink)                   │ Bearer tokens:
           │    & invoke connector runtime (/httprequest)                       │ • service.powerapps.com
           ▼                                                                    │ • apihub.azure.com
┌──────────────────────────────────────────────────────────┐                    │
│  Power Platform Connector Runtime (apihub.azure.com)     │ ◄──────────────────┘
│  First-party Microsoft Teams connection (shared_teams)   │
└──────────┬───────────────────────────────────────────────┘
           │ 3. Delegated Microsoft Graph passthrough (/teams, /me, /users)
           ▼
┌──────────────────────────────────────────────────────────┐
│  Microsoft Graph / Microsoft Teams                       │
│  Channels, threads, reactions, 1:1 & group chats         │
└──────────────────────────────────────────────────────────┘
```

---

## Capabilities

- **Zero-config & multi-tenant:** Automatically discovers your tenant ID, user identity, and Power Platform Teams connection from your `az` access token and caches them per tenant under `~/.cache/ms-teams/<tenantId>/`. Pass `--tenant <tenantId_or_domain>` (or set `MS_TEAMS_TENANT`) when working across multiple Microsoft 365 tenants.
- **Teams, channels, tabs, files & apps:** List joined or associated teams (`teams`), inspect team and channel metadata (`team-info`, `channel-info`), list team and channel members (`members`, `channel-members`), list team tags and tag members (`tags`, `tag-members`), list channel tabs (`tabs`), inspect a channel's SharePoint `filesFolder` (`files`), and list installed Teams apps (`apps`).
- **Chats & sidebar sections:** List 1:1, group, and meeting chats (`chats`), find 1:1 chats by colleague name or email (`find-chat`), list chat members (`chat-members`), create new 1:1 or group chats (`create-chat`; max 20 users, no guests), and list your custom Teams sidebar sections and their items (`sections`, `section-items`).
- **Read, search & delta-sync messages:** Read channel threads and chats (`read`, `thread`, `message`) with replies, reactions, links, importance, and file/card attachments; search messages and replies by text, regex, or author (`search`); or run incremental channel delta syncs (`delta`).
- **Post, reply, edit, delete & Adaptive Cards (dry-run by default):** Every write command (`send`, `reply`, `edit`, `delete`, `react`, `unreact`, `create-chat`, `create-meeting`) prints a full JSON preview unless `--send` is explicitly passed after confirmation. Supports HTML bodies, message importance (`normal`, `high`, `urgent`), and Adaptive Card JSON attachments (`--card` / `--card-file`).
- **Real `@mentions` & custom `:shortcode:` emojis:** Write `@[Ada Lovelace]`, `@[ada@example.com]`, `@[tag:on-call]`, `@[channel:General]`, or `@[team]` inline in HTML; the script resolves the Microsoft Entra / Teams entity IDs, constructs the Graph `mentions` array, and verifies after sending that every mention was attached by Teams. Standard Unicode emojis work directly, and custom tenant emojis (harvested automatically when reading messages/reactions or via `custom-emojis --scan`) can be written as `:shortcode:` in HTML and are expanded to `<customemoji>` tags automatically.
- **Online meetings, transcripts, recordings & AI insights:** Look up online meetings by join URL, meeting ID, or meeting chat (`meeting`), list and download VTT transcripts (`transcripts`, `transcript`) and recordings (`recordings`, `recording`), inspect ad-hoc call transcripts/recordings (`adhoc-calls`), fetch Copilot meeting AI insights (`ai-insights`), or schedule new online Teams meetings (`create-meeting`).
- **Chat and meeting creation (verified live):** `create-chat` creates a 1:1 or group chat (max 20 users, no guests). The chat is visible to its members, and Teams only lets tenant admins delete chats. `create-meeting` schedules a real Teams meeting on your calendar; with no attendees, nobody is notified. See [`capabilities.md`](skills/ms-teams/references/capabilities.md).
- **Self-healing onboarding (`onboard` & `doctor`):** Automatically discovers or creates your `shared_teams` connection in `Default-<tenantId>` and repairs expired connections via `getConsentLink` in your browser.
- **Watchlist digest (`digest`):** Scans channels (`--channel "Team/Channel"` or an optional `~/.config/ms-teams/watchlist.json`) and active chats since the last working day into compact JSONL (`<1900` chars per line) with open-action hints (`mention`, `team`, `question`, `check`).

---

## Installation

### 1. Skills CLI (`skills.sh` — OpenCode, Claude Code, Codex, Copilot, Cursor, Gemini CLI)

```bash
# Install globally for your agents
npx skills add wolfm89/ms-teams-skill --skill ms-teams -g
```

### 2. GitHub CLI (`gh skill`)

```bash
gh skill install wolfm89/ms-teams-skill ms-teams --scope user
```

### 3. Claude Code Plugin Marketplace

```text
/plugin marketplace add wolfm89/ms-teams-skill
/plugin install ms-teams@ms-teams-skill
```

### 4. Git Clone & Symlink (OpenCode / Manual)

```bash
git clone https://github.com/wolfm89/ms-teams-skill.git ~/projects/ms-teams-skill

# Shared across all agents supporting ~/.agents/skills:
mkdir -p ~/.agents/skills
ln -s ~/projects/ms-teams-skill/skills/ms-teams ~/.agents/skills/ms-teams

# Or specifically for OpenCode:
mkdir -p ~/.config/opencode/skills
ln -s ~/projects/ms-teams-skill/skills/ms-teams ~/.config/opencode/skills/ms-teams
```

An optional `/teams-digest` command template is included at [`examples/commands/teams-digest.md`](examples/commands/teams-digest.md) and can be copied into `~/.config/opencode/commands/` or `.claude/commands/`.

---

## Prerequisites & Onboarding

1. **Python 3.10+** (standard library only — zero `pip` dependencies; runs on Windows, macOS, WSL, and Linux). On native Windows use `py -3` or `python` wherever the examples say `python3`.
2. **Azure CLI (`az`)** signed in via interactive browser flow:
   ```bash
   az login --allow-no-subscriptions
   ```
   *(If you work across multiple tenants, pass `--tenant <tenantId_or_domain>` to `az login` and `teams.py`, or set `MS_TEAMS_TENANT`.)*
3. **Run onboarding once** (creates or discovers your Power Platform Microsoft Teams connection in `Default-<tenantId>`, opens the browser consent link if needed, and caches the runtime URL):
   ```bash
   python3 ~/.agents/skills/ms-teams/scripts/teams.py onboard --smoke
   ```
   *(If automatic connection creation is restricted in your tenant, open [make.powerautomate.com](https://make.powerautomate.com) → **`… More`** in the left sidebar → **Connections** → **+ New connection** → **Microsoft Teams** once, sign in, and re-run `onboard`.)*
4. **Verify at any time:**
   ```bash
   python3 ~/.agents/skills/ms-teams/scripts/teams.py doctor
   ```

---

## CLI Usage

```bash
TEAMS_PY=~/.agents/skills/ms-teams/scripts/teams.py

# Discover teams, channels, members, tags, tabs, files, and apps
python3 "$TEAMS_PY" teams
python3 "$TEAMS_PY" channels --team "Core Engineering"
python3 "$TEAMS_PY" members --team "Core Engineering"
python3 "$TEAMS_PY" tags --team "Core Engineering"
python3 "$TEAMS_PY" tabs "channel:Core Engineering/General"
python3 "$TEAMS_PY" files "channel:Core Engineering/General"

# Read or search channel threads and chats
python3 "$TEAMS_PY" read "channel:Core Engineering/General" --since 7d --limit 20
python3 "$TEAMS_PY" search "channel:Core Engineering/General" "deployment" --since 14d
python3 "$TEAMS_PY" find-chat "Ada Lovelace"
python3 "$TEAMS_PY" read self --limit 5

# Preview a message with real @mentions (DRY RUN — nothing is sent without --send)
python3 "$TEAMS_PY" send --to self --html-file - <<'EOF'
<p>Hi @[Ada Lovelace], could you take a look at this?</p>
EOF

# Generate a compact JSONL digest since the last working day
python3 "$TEAMS_PY" digest \
  --channel "Core Engineering/General" \
  --since last-workday \
  --out teams-digest.jsonl
```

See [`examples/watchlist.example.json`](examples/watchlist.example.json) and [`skills/ms-teams/references/digest.md`](skills/ms-teams/references/digest.md) for configuring an optional persistent watchlist at `~/.config/ms-teams/watchlist.json`.

---

## Sample prompts

- "Summarize what I missed in my Teams channels and chats since yesterday, and list open questions for me."
- "Read `channel:Engineering/General` since Monday and extract decisions, owners, and action items."
- "Find the thread about the deployment in `channel:Engineering/Releases` and give me a five-bullet summary."
- "Search `channel:Engineering/Architecture` for 'rate limit' in the last 30 days and group the answers by author."
- "Post the release notes to `channel:Engineering/Releases`, mention `@[Release Team]`, and show me the preview first."
- "Create a digest of my watched channels and chats for the last working day."
- "Schedule a 30-minute Teams meeting with `@[Ada Lovelace]` tomorrow at 10:00; show me the preview first."
- "Summarize the transcript of my last meeting into decisions and action items."

All writes are dry runs until the agent shows the preview and you confirm.

---

## Safety & Governance

### Built-in agent safety rails
- **Dry-run by default:** `send`, `reply`, `edit`, `delete`, `react`, `unreact`, `create-chat`, and `create-meeting` never mutate state unless `--send` is passed. The skill instructions require the agent to show the rendered preview and resolved mentions and wait for an explicit confirmation first.
- **Own-message enforcement:** `edit` and `delete` verify that the target message's author ID matches your user ID before calling Graph `PATCH` or `softDelete`.
- **No stored tokens:** Access tokens are requested in memory from `az account get-access-token` per invocation and are never written to disk.
- **No destructive admin operations:** Creating/archiving channels or teams and modifying memberships/tags are intentionally omitted.

### Tenant requirements & governance notes for administrators
This skill operates entirely within the signed-in user's delegated permissions using official Microsoft endpoints (`service.powerapps.com`, `apihub.azure.com`, and the `shared_teams` connector). For it to work in a tenant, five conditions must hold:
1. The user holds a license that includes Power Automate (such as the seeded *Power Automate for Office 365* plan).
2. The user can create or use personal connections in the `Default-<tenantId>` Power Platform environment.
3. The `shared_teams` connector is not blocked by a Power Platform Data Loss Prevention (DLP) policy.
4. Interactive `az login` is permitted and Conditional Access allows tokens for `https://service.powerapps.com/` and `https://apihub.azure.com`.
5. `consent.azure-apim.net` is reachable for one-time connection authorization.

Organizations that wish to govern or restrict direct connector runtime usage outside Power Automate flows should enable Default environment routing, apply connector DLP policies to the Default environment, and include the Power Platform API and Azure API Connections service principals in their Conditional Access policies.

---

## Development & Tests

Run the unit test suite with Python's built-in `unittest` runner (CI runs it on Linux, macOS and Windows with Python 3.10, 3.12 and the latest 3.x):

```bash
python3 -m unittest discover -s tests -v
```

Regenerate the README demo with `vhs docs/demo.tape`.

## License

[GPL-3.0-or-later](LICENSE) © 2026 Wolfgang Moser
