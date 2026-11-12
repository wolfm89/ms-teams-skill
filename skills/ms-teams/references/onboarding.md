# Onboarding: how Teams access is set up

Goal: read and send Teams messages from the command line without UI automation, without custom
Microsoft Graph admin consent, and without a required config file. `teams.py onboard` performs
these steps and `teams.py doctor` re-checks them. Both are idempotent.

## Why this route
- Microsoft Graph PowerShell / custom Graph app registrations: often blocked in enterprise tenants
  (Conditional Access `AADSTS53003` for device code; admin consent required for `Chat.ReadWrite` /
  `ChannelMessage.Send`). See `dead-ends.md`.
- The **Power Platform "Microsoft Teams" connector** (`shared_teams`) is a pre-authorized first-party
  Microsoft connector. A personal connection to it works with the user's own sign-in, and its
  `HttpRequest` operation passes Graph calls through (allowed first segments: `teams`, `me`, `users`).

## Zero-config discovery & multi-tenant caching
- On every run, `teams.py` requests an `https://apihub.azure.com` token from `az account get-access-token`
  (passing `--tenant <tenant>` if `--tenant` or `MS_TEAMS_TENANT` is set) and extracts your
  `tenantId` (`tid`), Entra user ID (`oid`), `displayName`, and `upn` directly from the JWT claims.
- All connection metadata (`session.json`), team/channel/member caches, and harvested custom emojis
  are stored per tenant under `~/.cache/ms-teams/<tenantId>/` (mode `0700`/`0600`).
- If a `Connected` Power Platform Teams connection already exists in `Default-<tenantId>`,
  `teams.py` discovers and caches its runtime URL automatically on the first command — you do not
  even need to run `onboard` first.

## Steps (what `onboard` does when creating or repairing a connection)
1. **az login & identity.** Checks your `az` token (`az login --allow-no-subscriptions` in the
   browser; device code is commonly blocked by Conditional Access).
2. **Environment.** `Default-<tenantId>` (the default Power Platform environment).
3. **Teams connection.** Lists connections of API `shared_teams` for the environment
   (`https://api.powerapps.com/providers/Microsoft.PowerApps/apis/shared_teams/connections`).
   - *Missing:* `onboard` attempts to create the connection automatically via
     `PUT .../apis/shared_teams/connections/<uuid>`. If automatic creation is restricted in the
     tenant, create one once in the browser at
     `https://make.powerautomate.com/environments/Default-<tenantId>/connections`
     (or open `make.powerautomate.com` -> **`... More`** in the left sidebar -> **Connections** ->
     **+ New connection** -> **Microsoft Teams**).
   - *Status not `Connected`* (e.g. newly created or expired): `POST .../connections/<id>/getConsentLink`
     with `{"redirectUrl":"https://global.consent.azure-apim.net/redirect"}`, open `consentLink` in the
     default browser (`os.startfile` on Windows, `open` on macOS, `powershell.exe Start-Process` on WSL,
     or `xdg-open` on Linux), sign in and approve, and wait until the status flips to `Connected`.
     The final page "did not receive an expected state query parameter" is harmless.
4. **Runtime URL & session cache.** Derived from the connection's `testLinks[0].requestUri`, cut at
   `/connections/<id>`:
   `https://<env>.environment.api.powerplatform.com/connectors/runtime/invoke/scalegroups/<region>/shards/<n>/apis/teams/connections/<id>`.
   Verified with a call to `/beta/me/teamwork` and cached in `~/.cache/ms-teams/<tenantId>/session.json`.
5. **Smoke test** (`--smoke`): posts one message with a real `@mention` to the user's own self-chat.

## Notes
- Tokens are never stored on disk by the skill.
- If the connection expires (status error, 401/403 from the runtime), run `teams.py onboard`.
