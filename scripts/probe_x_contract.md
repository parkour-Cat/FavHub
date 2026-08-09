# X (bookmarks) response contract probe

Purpose: freeze the exact X Bookmarks GraphQL response *shapes* the pure
parsers must handle, captured from a real user-authenticated browser session
via **passive interception**, without touching any credential. Frozen shapes
live in `tests/fixtures/x/` and are guarded by `tests/test_x_fixtures.py`.

## Capture method (passive interception)

A console script hooks the page's own `fetch`/`XMLHttpRequest`, records the
**response bodies** of requests whose URL matches `/i/api/graphql/*/Bookmarks`,
and downloads them as one JSON file after the user scrolls the bookmarks
page. The script never reads `document.cookie`, never constructs or copies
auth headers (Bearer/csrf/ct0), and sends no requests of its own. A full page
reload removes the hook; navigation must stay inside the SPA.

## Provenance of the current fixtures

> **STATUS: LIVE-CAPTURED (2026-07-26), redacted/trimmed** for
> `bookmarks-page-1.json`, `bookmarks-page-2.json`, `tweet-with-images.json`,
> `tweet-with-quote.json`, and `tombstone.json` — 3 intercepted pages
> (20 tweet entries + top/bottom cursors each; 59 `Tweet` results and 1 real
> `TweetTombstone`). Page fixtures are trimmed to a few entries; entries are
> stored verbatim (public tweet content by public authors; the response body
> carries no viewer identity fields).
>
> Two fixtures are **synthetic by nature**:
>
> - `logged-out.json` — an `errors`-array envelope; capturing the real one
>   would require logging the user out.
> - `page-changed.json` — models hypothetical schema drift.

## Contract findings (fold into parsers)

1. **Author identity lives under `core.user_results.result.core`**
   (`name`, `screen_name`) — the user-`legacy` block no longer carries them.
2. **Long tweets**: `legacy.full_text` is truncated; the full text is at
   `note_tweet.note_tweet_results.result.text` and must take precedence.
3. **`ext_alt_text` is rarely present** (absent on every photo in this
   sample): image alt is strictly optional.
4. **Tombstones are real**: `tweet_results.result.__typename ==
   "TweetTombstone"` with a `tombstone.text` explanation → item-level
   `source_unavailable`, never a parse failure.
5. **Cursors**: every page carries `cursor-top-*`/`cursor-bottom-*` entries
   (`content.value`); end-of-list was not observed in this capture — the
   working assumption (to verify in the first full run) is that a terminal
   page returns cursors with zero `TimelineTimelineItem` entries.
6. Timestamps use the legacy format `"Sun Jul 26 08:10:25 +0000 2026"`.
7. Media: photos expose `media_url_https` + `type: "photo"`; videos/GIFs
   appear as `type: "video" | "animated_gif"` (poster URL only is captured;
   binaries are never downloaded).
8. **Bookmarks payloads carry no subtitle documents** for video tweets;
   fetching text subtitle tracks would require additional per-video
   endpoints outside the passive-interception envelope, so subtitle tracks
   are explicitly out of M3 scope (design §1 amendment, 2026-07-26).

## Redaction rules (must hold for every fixture)

- Never record request headers, `Cookie`/`Set-Cookie`, `Authorization`
  (Bearer), `x-csrf-token`/`ct0`, `auth_token`, or client-transaction ids —
  the contract test forbids these substrings in any fixture.
- Only response bodies are stored; the viewer's own identity must not appear
  (verified: Bookmarks bodies carry relational booleans, not viewer ids).
- Public tweet authors (name/handle/public mid) may stay.
- If a candidate entry's text contains a forbidden substring, pick a
  different entry rather than editing content.

## Live smoke evidence (2026-07-26)

Two read-only smoke runs against the user's real captured session data
(passive-interception captures fed through parsers → mapper → SyncGateway →
MCP → SQLite → ItemStore → index → retrieval; nothing in the account was
modified). All checks passed; reports `smoke-x-run1-report.json` /
`smoke-x-run2-report.json` in the session scratchpad.

- **Run 1 (full, capped at 60):** 3 pages parsed — 59 tweets + 1 real
  tombstone (copyright-withheld post), 17 with photos, 11 with quotes; all
  60 ingested in 3 idempotent batches; pause (`rate_limited`) then replay of
  batch `b-0000` returned the identical receipt; the collecting Agent viewed
  one bookmarked image and its OCR/visual description was persisted as
  `ocr/0001.md` and cited by FTS search
  (`favhub:x/1232164438310380159#chunk-11`); a visible quoted tweet was
  inlined in `content.md`; the tombstone kept metadata with
  `source_unavailable` + estimated-timestamp flag; MCP status reported
  `partial`, `max_scan_reached`, empty `scopes`; the scan-capped run
  correctly withheld the platform frontier.
- **Run 2 (incremental, after one new user bookmark):** the fresh bookmark
  was discovered at the head of the stream and ingested with
  `added=1, duplicates=60, refreshed=0`; previously stored items were
  byte-identical afterwards; totals 60 → 61; the new item was indexed.
- **Capture observation:** re-installing the hook on an already-visited SPA
  records only the requests fired after installation (the second capture
  contained a single delta page) — the Skill's instruction to install the
  hook *before* navigating to the bookmarks page is load-bearing.
- **Not observed live:** an uncapped observable-end run (so no frontier has
  been persisted yet); text subtitle tracks (out of scope per design §1
  amendment).

On this evidence the X bookmarks connector is considered **live-verified**
for the M3 scope (small-scan smoke). Full collection follows the Skill's
smoke-first workflow.
