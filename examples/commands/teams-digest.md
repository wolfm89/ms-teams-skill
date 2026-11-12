---
description: Structured overview of what happened in your watched Teams channels and chats since the last working day, with open items for you. Usage - /teams-digest [today|yesterday|3d|YYYY-MM-DD] [--no-chats]
---

# Teams digest

Read-only overview of your watched channels (`~/.config/ms-teams/watchlist.json`, if configured) plus
your 1:1, group and meeting chats. Use the **`ms-teams` skill**. Do not post, react, edit or delete
anything.

```bash
python3 <skill-dir>/scripts/teams.py <command> ...
```

Arguments (may be empty): $ARGUMENTS

- A window word or date becomes `--since <it>`. The default is `last-workday`: from 00:00 local time of the last working day (Mon-Fri) until now.
- `--no-chats` skips the chats; `--no-meeting-chats` skips only the meeting chats.

## Process

1. Pick a file in the system temp directory (`/tmp` on Linux, macOS and WSL, `%TEMP%` on native Windows) and call it `<tmp>/teams-digest.jsonl`.
   Run `teams.py digest --out <tmp>/teams-digest.jsonl [--since <window>] [--no-chats] [--no-meeting-chats]`.
   If it fails with a connection or authentication error, run `teams.py doctor` and `teams.py onboard`.
2. Read `<tmp>/teams-digest.jsonl` once with the `read` tool, without `limit` (every line is kept under 1900 characters).
3. After reading, remove `<tmp>/teams-digest.jsonl` in a subsequent step.
4. Summarize activity per group and chat:
   - Start with the window label and a summary line of open items (`chat asks`, `direct mentions`, `team/channel mentions`).
   - Under each group, show an **Open for me** table (`What | From | When | Next step`) followed by a concise **Other** summary of channel threads and discussions.
   - Leave out any section or table that has no activity in the window.
