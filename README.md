# FavHub

English · [简体中文](README.zh-CN.md)

[![CI](https://github.com/parkour-Cat/FavHub/actions/workflows/ci.yml/badge.svg)](https://github.com/parkour-Cat/FavHub/actions/workflows/ci.yml)

FavHub is a local knowledge mirror for the things you saved and never read again. It
collects your Bilibili favourites, X bookmarks, Zhihu collections and GitHub stars into
a plain-file library on your own machine, indexes them for search, and exposes them to a
coding Agent over MCP so you can ask questions of your own collection instead of
scrolling it.

Three properties are load-bearing:

- **FavHub never sees a credential.** Collection happens inside your own logged-in browser
  through a Chrome extension. Cookies, tokens and request headers are never read,
  constructed, exported, or sent anywhere. The GitHub connector uses a public endpoint.
- **FavHub makes no network calls of its own** except to GitHub's public API. Summaries are
  written by the calling Agent's model; FavHub validates and stores them.
- **The files are the library.** `items/` is the fact source; SQLite holds derived index
  data that can be rebuilt from the files at any time.


## Install

```powershell
uv tool install "favhub[embedding]"
favhub setup
favhub doctor
```

`favhub setup` writes the data root, installs the Chrome extension files, registers the
Native Messaging host, and installs the Agent Skills. `favhub doctor` verifies the pinned
extension id, the native host manifest, the registry entry and the pipe handshake, naming
which one is broken rather than reporting a general failure.

### Load the extension (first install only)

Chrome cannot install an unpacked extension on a program's behalf, so this part is manual:

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and choose the directory `favhub setup` printed
   (`%LOCALAPPDATA%\FavHub\extension`).

The extension key is pinned, so the id survives reinstalls and the Native Messaging
allowlist only ever names one id.

### Upgrading

```powershell
uv tool upgrade favhub
favhub setup
```

**Then press Reload on the FavHub Collector card in `chrome://extensions`.** Chrome keeps
running the old copy until you do. FavHub compares the version Chrome reports against the
version it installed and *refuses to collect* when they differ, so a build that was never
reloaded fails with a version mismatch instead of collecting with last version's code.

On Windows an upgrade cannot replace `favhub-mcp.exe` while a FavHub is running. Close the
Agent window first; the CLI will otherwise tell you which process id is holding the root.

### Removing the browser integration

```powershell
favhub uninstall-browser
```

Removes the installed extension files, the Native Messaging manifest and its registry entry.
Your data root is untouched. Chrome still lists the extension until you remove it there too.

## Collecting

Three platforms are collected through the extension in your own browser. GitHub needs no
browser at all. Each connector has an Agent Skill that orchestrates the run; you ask your
Agent to sync, and it drives the tools.

| Platform | How it reads | Scoped by folder |
| --- | --- | --- |
| Bilibili | extension, active same-origin GETs | yes, per 收藏夹 |
| Zhihu | extension, active same-origin GETs | yes, per 收藏夹 |
| X | extension, passive interception of the page's own requests | no, one bookmark list |
| GitHub | FavHub calls the public starred API directly | no, one star list |

X is the exception because its GraphQL endpoints authenticate through request headers.
Rather than issue any request of its own, the extension hooks the page's own fetch and
records the response bodies — and drives the page to make those requests by scrolling the
bookmarks list itself. Passive describes the requests, not the scrolling: you do not scroll,
and you do not have to keep the tab in front, which is why the scroll is instant rather than
smooth (Chrome pauses the animation frames a smooth scroll needs in a hidden tab).

**Smoke first.** Every Skill runs a small `maxScanItems` pass in the real logged-in browser
before a full run. Fixture imports and the fake-browser tests are not a connector and prove
nothing about the live platform.

Key semantics that survive across connectors:

- **Per-scope incremental sync.** Each folder keeps its own frontier in
  `sync_frontier_scopes`; folders stop, pause and advance independently, and renaming a
  folder does not reset its frontier. A folder cut short by `maxScanItems` does not advance
  at all, so the next run rescans what it never confirmed.
- **Publication-time filters never stop pagination early.** Folder order is not publication
  order, so out-of-range items are counted and scanning continues.
- **Cross-folder dedup.** An item saved in several folders is captured once with every
  folder name unioned into `collections`.
- **`notes.md` is yours.** It is created once per item and never overwritten by any sync,
  enrichment or repair path.
- **Incremental never rewrites what it already has.** An item already in the library counts
  as a duplicate without comparing content. Use `mode: full` to refresh stored items.

```powershell
favhub github-sync --user <login> --mode incremental
```

An optional `FAVHUB_GITHUB_TOKEN` lifts the rate limit from 60 requests an hour per IP to
5,000 against that account. It is read at request time, stored nowhere, attached only to
api.github.com, and never appears in a result or an error — results report only
`authenticated: true|false`. Collection stays public-stars-only either way; a credential
raises the rate limit and does not widen what is collected.

### A platform quirk worth knowing

A Bilibili collection is sometimes offered a **transcript belonging to a different video**.
It is intermittent: the same video can yield a correct transcript on one run and another
video's on the next.

The request is not what goes wrong. The detail response supplies the title, the duration and
the cid together, and for every refusal recorded here the stored title and duration match the
real video exactly — so the cid sent with them was that video's too. The foreign transcript
therefore comes back in the player response. What is still open is whether that response
offered only the foreign track, or offered the right one beside it and this adapter's
preference for a human-authored track picked the wrong one.

The check does not depend on knowing. Bilibili names transcript objects
`<aid><cid><hash>`, so the object states which video it belongs to, and FavHub refuses one
that does not name the video being collected before downloading it. A refusal is recorded as
`subtitle_status: "wrong_video"` with the offered name in `subtitle_offered`, so it is
distinguishable from a video that simply has no transcript, and it never replaces a
transcript already held. The accepted object's name is kept in `subtitle_source`, which is
the only way to answer "whose words are these?" about a transcript already written down.

## Enriching: summaries, tags, content types

Summaries and tags come from the calling Agent's model. Ingest enqueues a durable
`summarize` task per item keyed by the capture `content_hash`, and
[`skills/favhub-enrich`](skills/favhub-enrich/SKILL.md) drives the pull → generate → submit
loop over `favhub.enrich_next`, `favhub.enrich_submit` and `favhub.enrich_skip`.

**Generation must run on a cheap-tier model.** Writing three sentences about a page you were
handed does not need the orchestrating model's price, and the Skill states this by tier
rather than by model name so both Claude Code and Codex can act on it. The `model` field must
record what actually generated the text — that honesty is what makes
`favhub enrich-redo --model <name>` able to requeue exactly the batch that went wrong.

Four rules are enforced by the server rather than merely requested of the Skill:

- A summary must be **shorter than what it summarises** (over a 200-character body).
- Chinese content needs **at least one tag containing Chinese characters**. A model asked for
  tags on Chinese text will sometimes transliterate rather than translate, and a
  transliterated tag is not something anyone would search for.
- An item whose body is **empty once URLs and the media list are removed** cannot be given a
  summary at all. The answer there is to decline it, not to describe the link.
- Rejections are returned **in words naming the rule that was broken**, so a caller can
  correct the submission rather than guess at which field was wrong.

The two skip codes differ in consequence. `generation_failed` returns the task to pending for
another attempt; `content_unsupported` declines it permanently. Changed content enqueues a
fresh task on its own, so declining is not a life sentence.

Results live in the `source.json` `enrichment` block (excluded from `content_hash`), render
into `content.md` as a `## 摘要` section plus a tag line — both FTS-searchable — and update
`items.content_type` so the content-type filter works. Applying an enrichment indexes that
item immediately, so a summary is searchable the moment it lands.

```powershell
favhub --root data enrich-backfill              # queue items collected before enrichment existed
favhub --root data enrich-redo --model <name>   # requeue everything one model wrote
favhub --root data enrich-redo --declined       # requeue every declined task
```

## Asking

The user-facing flow is a question, not a search box:

```text
question -> item-level search -> read the strongest full items -> evidence matrix -> cited answer
```

The Agent reads the full content of the best-matching saved items and writes a direct answer
with citations, following [`skills/favhub-ask`](skills/favhub-ask/SKILL.md). FavHub itself
calls no LLM; it provides search, retrieval, evidence fields and stable citations.

That answer must distinguish evidence from your saved items, from current external sources,
and from the Agent's own inference. A personal collection can be incomplete or years out of
date, so changing facts deserve a current check rather than being repeated because they were
once bookmarked.

```powershell
favhub --root data search "<your question>"
favhub --root data search "<query>" --platform zhihu --collection "<folder>" --limit 20
favhub --root data get-item x <tweet-id> --include-content
```

Search limits count unique saved items, not matching chunks. Each result carries one primary
citation and up to three supporting chunks, plus an `evidence_level` of `title_only`, `body`,
`transcript`, `ocr` or `mixed`. `title_only` results stay available as lower-ranked leads
rather than content evidence, and say so.

Published-time and favorited-time bounds are separate filters. Favorited time is when the
item entered your collection — real for Bilibili, Zhihu and GitHub, estimated from first-sync
time for X, which says so. Items with no known favorited time are excluded while that filter
is active.

For several queries in a row, `search-batch` keeps one process open so the embedding provider
stays loaded between them:

```powershell
favhub --root data search-batch --query "<first>" --query "<second>" --retrieval-mode hybrid
```

### Retrieval modes

`--retrieval-mode` selects the path:

- **`auto`** (default) runs hybrid retrieval and degrades gracefully to lexical with
  diagnostics when embeddings are unavailable.
- **`fts`** is lexical-only, for diagnostics. It loads no embeddings.
- **`hybrid`** is strict: it errors rather than silently falling back.

Use `auto`. Lexical search alone frequently returns **nothing at all** for a conversational
Chinese question, because FTS tokenization only matches when the query's words appear
literally in the text. Semantic retrieval is what makes a question-shaped query work. The
practical consequence: if the embedding index breaks, search does not error — it quietly
becomes an engine that answers such questions with silence, which is why `auto` reports the
mode and any fallback warning it used.

### Your own folders

```powershell
favhub --root data collections
```

Folders, largest first, counting only items the platform still serves, with a per-platform
count of how many items no folder describes. A named folder was a deliberate act and marks a
topic worth consulting; a platform's default folder collects one-click saves and marks
nothing. The `unfiled` count exists because folders are a thing only some platforms have —
GitHub stars and X bookmarks are flat lists, so reading the folder list as the whole library
would hide them entirely. `favhub.collections` exposes the same map over MCP, and the
favhub-ask Skill reads it to decide whether searching is worth the latency at all.

## Optional local semantic retrieval

```powershell
uv sync --extra embedding
favhub --root data embeddings init
favhub --root data embeddings build
```

Initialization is the only path allowed to download a model; build loads local-only. The
default is `intfloat/multilingual-e5-small` (MIT), cached below `<data-root>/models/`.
`--max-items` bounds attempted work, `--force` rebuilds active-profile vectors without
touching source snapshots or notes, and `--quiet` suppresses the progress heartbeat.

Vectors are normalized 384-dimensional float32 BLOBs in SQLite. Queries exact-scan eligible
vectors and fuse vector and FTS ranks with equal-weight RRF. This suits personal-library
scale and avoids running a vector service. There is no remote embedding API, no telemetry and
no content-export path; embedded content stays on the machine.

```powershell
uv run python scripts/benchmark_m2b.py
```

reports scan and fusion times at 1,000/10,000/50,000 chunks with memory and platform
metadata. It describes the current machine, not a cross-hardware guarantee.

## MCP over stdio

```powershell
uv run favhub-mcp --root data
```

Newline-delimited JSON-RPC on stdin/stdout. Clients must `initialize`, send
`notifications/initialized`, then call tools. Arguments are camelCase, results snake_case; no
credentials, local paths or arbitrary URLs are accepted.

| Group | Tools |
| --- | --- |
| Retrieval | `favhub.search`, `favhub.get_item`, `favhub.collections`, `favhub.status` |
| Browser collection | `favhub.browser_start`, `favhub.browser_status`, `favhub.browser_resume`, `favhub.browser_cancel` |
| Capture ingest | `favhub.sync_start`, `favhub.sync_submit_batch`, `favhub.sync_pause`, `favhub.sync_finish`, `favhub.sync_status` |
| GitHub | `favhub.github_sync` |
| Enrichment | `favhub.enrich_next`, `favhub.enrich_submit`, `favhub.enrich_skip` |

The adapter is a stdio process, not an HTTP service or a resident daemon. It is the right
interface for conversational use because the local embedding provider stays warm in the same
process. Embedding setup remains lazy: nothing loads at startup, and the first semantic query
loads what it needs.

### One FavHub per data root

A running FavHub holds an exclusive lock on its data root for its whole lifetime. That makes
the MCP tools and the CLI alternatives rather than companions: use the MCP tool while an
Agent window is open, and the CLI when it is not. A CLI command turned away reports the
process id holding the root, which is normally that Agent window.

## Storage and citations

```text
items/<platform>/<source-id>/
  source.json          capture snapshot, plus the enrichment block
  content.md           system-generated, indexed
  transcript/0001.md   Bilibili subtitles, indexed
  ocr/NNNN.md          X image descriptions, indexed
  assets/              raw responses, never indexed
  notes.md             yours, created once, never overwritten
```

Search `local_path` values are data-root-relative (`items/x/2081.../content.md`), never
machine-specific absolute paths. Citations are stable identities of the form
`favhub:<platform>/<source_id>#chunk-<ordinal>`.

Files a capture no longer produces are removed, but only within the asset roots that capture
declares it knows the full contents of. A Bilibili capture always attempts a transcript, so
producing none is a finding about the video; an X sync produces image descriptions only when
it was asked to, so producing none means nothing at all. Emptiness cannot tell those apart,
which is why the mapper declares it rather than the storage layer guessing.

## Index work and status

```powershell
favhub --root data reindex          # enqueue missing index work
favhub --root data reindex --force  # push every current snapshot back through indexing
favhub --root data status           # index and embedding state
favhub --root data status JOB_ID    # one sync job's progress
```

There is no background daemon. Enrichment indexes what it rewrites inline, and
`embeddings build` drains both index and embedding queues; anything else accumulates until
one of those runs. `pending_index_tasks` counts pending and running work; `failed_index_tasks`
counts tasks carrying a recorded error, and since failures stay pending for retry the two
counts overlap.

Roots restored from a backup or collected before a feature existed can be lifted forward:

```powershell
favhub --root data favtime-backfill
favhub --root data collections-backfill
favhub --root data access-backfill
```

## Development

```powershell
uv sync --all-extras
```

The extras are not optional for development. Semantic retrieval imports numpy at
the point of use and the suite covers that path, so an environment without them
fails the gate on a missing module rather than on anything you changed. The dev
group installs by default and needs no flag.

The full gate before merging:

```powershell
uv run pytest --cov
npm run test:extension
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv lock --check
git diff --check
```

`--cov` fails below 90% overall, configured in `pyproject.toml` rather than left to whoever
remembers to read the number. It is deliberately not in `addopts`: a focused run like
`pytest tests/test_chunking.py` would measure one file against the whole package and fail a
floor it was never trying to meet. Individual modules vary — `browser_launcher` is lowest
because it starts a real Chrome — and a module well below the floor is a release concern to
report rather than hide.

`scripts/smoke_browser_extension.ps1` launches Chrome with a throwaway profile under an
explicit temporary root, so a smoke run never touches your real profile. It supplies no
credentials and bypasses no login: sign in by hand in that profile, exactly as a user would.

Fixture import is a deterministic demo and test path, not a connector:

```powershell
uv run favhub --root .tmp/favhub import-fixture tests/fixtures/m2a-captured-items.json --mode full
```

It passes through the same `SyncModule` and `LibraryModule` as a real capture and proves
nothing about any platform's live API, authentication, or pages.

## Out of scope

FavHub does not do OCR or ASR itself, download video, cover or image binaries, collect
comments or danmaku, index code, read private stars, follow external links, run a GUI, run a
daemon, schedule anything automatically, or cluster items across the library. Tags are free
text with no controlled vocabulary. Image descriptions and subtitles are indexed only when an
upstream collector already supplied them.
