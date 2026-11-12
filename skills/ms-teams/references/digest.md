# Watchlist & Digest (`teams.py digest`)

`teams.py digest` scans watched channels plus your active 1:1, group, and meeting chats over a time
window (default `last-workday` from 00:00 local time) and emits compact JSON lines under 1900
characters each so a coding agent can read the entire digest in a single tool call.

## Three ways to run `digest`

1. **Chats only (zero config):**
   ```bash
   python3 <skill-dir>/scripts/teams.py digest --since last-workday
   ```
   If no `--channel` flags and no `watchlist.json` exist, `digest` summarizes all active 1:1, group,
   and meeting chats in the time window.
2. **Ad-hoc channels via `--channel` (zero config):**
   ```bash
   python3 <skill-dir>/scripts/teams.py digest \
     --channel "Core Engineering/General" \
     --channel "Core Engineering/Releases"
   ```
3. **Saved watchlist (`~/.config/ms-teams/watchlist.json`, or `--watchlist FILE` / `$MS_TEAMS_WATCHLIST`):**
   ```json
   {
     "watch": [
       {
         "group": "Engineering",
         "channels": [
           {
             "team": "Core Engineering",
             "names": ["General", "Releases", "Incidents"]
           }
         ],
         "subgroups": [
           {
             "name": "Architecture",
             "channels": [
               {
                 "team": "Core Engineering",
                 "names": ["RFCs", "Design Reviews"]
               }
             ]
           }
         ]
       }
     ]
   }
   ```

### Multi-tenant `watchlist.json`
If you use multiple tenants (`--tenant <tenantId_or_domain>`), you can scope watchlists per tenant in
the same `~/.config/ms-teams/watchlist.json` file:

```json
{
  "tenants": {
    "00000000-0000-0000-0000-000000000000": {
      "watch": [
        {
          "group": "Engineering",
          "channels": [{ "team": "Core Engineering", "names": ["General"] }]
        }
      ]
    }
  }
}
```

## Output JSONL record kinds

| `kind` | Description |
|---|---|
| `header` | `tenantId`, window metadata (`label`, `since`, `until`), `me`, `channelsChecked`, and any `errors` |
| `post` | Channel root message with `group`, optional `subgroup`, `team`, `channel`, `mentions`, `reactions`, `attachments`, and open-action hints (`open: "mention" \| "team"`) |
| `reply` | Reply inside the window, immediately following its parent `post` |
| `chat` | Active chat summary (`type`, `with` or `name`, `lastMessageByMe`, `open: "question" \| "check"`) |
| `chatmsg` | Up to the last 6 non-bot messages in the active chat above |
