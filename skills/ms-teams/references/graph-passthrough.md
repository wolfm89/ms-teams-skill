# Graph passthrough (`HttpRequest`)

The connector operation `POST {runtime}/httprequest` forwards a Microsoft Graph call using the
connection's identity. The script wraps it as `Conn.graph`.

## Request shape
- Method of the HTTP call is always `POST` to `{runtime}/httprequest`.
- Headers: `Authorization: Bearer <apihub token>`, `Uri: <full Graph URL>`, `Method: GET|POST|PATCH|DELETE`,
  `ContentType: application/json` (required for every non-GET, even with an empty body: send `{}`).
- Body: the raw Graph JSON. Header values must be ASCII (URL-encode anything else).

## Restrictions
- Path must be `.../v1.0|beta/{teams|me|users}/...` and target `users/{id}` or one of the objects
  `channels, chats, installedApps, messages, pinnedMessages, onlineMeetings`.
  `/me` alone, `/chats/...`, `/search`, `/me/teamwork/sections` are rejected (400).
- Errors from Graph come back with their own status and `error.message`; errors starting with
  "URI path is not a valid Graph endpoint" come from the connector.
- `HTTP method value contains invalid characters` = a header was malformed.

## Behaviours worth knowing
- Paging: follow `@odata.nextLink` (a full Graph URL) as the next `Uri`. Pages with
  `$expand=members` hold about 10 chats, so page up to ~20 times to scan all one-on-one chats.
- Channel messages are ordered by last modification (replies bump the parent), so a time window
  cannot stop on `createdDateTime`; the script stops when a whole page is older than the window
  by `lastModifiedDateTime`, then filters on `createdDateTime`.
- `$top` max 50; with `$expand=replies` the script uses 20.
- `softDelete`, `setReaction`, `unsetReaction` are POSTs; edit is `PATCH` (204, no body).
- Deleted messages still appear in raw lists with `deletedDateTime`; `read` hides them.
- Dedicated connector operations (no `Uri` header) are used for teams, channels, members and tags,
  e.g. `GET {runtime}/beta/me/joinedTeams`, `/beta/teams/{id}/allChannels`, `/v1.0/teams/{id}/members`,
  `/v1.0/teams/{id}/tags`.
