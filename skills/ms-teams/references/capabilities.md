# Capabilities

Status legend: **verified** = exercised against a live tenant; *schema-backed* = built from the
Microsoft Learn connector reference / Graph docs.

## Read
| Feature | Command | How | Status |
|---|---|---|---|
| Joined teams, associated teams, team details | `teams`, `team-info` | connector operations (`/beta/me/joinedTeams`, `/v1.0/me/teamwork/associatedTeams`, `/beta/teams/{id}`) | **verified** |
| All channels in a team, channel details | `channels`, `channel-info` | connector operations (`/beta/teams/{id}/allChannels`, `/beta/teams/{id}/channels/{cid}`) | **verified** |
| Team members, direct channel members | `members`, `channel-members` | connector `/v1.0/teams/{id}/members` + Graph `.../channels/{cid}/members` | **verified** |
| Team tags and tag members | `tags`, `tag-members` | connector `/v1.0/teams/{id}/tags` and `.../tags/{tid}/members` | **verified** |
| Channel messages incl. `$expand=replies`, attachments, paging, `since` cut-off | `read`, `thread`, `message`, `search` | Graph passthrough | **verified** |
| Channel incremental delta sync (`@odata.deltaLink`) | `delta` | Graph `teams/{id}/channels/{cid}/messages/delta` | **verified** |
| Channel tabs, SharePoint `filesFolder` metadata | `tabs`, `files` | Graph `.../channels/{cid}/tabs?$expand=teamsApp`, `.../filesFolder` | **verified** |
| Installed Teams apps in a team | `apps` | Graph `teams/{id}/installedApps?$expand=teamsAppDefinition` | **verified** |
| Chats: list by type, find 1:1 chat by member, list chat members | `chats`, `find-chat`, `chat-members` | Graph `me/chats` and `me/chats/{id}/members` | **verified** |
| Read chat messages, self-chat (`48:notes`) | `read` | Graph `me/chats/{id}/messages` | **verified** |
| Teamwork sidebar sections and section items | `sections`, `section-items` | connector `/beta/me/teamwork/sections` and `.../items` | **verified** |
| Online meeting lookup, transcripts, recordings, ad-hoc calls, Copilot AI insights | `meeting`, `transcripts`, `transcript`, `recordings`, `recording`, `adhoc-calls`, `ai-insights` | dedicated connector operations (`/v1.0/me/onlineMeetings/...`, `/v1.0/me/adhocCalls/...`, `/v1.0/copilot/...`) | *schema-backed*; requires organizer/access rights (403 for others' meetings) |

## Write (all dry-run unless `--send`)
| Feature | Command | Status |
|---|---|---|
| Send message or Adaptive Card to chat, self-chat, or channel with `@mentions`, standard/custom `:shortcode:` emojis, and importance | `send`, `custom-emojis` | **verified** |
| Reply in a channel thread (HTML, `@mentions`, or Adaptive Card) | `reply` | **verified** |
| Edit own message or reply (`PATCH`) | `edit` | **verified** |
| Soft-delete own message or reply (`softDelete`) | `delete` | **verified** |
| Set / unset unicode reaction on channel messages and replies | `react`, `unreact` | **verified** |
| Create a 1:1 or group chat | `create-chat` | **verified** (`POST /beta/chats`); the connector adds you automatically, allows at most 20 other users and no guests. The chat is visible to its members; Teams only lets tenant admins delete chats. |
| Schedule an online Teams meeting on calendar | `create-meeting` | **verified** (`POST /v1.0/me/calendars/{calendarid}/events`); the required body is `isOnlineMeeting`, `onlineMeetingProvider`, `subject`, `body`, `timeZone`, `start.dateTime`, `end.dateTime`. With no attendees nobody is notified. Needs an Exchange Online mailbox when a time zone is selected. Deleting the event isn't exposed by the connector; use Outlook or Teams. |

## Not possible
- Reactions in **chats** and **pinned messages** (connector blocks `/chats/...`; `/me/chats/.../setReaction` and `pinnedMessages` return "not supported" / 404).
- **Custom** organization emoji reactions: Graph `setReaction` accepts unicode characters only.
- Cross-tenant search, presence, bare `/me`, file binary upload, or first segments other than
  `teams`, `me`, `users` through the Graph passthrough.
- **Destructive admin operations** (creating/archiving channels or teams, modifying membership or tags)
  are deliberately not exposed.

## Mention formats sent to Graph
See `mentions.md`.
