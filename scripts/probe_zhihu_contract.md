# Zhihu collections-API contract probe

Probed live on 2026-07-26 via a user-run console script on a logged-in
zhihu.com page (same-origin credentialed GETs, response bodies only — no
cookie, token, or request header ever left the page or entered any output).
23 collections observed. Frozen, redacted shapes live in
`tests/fixtures/zhihu/` and are guarded by `tests/test_zhihu_fixtures.py`.

## Contract findings

1. `GET /api/v4/me` → `url_token` (script-internal only; never persisted to
   fixtures). `GET /api/v4/people/<url_token>/collections?offset=&limit=20`
   → `data[]` of `{id (int), title, item_count, is_default, created_time}`
   with `paging.is_end` as the end signal.
2. `GET /api/v4/collections/<id>/items?offset=&limit=20` → `data[]` where
   each element has exactly `created` (ISO8601 with `+08:00` — the **real
   favorited time**, feeding `favorited_at` natively, no estimate flag) and
   a polymorphic `content` keyed by `type`.
3. `type == "answer"`: full HTML body in `content` (200B–11KB observed);
   note `question.id` arrives as a **decimal string** while collection ids
   are ints — parsers accept both,
   `question.{id,title,url}`, `author.name`, `voteup_count`, epoch-second
   `created_time`/`updated_time`, answer URL under /question/…/answer/….
4. `type == "article"`: own `title`, `url` (zhuanlan.zhihu.com/p/…), full
   HTML `content`, `image_url`, epoch-second `created`/`updated` — note the
   key names differ from answer's `created_time`/`updated_time`.
5. **`totals` is unreliable**: deleted/hidden favorites shrink pages below
   the limit on non-final pages (observed 19/20 with totals=104 and 15/18
   with is_end=true). Only `paging.is_end` terminates a scan; a short page
   is NOT an end signal.
6. Body HTML uses a small clean tag set (`p a b code pre ol ul li div span
   img figure br`); images are public zhimg.com CDN URLs.
7. **Video answers** (observed in the full capture): `type == "answer"`
   with a legitimately EMPTY `content` string and the video under
   `attachment {type: "VIDEO", video.title}` — parsed with empty html and
   the video title carried into a `## 视频` body section, never a page
   failure. Other content types (pin/zvideo) did not appear in probe pages; the
   `item-unknown-type.json` fixture is synthetic and drives the degrade
   path (keep type + best-effort title/excerpt/url, never fail the page).
8. Error envelopes are synthetic by necessity (cannot be captured without
   logging out or tripping rate limits): Zhihu's documented `{"error":
   {message, code}}` shape. Parsers map primarily by `code` (100/101 →
   `login_required`, 4039 → `rate_limited`) with message/name keywords as
   fallback; any non-list `data` → `page_changed`, and a missing or
   non-boolean `paging.is_end` is `page_changed` too — never a quiet end,
   never an empty success.

## Live collection evidence

Executed 2026-07-26 late night from the user's full console capture
(favhub-zhihu-full.js, all folders scanned to `paging.is_end`): 23
collections, 518 raw entries (10 fewer than the declared item_count sum —
deleted favorites, as predicted), 490 unique items after cross-folder
dedup (28 duplicates collapsed, earliest favorited_at kept), including 3
degraded zvideo entries and 1 video answer (empty body + VIDEO attachment,
discovered live and folded back into the parser). Ingested through the MCP
gateway in idempotent 20-item batches: added=490, platform and all 23
scopes **completed**, per-folder frontiers established (verified via an
incremental restart), `favorited_at` column lifted for all 490 (range
2012-07-24 .. 2026-07-12), FTS reindexed and retrieval cites zhihu chunks
(e.g. favhub:zhihu/answer-919510352#chunk-1) with favorited-window queries
working. The library now holds 2384 items across four platforms.
