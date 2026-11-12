---
name: ms-teams
description: "Read and write Microsoft Teams from the command line: read, search, and sync messages in channels and chats (with replies, attachments, and reactions), post/reply/edit/delete your own messages and Adaptive Cards with real @mentions of people, team tags and channels, react to channel messages, inspect teams, channels, members, tags, tabs, files, installed apps, sidebar sections, and online meetings (transcripts, recordings, Copilot AI insights), or schedule meetings. Use whenever the user wants to read, summarize, search, post or answer something in Teams, mentions a Teams channel, chat, or meeting, or asks to notify/ping someone in Teams. Also use for setting up or repairing Teams access (onboarding, 'Teams does not work', connection errors)."
license: GPL-3.0-or-later
compatibility: Requires Python 3.10+ (standard library only), the Azure CLI signed in with `az login`, a Power Platform Microsoft Teams connection, and network access to Microsoft Graph and Power Platform. Works on Linux, macOS, WSL and native Windows.
---

# ms-teams

Talks to Microsoft Teams through the user's own **Power Platform Teams connection** and the
connector's Microsoft Graph passthrough. It needs only an `az login` and a standard Power Platform
Microsoft Teams connection; no custom Microsoft Entra app registration, Graph admin consent, or
config file is required. Everything is done by one zero-dependency Python 3 script inside this
skill directory:

```bash
python3 <skill-dir>/scripts/teams.py [--tenant <tenantId_or_domain>] [--refresh] <command> ...
```

On native Windows use `py -3` or `python` instead of `python3`.
`--refresh` ignores the cached team, channel and member lists (kept for 1 and 24 hours) and fetches
them again; use it after a rename or when someone just joined a team.

Replace `<skill-dir>` with the base directory shown when this skill is loaded (commonly
`~/.agents/skills/ms-teams` or `~/.config/opencode/skills/ms-teams`).

Messages are sent **as the user**, so treat every write as the user speaking.

Call the script by its path (it is executable and has a shebang). Do not keep `python3 .../teams.py`
in a shell variable such as `TEAMS="python3 ..."`: zsh does not split an unquoted variable on spaces
and fails with "no such file or directory". Feed multi-line HTML with a quoted heredoc:
`--html-file - <<'EOF' ... EOF`.

## First use / multi-tenant / something is broken

1. **Zero-config discovery:** When your `az` login and Power Platform Teams connection are already
   `Connected`, commands (`teams`, `read`, `send`, etc.) auto-discover your tenant, user profile,
   and connection runtime and cache them per tenant in `~/.cache/ms-teams/<tenantId>/session.json`.
2. **Multiple tenants:** Pass `--tenant <tenantId_or_domain>` (or set `MS_TEAMS_TENANT`) to target a
   specific Microsoft Entra tenant. Each tenant gets its own isolated cache directory under
   `~/.cache/ms-teams/<tenantId>/`.
3. **Setup / repair:** `teams.py doctor` checks every setup step and prints `OK` or `FIX`. If
   anything is `FIX`, run `teams.py onboard` (add `--smoke` to post one test message to your own
   self-chat). It creates or repairs the connection by opening a consent page in your default
   browser. Details: `references/onboarding.md`.
4. Read `references/dead-ends.md` before trying other ways in (Graph PowerShell, device code,
   local Teams API, UI automation).

## Commands

| Command | Purpose |
|---|---|
| `doctor`, `onboard` | check / set up or repair the Power Platform Teams connection |
| `teams [--associated]`, `team-info <team>` | list joined or associated teams, get team details |
| `channels --team T`, `channel-info <target>` | list channels in a team, get channel details |
| `members --team T`, `channel-members <target>` | list team members or direct channel members |
| `tags --team T`, `tag-members --team T --tag TAG` | list team tags and tag members |
| `tabs <channel target>`, `files <channel target>` | list channel tabs, get SharePoint `filesFolder` metadata |
| `apps <team or channel target>` | list installed Teams apps in a team |
| `find-user <name or email> [--team T]` | resolve a person (cached team members first, then tenant) |
| `chats [--type oneOnOne\|group\|meeting]`, `find-chat <person>` | list chats, find a 1:1 chat |
| `chat-members <chat target>` | list members of a chat |
| `create-chat --users u1,u2 [--topic "..."] [--send]` | create a 1:1 or group chat (dry-run unless `--send`; max 20 users, no guests) |
| `sections`, `section-items <section>` | list custom Teams sidebar sections and their items |
| `read <target> [--since 7d] [--limit N] [--mentioning-me] [--json]` | read messages (channels include replies, reactions, attachments) |
| `thread <channel target> <messageId>` | one channel message with all replies |
| `message <target> <messageId> [--reply-id ID]` | read a single message or reply |
| `search <target> <query> [--regex] [--author A] [--since 30d]` | search messages and replies in a channel or chat |
| `delta <channel target> [--delta-link URL]` | incremental channel message sync via Graph delta query |
| `digest [--since last-workday\|today\|yesterday\|3d\|YYYY-MM-DD] [--channel Team/Channel] [--watchlist FILE] [--no-chats] [--no-meeting-chats] [--out FILE] [--format lines\|json] [--verbose]` | read-only JSON-lines digest of watched channels and active chats (see `references/digest.md`) |
| `send --to <target> [--subject S] [--importance normal\|high\|urgent] --html/--text/--html-file/--card/--card-file ...` | post a message or Adaptive Card |
| `reply --to <channel target> --message-id ID ...` | reply in a channel thread |
| `edit`, `delete --to <target> --message-id ID [--reply-id ID]` | change / soft-delete **your own** message |
| `react`, `unreact --to <channel target> --message-id ID [--reply-id ID] --emoji 👍` | unicode reactions, channels only |
| `meeting <joinWebUrl\|meetingId\|chat:ID>` | look up online meeting metadata |
| `transcripts <meeting>`, `transcript <meeting> <transcriptId> [--out FILE]` | list and fetch VTT meeting transcripts |
| `recordings <meeting>`, `recording <meeting> <recordingId> --out FILE` | list and download meeting recordings |
| `adhoc-calls [--kind transcripts\|recordings]` | list transcripts or recordings from ad-hoc calls |
| `ai-insights <meeting> [--insight-id ID]` | list or fetch Copilot AI insights for a meeting |
| `create-meeting --subject "..." --start ISO --end ISO [--attendees ...] [--send]` | schedule an online Teams meeting (dry-run unless `--send`; needs an Exchange Online mailbox) |
| `graph <path> [--version v1.0\|beta] [--header "Name: Value"]` | raw read-only Graph GET through the connector |
| `op <path>` | raw read-only connector GET operation |
| `custom-emojis [--scan <target>] [--json]` | list or harvest custom tenant emojis for `:shortcode:` use in HTML |

**Digest & optional watchlist (`~/.config/ms-teams/watchlist.json`):** `digest` checks channels passed
via `--channel "Team/Channel"` (repeatable) or listed in `~/.config/ms-teams/watchlist.json`
(overridable via `--watchlist FILE` or `MS_TEAMS_WATCHLIST`), plus your active 1:1, group, and meeting
chats. It prints one JSON object per line (`header`, `post`, `reply`, `chat`, `chatmsg`), or writes
them to `--out FILE` (mode 0600) and prints only a summary. Every line stays under 1900 characters so
one `read` shows everything. Details: `references/digest.md`.

**Targets:** `self` (the user's own self-chat), `chat:<chatId>`, or `channel:<Team fragment>/<Channel fragment>`.
Names are matched loosely (case, accents, emoji ignored) and must be unique.

**Mentions** go inline in the HTML body as `@[...]` placeholders, resolved to real mentions:

| Placeholder | Mentions | Where |
|---|---|---|
| `@[Ada]`, `@[Ada Lovelace]`, `@[ada.lovelace@example.com]` | a person | chats and channels |
| `@[tag:on-call]` | a team tag | channels only |
| `@[channel:General]` or `@[channel:Engineering/General]` | a channel | channels only |
| `@[team]` | the whole team | channels only |

Ambiguous or unknown names stop with a list of candidates; never guess a person. Details:
`references/mentions.md`.

## Output shape of `read --json`

`{target, me, count, messages}`. Each message: `id`, `author`, `authorId`, `byMe`, `created`, `modified`, `subject`,
`importance`, `text`, `links` (list of URLs in the message), `attachments` (optional), `mentions`, `mentionsMe`,
`reactions`, `reactionNames`, `reactionsByMe`, `replies`, `replyCount`, `webUrl`.

## Safety rules (follow these every time)

- **Writes are dry-runs by default.** `send`, `reply`, `edit`, `delete`, `react`, `unreact`,
  `create-chat`, and `create-meeting` only print a preview until `--send` is passed. Show the user
  the preview (rendered text, target, resolved mentions) and pass `--send` **only after an explicit
  yes in this conversation**.
- **Only post where the user told you to.** Never pick another channel or chat on your own. For
  first tests use target `self`.
- **After sending, check the result.** The script prints `mentionsResolved` / `mentionsMissing`.
  If a mention is missing, tell the user and offer the printed undo command (`delete ... --send`);
  do not delete or repost without their OK.
- **Edit/delete only touch your own messages**; the script refuses anything else.
- **No destructive admin operations**: no creating/archiving channels or teams, no membership or tag
  modifications. Do not use `graph` or `op` for anything but reading.
- **Reading is for the question asked.** Summarize instead of dumping messages; do not write
  message contents into files or other tools unless the user asks.
- Tokens are never stored; the script asks `az` each run. Do not print or persist them.

## Limits to remember

- **Emojis in messages vs. reactions:**
  - In **message bodies** (`--html` / `--html-file`), both standard Unicode emojis (`👍 👀 💬 ✅ 🚀`) and custom tenant emojis (`:shortcode:`) work. Custom emojis are harvested automatically into `~/.cache/ms-teams/<tenantId>/custom-emojis.json` whenever messages or custom reactions are read (or explicitly via `teams.py custom-emojis --scan <target>`), and `:shortcode:` is expanded into `<customemoji>` tags on send/reply/edit.
  - On **channel reactions** (`react` / `unreact`), Microsoft Graph's `setReaction` action only accepts standard Unicode emoji characters; custom organization reactions can be read (by `name`) and harvested for message bodies, but cannot be set as reactions via Graph.
- Chat reactions, cross-tenant search, presence, and file binary upload are not available through the
  connector passthrough.
- Creating a chat makes it visible to its members, and Teams only lets tenant admins delete chats. Creating
  a meeting with no attendees notifies nobody.
- Meeting transcripts, recordings, and Copilot AI insights only work for meetings the signed-in user
  is permitted to access (lookup of others' meetings returns 403).
- Full matrix with what was verified: `references/capabilities.md`.
