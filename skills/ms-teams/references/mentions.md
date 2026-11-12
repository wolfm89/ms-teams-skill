# Mentions

Write mentions in the HTML body as `@[spec]`; `teams.py` replaces each with `<at id="N">Name</at>`
and adds the matching entry to the Graph `mentions` array. The same entity used twice reuses one id.

| Spec | Resolution | Graph `mentioned` entry | Status |
|---|---|---|---|
| `@[Name]`, `@[user:Name]`, `@[email]` | cached team members first, then `users?$search` in the tenant; must be unique | `{"user":{"id","displayName","userIdentityType":"aadUser"}}` | verified (chat & channel) |
| `@[tag:Name]` | team tags of the target channel's team | `{"tag":{"id","displayName"}}` | channel posts only |
| `@[channel:ChannelName]` or `@[channel:Team/Channel]` | channel name in the target's team, or `Team/Channel` | `{"conversation":{"id","displayName","conversationIdentityType":"channel"}}` | channel posts only |
| `@[team]` | the target channel's team | `{"conversation":{"id":<teamId>,"displayName":<teamName>,"conversationIdentityType":"team"}}` | channel posts only |

## Rules and findings
- Channel, tag and team mentions are **not possible in chats** (Teams limitation); the script
  refuses them there.
- The connector's own "post message" operation cannot create channel mentions (it only converts
  `<at>email</at>` for users). That is why `send` uses the Graph passthrough with an explicit
  `mentions` array.
- After every send the response is compared with the request: `mentionsResolved` /
  `mentionsMissing`. Missing mentions mean the post went out without that ping. Offer the user the
  undo command printed by the script.
- Ambiguous names (several matches) and unknown names abort with candidates. Use the full name
  or email.
- Mentioning a channel notifies that channel's followers; mentioning a tag notifies tag members.
  Think about who gets pinged before sending to a shared channel.
- Display names can contain commas or department suffixes (e.g. `Lovelace Ada, ENG-42`); resolution
  handles them, but the safest spec is the email.
