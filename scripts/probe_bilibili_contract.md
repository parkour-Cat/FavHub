# Bilibili response contract probe

Purpose: freeze the exact Bilibili response *shapes* the pure parsers must
handle, captured from a real user-authenticated browser session, **without**
capturing any credential. The frozen shapes live in `tests/fixtures/bilibili/`
and are guarded by `tests/test_bilibili_fixtures.py`.

## Provenance of the current fixtures

> **STATUS: LIVE-CAPTURED (2026-07-26), redacted** for `folders.json`,
> `resources-page-1.json`, `video-detail.json`, and `subtitle.json` — copied
> from a user-authenticated browser session via a same-origin, read-only
> console probe (response bodies only; the user's own `mid`, display name,
> and avatar replaced with placeholders; `folders.json` trimmed to two
> entries with `count` adjusted).
>
> Two fixtures remain **synthetic by nature**:
>
> - `login-required.json` — capturing a real `-101` would require logging the
>   user out. Probe observation: `fav/folder/created/list-all` *without*
>   credentials returns `code: 0` for public folders, so logged-out detection
>   must rely on endpoints that do fail closed (e.g. `x/web-interface/nav`).
> - `page-changed.json` — models a hypothetical future schema drift, which by
>   definition cannot be captured from the current live schema.
>
> Live-probe contract findings folded back into the parsers:
> subtitle *documents* carry their language under `lang` (the player track
> metadata uses `lan`); one live subtitle URL returned an HTML page instead
> of JSON, confirming the `malformed_subtitle` path occurs in practice.

## Redaction rules (must hold for every fixture)

Never record, and strip before saving:

- `Cookie` / `Set-Cookie` request or response headers.
- Credential fields: `SESSDATA`, `bili_jct`, `DedeUserID`, `DedeUserID__ckMd5`,
  `buvid3`/`buvid4`, `access_key`, and any `csrf` token.
- `Authorization` headers and any full request-header dump.
- Browser debug endpoints (`devtools`, remote debugging ports) and stack traces.
- Personal identity beyond public author names (avoid the logged-in user's own
  `mid`; author `upper.mid` is public metadata and may stay).

`tests/test_bilibili_fixtures.py` enforces that none of the above substrings
appear in any fixture and that each fixture is a JSON object.

## What each fixture models (endpoint → file)

| Fixture | Real source shape | Notes |
| --- | --- | --- |
| `folders.json` | `GET /x/v3/fav/folder/created/list-all?up_mid=<mid>` | `data.list[]` folder identity (`id`), `title`, `media_count`. Two folders to exercise all-folder enumeration and cross-folder dedup. |
| `resources-page-1.json` | `GET /x/v3/fav/resource/list?media_id=<id>&pn=1&ps=20&order=mtime` | `data.medias[]` list entries with `bvid`, `title`, `upper`, `pubtime`, `intro`; `data.has_more` pagination flag. |
| `video-detail.json` | `GET /x/web-interface/view?bvid=<bvid>` | `data` title/owner/`pubdate`/`desc`/`cid`/`pages`. |
| `subtitle.json` | player subtitle track + AI-subtitle document (`data.body[]`) | `lan`/`lan_doc` plus timestamped `body[]` cues; includes a duplicate cue and an out-of-order cue to exercise dedup/sort. |
| `login-required.json` | any API path when the session is logged out | `code: -101, "账号未登录"` — must map to `login_required`, never an empty success. |
| `page-changed.json` | folder-list shape after an incompatible schema change | `data.folders[]` with renamed keys and no `id`/`title` — must map to `page_changed`. |

## Live smoke evidence (2026-07-26)

Two read-only smoke runs against the user's authenticated session data
(console-probe captures fed through the real parser → SyncGateway → MCP →
SQLite → ItemStore → index → retrieval stack; nothing in the account was
modified). All checks passed; reports: `smoke-run1-report.json` /
`smoke-run2-report.json` in the session scratchpad.

- **Run 1 (full, capped):** 25/25 folders enumerated as independent scopes;
  8 unique videos ingested from 2 sampled folders (scanned 5+3); 7 subtitle
  documents persisted as transcript + raw asset; 1 live `malformed_subtitle`
  (subtitle URL served HTML) kept metadata with the stable code; pause
  (`rate_limited`) left 0 frontier rows; resume replayed batch `b-0000` to
  the identical receipt; per-folder `scanned`/`visible_total`/
  `max_scan_reached` reported over real MCP JSON-RPC; FTS search cited
  `favhub:bilibili/BV1bkz2gvaz6#chunk-2` at
  `items/bilibili/BV1bkz2gvaz6/content.md`.
- **Run 2 (incremental, after one new user favorite):** the new video was
  discovered at the head of 默认收藏夹 (`media_count` 597 → 598), ingested
  with `added=1, duplicates=7, refreshed=0`; previously stored items were
  byte-identical afterwards; total items 8 → 9; the new item was indexed.
- **Not observed live:** a cross-folder duplicate (no video appeared in two
  sampled folders in this account's page heads); the union behavior is
  covered by unit and fake-browser integration tests. Frontier advancement
  was intentionally withheld in both runs because every sampled folder was
  scan-capped (`max_scan_reached`), matching the frontier-only-at-terminal
  rule; a full uncapped run is required before frontiers persist.

On this evidence the Bilibili connector is considered **live-verified** for
the M2C scope (small-scan smoke). Full-folder collection still follows the
Skill's smoke-first workflow.

## Manual live-probe checklist (user-driven, run in the real session)

1. Open bilibili.com already logged in; navigate to the favorites page.
2. In DevTools → Network, filter to `api.bilibili.com` XHR/fetch.
3. For each row above, copy the **response body** only (never the request
   headers / cookies), apply the redaction rules, and overwrite the matching
   fixture file, preserving field shape.
4. Re-run `pytest tests/test_bilibili_fixtures.py -q` — it must stay green.
5. If a required response cannot be read from the session (e.g. it is served
   as login HTML rather than JSON), record the observed failure here and stop
   before claiming connector support.
