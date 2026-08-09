# GitHub starred-API contract probe

Probed live on 2026-07-26 against the user's public profile (example-user,
~288 stars) by the collecting Agent itself — no browser, no session, and no
credential exists anywhere in this pipeline. Frozen shapes live in
`tests/fixtures/github/` and are guarded by `tests/test_github_fixtures.py`.

## Contract findings

1. `GET https://api.github.com/users/<user>/starred?per_page=100&page=N` with
   header `Accept: application/vnd.github.star+json` returns an **array** of
   `{starred_at, repo}`; `starred_at` is the real bookmark timestamp (feeds
   `favorited_at` natively — the only platform so far with exact values).
2. Pagination is driven by the `Link` response header (`rel="next"` /
   `rel="last"`); newest star first. Unauthenticated budget is 60 requests
   per hour per IP; a full scan of ~288 stars costs ~3 API calls.
3. `repo` carries `full_name`, `html_url`, `description` (nullable),
   `language` (nullable), `topics[]`, `owner.login`, `default_branch`,
   `pushed_at`, `stargazers_count`, `archived`, `fork`.
4. READMEs come from the raw CDN (no API quota):
   `https://raw.githubusercontent.com/<full_name>/<default_branch>/README.md`,
   with fallbacks `readme.md`, `README.rst`, `README` on 404; missing README
   degrades to description-only content, never an item failure.
5. Error shapes: 403 with a rate-limit `message` (fixture synthetic — cannot
   be captured without exhausting the budget) → platform pause
   `rate_limited`; 404 `{"message": "Not Found", "status": "404"}` (live
   capture) → `source_unavailable` for the user scope; a non-array body →
   `page_changed`, never an empty success.
6. `page-changed.json` is synthetic by definition (hypothetical drift).

## Live collection evidence

Executed 2026-07-26 by the collecting Agent (zero user steps): 288 stars over
3 API pages, 286 READMEs from the raw CDN (2 missing, degraded to
description-only), all 288 ingested in idempotent 20-item batches with
status **completed** and the platform frontier established
(head WICG__html-in-canvas = newest star). Retrieval cites README chunks
(e.g. favhub:github/amingclawdev__toolBoxClient#chunk-25) and the native
starred_at feeds --favorited-since windows exactly. The library now holds
1894 items across bilibili, x, and github.
