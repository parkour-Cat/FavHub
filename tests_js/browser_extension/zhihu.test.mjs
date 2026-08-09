// Zhihu is active-mode, like Bilibili: it issues its own same-origin GETs
// rather than watching the page. The rule that earns its own tests here is the
// end signal — `paging.is_end` is the only one, and a page shorter than the
// limit is *not* the end, because deleted favourites shrink pages in the middle
// of a collection. Treating a short page as terminal would silently truncate a
// scan and then advance a frontier past everything it never saw.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInThisContext } from "node:vm";

import {
  COLLECTIONS_PATH,
  ITEMS_PATH,
  ME_ENDPOINT,
  PAGE_SIZE,
  REQUEST_INTERVAL_MS,
  collectionsUrl,
  createZhihuAdapter,
  isCollectionsRoute,
  itemsUrl,
  readCollectionsPage,
  readEnvelope,
  readItemsPage,
  sourceIdOf,
} from "../../src/favhub/browser_extension/adapters/zhihu.js";

runInThisContext(
  readFileSync(new URL("../../src/favhub/browser_extension/bridge.js", import.meta.url), "utf8"),
);
const { isAllowedRequest, sendsCredentials } = globalThis.FavHubBridge;

const TOKEN = "some-user";

function answer(id, created = 1_700_000_000) {
  return {
    created,
    content: {
      type: "answer",
      id,
      url: `https://www.zhihu.com/answer/${id}`,
      question: { id: 9, title: "问题" },
    },
  };
}

/** A page envelope, with is_end stated explicitly every time. */
function page(data, isEnd) {
  return JSON.stringify({ data, paging: { is_end: isEnd } });
}

/** A stand-in for the page, recording every request and every pause. */
function harness({ replies, maxScanItems = null, frontierScopes = {} } = {}) {
  const requested = [];
  const waits = [];
  const offered = [];
  const paused = [];
  let declared = null;

  const controller = {
    declareScopes: async (scopes) => {
      declared = scopes;
      return Object.fromEntries(scopes.map((s) => [s.scopeId, frontierScopes[s.scopeId] ?? []]));
    },
    offer: async (event) => offered.push(event),
    pause: async (code, message) => paused.push({ code, message }),
  };

  const adapter = createZhihuAdapter({
    controller,
    maxScanItems,
    request: async (url) => {
      requested.push(url);
      const reply = replies(url);
      return reply ?? { ok: false, code: "page_changed" };
    },
    wait: async (ms) => waits.push(ms),
  });

  return { adapter, requested, waits, offered, paused, scopes: () => declared };
}

/** Answers /me, one collections page, then item pages keyed by id:offset. */
function standardReplies({ collections, pages }) {
  return (url) => {
    if (url.startsWith(ME_ENDPOINT)) {
      return { ok: true, body: JSON.stringify({ url_token: TOKEN, id: "x" }) };
    }
    // Items first: an items url contains "/collections" too.
    if (url.includes(ITEMS_PATH)) {
      const parsed = new URL(url);
      const id = parsed.pathname.split("/")[4];
      const offset = parsed.searchParams.get("offset");
      const found = pages[`${id}:${offset}`];
      return found === undefined
        ? { ok: true, body: page([], true) }
        : { ok: true, body: page(found.data, found.isEnd) };
    }
    return { ok: true, body: page(collections, true) };
  };
}

// -- routing and the request allowlist ----------------------------------------

test("only the account's own collections page starts a run", () => {
  // /collections/mine is where the account's own shelves are; bare
  // /collections is the discovery page. Both are accepted because a run only
  // needs the tab to be on the right site — the adapter fetches through the
  // API and never reads the page — but nothing else on zhihu.com is.
  assert.equal(isCollectionsRoute("https://www.zhihu.com/collections/mine"), true);
  assert.equal(isCollectionsRoute("https://www.zhihu.com/collections/mine/"), true);
  assert.equal(isCollectionsRoute("https://www.zhihu.com/collections"), true);
  for (const url of [
    "https://www.zhihu.com/",
    "https://www.zhihu.com/question/123",
    "https://www.zhihu.com/collections/12345",
    "https://zhuanlan.zhihu.com/collections",
    "not a url",
  ]) {
    assert.equal(isCollectionsRoute(url), false, url);
  }
});

test("every url the adapter builds is one the page is allowed to fetch", () => {
  for (const url of [ME_ENDPOINT, collectionsUrl(TOKEN, 0), itemsUrl("108963847", 40)]) {
    assert.equal(isAllowedRequest("zhihu", url), true, url);
  }
});

test("the allowlist admits reading a collection but not acting on one", () => {
  // The identifiers are in the path, so a prefix rule would have admitted every
  // neighbouring operation. These are the ones it must keep out.
  for (const url of [
    "https://www.zhihu.com/api/v4/collections/123/delete",
    "https://www.zhihu.com/api/v4/collections/123/contents",
    "https://www.zhihu.com/api/v4/collections/123",
    "https://www.zhihu.com/api/v4/people/someone/following",
    "https://www.zhihu.com/api/v4/me/collections",
    "https://www.zhihu.com/api/v4/",
    "https://www.zhihu.com/api/v4/collections/abc/items",
    "http://www.zhihu.com/api/v4/collections/123/items",
    "https://evil.example.com/api/v4/collections/123/items",
  ]) {
    assert.equal(isAllowedRequest("zhihu", url), false, url);
  }
});

test("zhihu requests carry the session, since the api authenticates by cookie", () => {
  assert.equal(sendsCredentials("zhihu", itemsUrl("1", 0)), true);
  assert.equal(sendsCredentials("zhihu", "https://cdn.example.com/x"), false);
});

// -- envelopes ----------------------------------------------------------------

test("an error envelope becomes the same stable code Python would give it", () => {
  // The adapter decides whether to pause; Python parses the same body again.
  // Disagreeing would mean pausing on what Python accepts, or the reverse.
  assert.deepEqual(readEnvelope(JSON.stringify({ error: { code: 100 } })), {
    ok: false,
    code: "login_required",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ error: { message: "请先登录" } })), {
    ok: false,
    code: "login_required",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ error: { code: 4039 } })), {
    ok: false,
    code: "rate_limited",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ error: { message: "请求过于频繁" } })), {
    ok: false,
    code: "rate_limited",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ error: { code: 999 } })), {
    ok: false,
    code: "page_changed",
  });
});

test("a body that is not json reads as page_changed, never as an empty page", () => {
  assert.deepEqual(readEnvelope("<!doctype html>"), { ok: false, code: "page_changed" });
});

test("a page without paging.is_end is refused rather than treated as the end", () => {
  // A dropped paging block must never read as a quiet end of scan.
  assert.deepEqual(readItemsPage({ data: [] }), { ok: false, code: "page_changed" });
  assert.deepEqual(readItemsPage({ data: [], paging: {} }), { ok: false, code: "page_changed" });
  assert.deepEqual(readItemsPage({ paging: { is_end: true } }), {
    ok: false,
    code: "page_changed",
  });
});

test("collections without an id or title are skipped rather than guessed at", () => {
  const parsed = readCollectionsPage({
    data: [
      { id: 1, title: "默认收藏夹" },
      { id: 2 },
      { title: "no id" },
      null,
      { id: 3, title: "技术" },
    ],
    paging: { is_end: true },
  });
  assert.deepEqual(parsed.collections, [
    { scopeId: "1", scopeName: "默认收藏夹" },
    { scopeId: "3", scopeName: "技术" },
  ]);
  assert.equal(parsed.isEnd, true);
});

test("source ids match the ones Python derives, or the frontier never matches", () => {
  assert.equal(sourceIdOf(answer(55)), "answer-55");
  assert.equal(sourceIdOf({ content: { type: "article", id: 7 } }), "article-7");
  assert.equal(sourceIdOf({ content: { type: "zvideo", id: 9 } }), "zvideo-9");
  assert.equal(sourceIdOf({ content: {} }), null);
  assert.equal(sourceIdOf({}), null);
});

// -- pagination ---------------------------------------------------------------

test("collections are declared before any page is offered", async () => {
  const { adapter, offered, scopes } = harness({
    replies: standardReplies({
      collections: [{ id: 7, title: "默认收藏夹" }],
      pages: { "7:0": { data: [answer(1)], isEnd: true } },
    }),
  });

  await adapter.run();

  assert.deepEqual(scopes(), [{ scopeId: "7", scopeName: "默认收藏夹" }]);
  assert.equal(offered.length, 1);
  assert.equal(offered[0].kind, "zhihu.items_page");
  assert.equal(offered[0].scopeId, "7");
  assert.equal(offered[0].scopeName, "默认收藏夹");
});

test("a page is forwarded verbatim, so Python decides what it contains", async () => {
  const body = page([answer(1)], true);
  const { adapter, offered } = harness({
    replies: (url) =>
      url.startsWith(ME_ENDPOINT)
        ? { ok: true, body: JSON.stringify({ url_token: TOKEN }) }
        : url.includes(ITEMS_PATH)
          ? { ok: true, body }
          : { ok: true, body: page([{ id: 7, title: "f" }], true) },
  });

  await adapter.run();

  assert.equal(offered[0].body, body, "the raw response text, not a reshaped copy");
});

test("a short page is not the end: only is_end stops a collection", async () => {
  // Deleted favourites shrink pages in the middle of a collection, so a page
  // below the limit is ordinary. Stopping there would truncate the scan.
  const { adapter, requested, offered } = harness({
    replies: standardReplies({
      collections: [{ id: 7, title: "f" }],
      pages: {
        "7:0": { data: [answer(1)], isEnd: false },
        [`7:${PAGE_SIZE}`]: { data: [answer(2), answer(3)], isEnd: false },
        [`7:${PAGE_SIZE * 2}`]: { data: [answer(4)], isEnd: true },
      },
    }),
  });

  await adapter.run();

  const offsets = requested
    .filter((url) => url.includes(ITEMS_PATH))
    .map((url) => new URL(url).searchParams.get("offset"));
  assert.deepEqual(offsets, ["0", String(PAGE_SIZE), String(PAGE_SIZE * 2)]);
  assert.equal(offered.length, 3);
  assert.equal(adapter.summary().observedEnd, true);
});

test("an empty page that is not the end still continues", async () => {
  const { adapter, requested } = harness({
    replies: standardReplies({
      collections: [{ id: 7, title: "f" }],
      pages: {
        "7:0": { data: [], isEnd: false },
        [`7:${PAGE_SIZE}`]: { data: [answer(1)], isEnd: true },
      },
    }),
  });

  await adapter.run();

  const offsets = requested
    .filter((url) => url.includes(ITEMS_PATH))
    .map((url) => new URL(url).searchParams.get("offset"));
  assert.deepEqual(offsets, ["0", String(PAGE_SIZE)]);
});

test("collections themselves are paged to is_end, not just the first page", async () => {
  const seen = [];
  const { adapter, scopes } = harness({
    replies: (url) => {
      if (url.startsWith(ME_ENDPOINT)) {
        return { ok: true, body: JSON.stringify({ url_token: TOKEN }) };
      }
      if (url.includes(ITEMS_PATH)) return { ok: true, body: page([], true) };
      const offset = new URL(url).searchParams.get("offset");
      seen.push(offset);
      return offset === "0"
        ? { ok: true, body: page([{ id: 7, title: "a" }], false) }
        : { ok: true, body: page([{ id: 8, title: "b" }], true) };
    },
  });

  await adapter.run();

  assert.deepEqual(seen, ["0", String(PAGE_SIZE)]);
  assert.deepEqual(scopes(), [
    { scopeId: "7", scopeName: "a" },
    { scopeId: "8", scopeName: "b" },
  ]);
});

test("a collection stops at the frontier the previous run confirmed", async () => {
  const { adapter, offered } = harness({
    frontierScopes: { 7: ["answer-2"] },
    replies: standardReplies({
      collections: [{ id: 7, title: "f" }],
      pages: {
        "7:0": { data: [answer(1), answer(2), answer(3)], isEnd: false },
      },
    }),
  });

  await adapter.run();

  assert.equal(offered.length, 1, "the page carrying the frontier is still forwarded");
  assert.equal(adapter.summary().observedEnd, true);
  assert.deepEqual(adapter.summary().frontierScopes, { 7: ["answer-1"] });
});

test("the scan cap truncates the run and refuses to advance any frontier", async () => {
  const { adapter } = harness({
    maxScanItems: 2,
    replies: standardReplies({
      collections: [
        { id: 7, title: "a" },
        { id: 8, title: "b" },
      ],
      pages: {
        "7:0": { data: [answer(1), answer(2), answer(3)], isEnd: false },
        "8:0": { data: [answer(9)], isEnd: true },
      },
    }),
  });

  await adapter.run();
  const summary = adapter.summary();

  assert.equal(summary.maxScanReached, true);
  assert.equal(summary.observedEnd, false);
  // Absent, not empty: FavHub refuses a scope that reports a cap and names a
  // frontier at once, because the next run would skip what this one missed.
  assert.deepEqual(summary.frontierScopes, {});
  assert.equal(summary.scopeResults["8"].maxScanReached, true);
});

test("a run that finished every collection reports the end and its frontiers", async () => {
  const { adapter } = harness({
    replies: standardReplies({
      collections: [
        { id: 7, title: "a" },
        { id: 8, title: "b" },
      ],
      pages: {
        "7:0": { data: [answer(1)], isEnd: true },
        "8:0": { data: [answer(9)], isEnd: true },
      },
    }),
  });

  await adapter.run();
  const summary = adapter.summary();

  assert.equal(summary.observedEnd, true);
  assert.equal(summary.maxScanReached, false);
  assert.deepEqual(summary.frontierScopes, { 7: ["answer-1"], 8: ["answer-9"] });
  assert.deepEqual(summary.frontierIds, []);
});

// -- throttling and failure ---------------------------------------------------

test("requests are spaced, so a run stays a guest on someone else's service", async () => {
  const { adapter, waits } = harness({
    replies: standardReplies({
      collections: [{ id: 7, title: "f" }],
      pages: {
        "7:0": { data: [answer(1)], isEnd: false },
        [`7:${PAGE_SIZE}`]: { data: [answer(2)], isEnd: true },
      },
    }),
  });

  await adapter.run();

  assert.ok(waits.length >= 2, `expected pauses between requests, saw ${waits.length}`);
  assert.ok(waits.every((ms) => ms >= REQUEST_INTERVAL_MS));
});

test("a logged-out session pauses with its stable code and collects nothing", async () => {
  const { adapter, offered, paused } = harness({
    replies: () => ({ ok: true, body: JSON.stringify({ error: { code: 100 } }) }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "login_required");
  assert.deepEqual(offered, []);
});

test("a rate limit pauses rather than being retried into a harder block", async () => {
  const { adapter, paused } = harness({
    replies: (url) =>
      url.startsWith(ME_ENDPOINT)
        ? { ok: true, body: JSON.stringify({ url_token: TOKEN }) }
        : { ok: true, body: JSON.stringify({ error: { code: 4039 } }) },
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "rate_limited");
});

test("an unreachable page pauses rather than reporting an empty library", async () => {
  const { adapter, paused } = harness({
    replies: () => ({ ok: false, code: "browser_unavailable" }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "browser_unavailable");
});

test("an account with no collections pauses instead of finishing empty", async () => {
  // Finishing "successfully" here would advance the frontier past a library
  // that was merely unreadable.
  const { adapter, paused } = harness({
    replies: (url) =>
      url.startsWith(ME_ENDPOINT)
        ? { ok: true, body: JSON.stringify({ url_token: TOKEN }) }
        : { ok: true, body: page([], true) },
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "page_changed");
});

test("a missing url_token stops the run before any collection is requested", async () => {
  const { adapter, requested, paused } = harness({
    replies: () => ({ ok: true, body: JSON.stringify({ id: "no token here" }) }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "page_changed");
  assert.ok(!requested.some((url) => url.includes(COLLECTIONS_PATH)));
});

// -- what the adapter must never do -------------------------------------------

/** Strip comments so the scan judges code, not the prose explaining it. */
function codeOnly(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const SOURCE = codeOnly(
  readFileSync(
    new URL("../../src/favhub/browser_extension/adapters/zhihu.js", import.meta.url),
    "utf8",
  ),
);

test("the adapter never scrolls, clicks, or injects page-world code", () => {
  for (const forbidden of ["scrollTo", "scrollIntoView", "click(", "createElement", "innerHTML"]) {
    assert.ok(!SOURCE.includes(forbidden), `zhihu.js must not use ${forbidden}`);
  }
});

test("the adapter never names a header or a credential", () => {
  const lowered = SOURCE.toLowerCase();
  for (const forbidden of ["headers", "z_c0", "document.cookie", "authorization", "x-zse"]) {
    assert.ok(!lowered.includes(forbidden), `zhihu.js must not mention ${forbidden}`);
  }
});

test("the adapter issues only GETs, and never a write endpoint", () => {
  for (const forbidden of ['method: "POST"', 'method: "DELETE"', "/delete", "/contents"]) {
    assert.ok(!SOURCE.includes(forbidden), `zhihu.js must not use ${forbidden}`);
  }
});
