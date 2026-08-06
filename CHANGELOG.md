# Changelog

## 1.1.0 — 2026-08-06

- The positional CLI argument now accepts either a LiveLib nickname or a numeric user ID.
- All-digit input such as `121955` bypasses `/reader/121955` and requests the user API directly.
- Added conflict validation when numeric positional input and `--user-id` disagree.
- Added truthful source metadata for ID-only exports; `username` is `null` in that mode.
- Bumped the normalized JSON schema to 1.1 and added profile identifier metadata.
- Added regression tests proving that numeric-ID mode never performs an HTML profile lookup.

## 1.0.2 — 2026-08-06

- Fixed the real LiveLib pagination behavior: links advertised as
  `https://beta.api.livelib.ru/api/...` are now rewritten to the working
  `https://beta.api.livelib.ru/trisolaris/api/...` endpoint before requesting.
- Added support for query-only pagination links resolved against the current page.
- Added a 667-record, 67-page regression test using the exact short pagination
  URL shape observed on the `k33p` export.
- Kept strict same-host, HTTPS, fragment, traversal, and API-path validation.

## 1.0.1 — 2026-08-06

- Fixed pagination for LiveLib responses that return next-page URLs under
  `https://beta.api.livelib.ru/api/...` instead of `/trisolaris/api/...`.
- Kept strict HTTPS, same-host, API-path, and fragment validation.
- Added regression tests for both observed pagination URL formats.

## 1.0.0 — 2026-08-06

- Added installable package and `livelib-export` console command.
- Added normalized five-point and ten-point ratings.
- Added stable export schema metadata.
- Added atomic file writes and overwrite protection.
- Added host/path validation for pagination URLs.
- Added Retry-After HTTP-date support and pagination safety limits.
- Added optional deduplication and sorting.
- Added library API, tests, CI, documentation, and MIT license.
