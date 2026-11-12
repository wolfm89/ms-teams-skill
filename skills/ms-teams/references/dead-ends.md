# Dead ends (do not retry)

| Approach | Typical result in locked-down enterprise tenants |
|---|---|
| Graph PowerShell `Connect-MgGraph` interactive (from WSL / headless) | Fails: "A window handle must be configured" (WAM) |
| Graph PowerShell / custom client, **device code** flow | Often blocked by Conditional Access (`AADSTS53003`) |
| Graph PowerShell / CLI client, **auth code + PKCE** in the browser | Sign-in passes Conditional Access, then stops at **"Need admin approval"**: user consent is restricted and no third-party/CLI app has `Chat.*` / `ChannelMessage.Send` grants |
| Azure CLI token for Graph (`az account get-access-token --resource https://graph.microsoft.com`) | Scopes are directory/group only; no chat or channel message scopes |
| Teams local third-party API (`ws://127.0.0.1:8124`) | Retired by Microsoft (Message Center MC1266901); nothing listens; never supported sending messages |
| Teams deep link `msteams:/l/chat/0/0?users=..&message=..` + `SendKeys` | Brittle UI automation: requires window focus, fixed sleeps, and prefill is ignored when the target chat is already open |
| Microsoft "Work IQ Teams" MCP server (`mcp_TeamsServer`) | Requires tenant admin governance and Copilot licensing |
| Standard Graph-based Teams MCP servers (e.g. `floriscornel/teams-mcp`) | Require admin-consented Graph permissions: hits the same consent wall |
| Power Automate Workflows webhook | Works only for one fixed chat/channel as the Workflows bot; requires manual flow setup per target |
| Reactions in chats | `/chats/...` is outside the connector's allowed first segments; `/me/chats/.../setReaction` returns "not supported" |
| Custom organization emoji reactions | Graph `setReaction` accepts standard unicode emoji only |
| `<at>name</at>`, `<at>id</at>`, `<atChannel>`, `<atTeam>` tokens in the connector's `PostMessageToConversation` operation | Channel/team tokens are stripped to plain text; use the Graph passthrough with a `mentions` array instead |
| Extra `mentions` property on the connector's `PostMessageToConversation` operation | Ignored by the connector |
| Channel/team mentions in a personal chat | Not supported by Teams; test channel mentions in a channel |

If the Power Platform connection itself breaks or expires, run `teams.py onboard` instead of looking
for another route.
