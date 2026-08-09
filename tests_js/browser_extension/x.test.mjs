import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  createXAdapter,
  inspectPage,
  isBookmarksRoute,
  matchesBookmarksResponse,
  RESPONSE_PATTERNS,
} from "../../src/favhub/browser_extension/adapters/x.js";

const SOURCE = new URL("../../src/favhub/browser_extension/adapters/x.js", import.meta.url);

function fixture(name) {
  return readFileSync(new URL(`../../tests/fixtures/x/${name}`, import.meta.url), "utf8");
}

const PAGE_1 = fixture("bookmarks-page-1.json");
const PAGE_2 = fixture("bookmarks-page-2.json");

// -- the adapter must never become a client -----------------------------------

/** Strip comments so the scan judges code, not the prose explaining it. */
function codeOnly(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

test("the adapter constructs no requests and reads no credentials", () => {
  const code = codeOnly(readFileSync(SOURCE, "utf8"));
  for (const forbidden of [
    "fetch(",
    "XMLHttpRequest",
    "document.cookie",
    "setRequestHeader",
    "Authorization",
    "Bearer",
    "headers",
  ]) {
    assert.ok(!code.includes(forbidden), `x.js code must not use ${forbidden}`);
  }
  // The prose is allowed to explain why, and should.
  assert.ok(readFileSync(SOURCE, "utf8").includes("never issues a request"));
});

// -- endpoint allowlist -------------------------------------------------------

test("only the Bookmarks GraphQL operation is matched", () => {
  assert.ok(matchesBookmarksResponse("/i/api/graphql/aBcD1234/Bookmarks"));
  assert.ok(matchesBookmarksResponse("https://x.com/i/api/graphql/xy-Z_9/Bookmarks?variables=%7B%7D"));
});

test("every other endpoint is rejected", () => {
  const rejected = [
    "/i/api/graphql/aBcD1234/Likes",
    "/i/api/graphql/aBcD1234/HomeTimeline",
    "/i/api/graphql/aBcD1234/BookmarksAllDelete",
    "/i/api/graphql/aBcD1234/UserTweets",
    "/i/api/2/notifications/all.json",
    "/1.1/account/settings.json",
    "https://evil.example/i/api/graphql/x/Bookmarks",
    "/i/api/graphql/Bookmarks",
    "",
  ];
  for (const url of rejected) {
    assert.equal(matchesBookmarksResponse(url), false, url);
  }
});

test("the hook is armed with the bookmarks pattern only", () => {
  assert.deepEqual(RESPONSE_PATTERNS, ["/i/api/graphql/"]);
});

test("only the bookmarks route activates the adapter", () => {
  for (const url of [
    "https://x.com/i/bookmarks",
    "https://x.com/i/bookmarks/",
    "https://x.com/i/bookmarks/all",
    "https://twitter.com/i/bookmarks",
  ]) {
    assert.ok(isBookmarksRoute(url), url);
  }
  for (const url of [
    "https://x.com/home",
    "https://x.com/notifications",
    "https://x.com/i/likes",
    "https://x.com/someone/likes",
    "https://evil.example/i/bookmarks",
  ]) {
    assert.equal(isBookmarksRoute(url), false, url);
  }
});

// -- structural inspection (parsing itself stays in Python) -------------------

test("a page yields its bottom cursor and entry ids", () => {
  const page = inspectPage(PAGE_1);
  assert.equal(page.kind, "page");
  assert.equal(page.bottomCursor, "cursor-bottom-1533240440289373833");
  assert.ok(page.tweetIds.includes("1011048574412526505"));
  assert.equal(page.tweetIds.length, 3);
  assert.equal(page.atEnd, false);
});

test("a logged-out envelope pauses instead of reading as an empty page", () => {
  const result = inspectPage(fixture("logged-out.json"));
  assert.equal(result.kind, "error");
  assert.equal(result.code, "login_required");
});

test("a renamed schema is reported as page_changed", () => {
  const result = inspectPage(fixture("page-changed.json"));
  assert.equal(result.kind, "error");
  assert.equal(result.code, "page_changed");
});

test("malformed JSON is page_changed, never a silent skip", () => {
  assert.equal(inspectPage("not json").code, "page_changed");
});

test("a rate-limit envelope is distinguished from a login failure", () => {
  const body = JSON.stringify({ errors: [{ message: "Rate limit exceeded", code: 88 }] });
  assert.equal(inspectPage(body).code, "rate_limited");
});

test("a page with no bottom cursor is the observable end", () => {
  const body = JSON.stringify({
    data: {
      bookmark_timeline_v2: {
        timeline: { instructions: [{ type: "TimelineAddEntries", entries: [] }] },
      },
    },
  });
  const page = inspectPage(body);
  assert.equal(page.kind, "page");
  assert.equal(page.atEnd, true);
  assert.equal(page.tweetIds.length, 0);
});

// -- adapter driving ----------------------------------------------------------

function harness({ frontier = [], maxScanItems = null } = {}) {
  const offered = [];
  const scrolls = [];
  let paused = null;
  const controller = {
    state: "capturing",
    async offer(event) {
      offered.push(event);
    },
    async pause(code, message) {
      paused = { code, message };
    },
  };
  const adapter = createXAdapter({
    controller,
    frontier,
    maxScanItems,
    scroll: async (reason) => {
      scrolls.push(reason);
    },
  });
  return { adapter, offered, scrolls, controller, paused: () => paused };
}

test("only the response body is forwarded, never the url or anything else", async () => {
  const { adapter, offered } = harness();
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks?variables=%7B%7D", PAGE_1);
  assert.equal(offered.length, 1);
  assert.deepEqual(Object.keys(offered[0]).sort(), ["body", "kind", "platform"]);
  assert.equal(offered[0].kind, "x.bookmarks_page");
  assert.equal(offered[0].platform, "x");
  assert.equal(offered[0].body, PAGE_1);
});

test("a response from a rejected endpoint is dropped without offering it", async () => {
  const { adapter, offered, scrolls } = harness();
  const result = await adapter.onResponse("/i/api/graphql/abc/Likes", PAGE_1);
  assert.equal(result.accepted, false);
  assert.equal(offered.length, 0);
  assert.equal(scrolls.length, 0);
});

test("scrolling happens only after the page was acknowledged", async () => {
  const order = [];
  const controller = {
    state: "capturing",
    async offer() {
      order.push("offer");
    },
    async pause() {},
  };
  const adapter = createXAdapter({
    controller,
    frontier: [],
    maxScanItems: null,
    scroll: async () => {
      order.push("scroll");
    },
  });
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.deepEqual(order, ["offer", "scroll"]);
});

test("a missing bottom cursor stops the run instead of scrolling forever", async () => {
  const { adapter, scrolls } = harness();
  const body = JSON.stringify({
    data: {
      bookmark_timeline_v2: {
        timeline: { instructions: [{ type: "TimelineAddEntries", entries: [] }] },
      },
    },
  });
  const result = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", body);
  assert.equal(result.atEnd, true);
  assert.equal(scrolls.length, 0);
});

test("a repeated cursor stops the run rather than looping on one page", async () => {
  const { adapter, scrolls } = harness();
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  const second = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.equal(second.atEnd, true);
  assert.equal(second.reason, "cursor_repeated");
  assert.equal(scrolls.length, 1, "the identical second page must not scroll again");
});

test("both pages are still offered so a replayed page reaches the receipt logic", async () => {
  const { adapter, offered } = harness();
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.equal(offered.length, 2, "deduplication is FavHub's job, not the adapter's");
});

test("an incremental run stops at the first frontier id", async () => {
  const { adapter, scrolls } = harness({ frontier: ["1816024121722501770"] });
  const result = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.equal(result.atEnd, true);
  assert.equal(result.reason, "frontier_reached");
  assert.equal(scrolls.length, 0);
});

test("a frontier id that is absent keeps the scan going", async () => {
  const { adapter, scrolls } = harness({ frontier: ["9999999999999999999"] });
  const result = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.equal(result.atEnd, false);
  assert.equal(scrolls.length, 1);
});

test("maxScanItems bounds a smoke run and reports it as truncated", async () => {
  const { adapter, scrolls } = harness({ maxScanItems: 4 });
  const first = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  assert.equal(first.atEnd, false);
  const second = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_2);
  assert.equal(second.atEnd, true);
  assert.equal(second.reason, "max_scan_reached");
  assert.equal(adapter.summary().maxScanReached, true);
  assert.equal(scrolls.length, 1);
});

test("an error envelope pauses the session with its stable code", async () => {
  const { adapter, paused, offered, scrolls } = harness();
  const result = await adapter.onResponse(
    "/i/api/graphql/abc/Bookmarks",
    fixture("logged-out.json"),
  );
  assert.equal(result.accepted, false);
  assert.equal(paused().code, "login_required");
  assert.equal(offered.length, 0, "an error envelope is never submitted as content");
  assert.equal(scrolls.length, 0);
});

test("the summary reports what finish_scan needs and nothing more", async () => {
  const { adapter } = harness();
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  const summary = adapter.summary();
  assert.deepEqual(Object.keys(summary).sort(), [
    "frontierIds",
    "maxScanReached",
    "observedEnd",
    "scanned",
  ]);
  // The newest ids become the next incremental run's stopping line.
  assert.deepEqual(summary.frontierIds, [
    "1011048574412526505",
    "1816024121722501770",
    "1112444407014444922",
  ]);
  assert.equal(summary.scanned, 3);
});

test("the frontier keeps the newest ids across pages, not just the first one", async () => {
  const { adapter } = harness();
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_2);

  // A one-id frontier stops recognising anything the moment that single
  // bookmark is removed, and the next incremental run then scrolls the whole
  // timeline. Every id this run confirmed from the top is worth keeping.
  assert.deepEqual(adapter.summary().frontierIds, [
    "1011048574412526505",
    "1816024121722501770",
    "1112444407014444922",
    "1330120223525413131",
    "1132003155372311920",
  ]);
});

test("the frontier is capped so a long run does not carry the whole timeline", async () => {
  const { adapter } = harness();
  const ids = Array.from({ length: 30 }, (_value, index) => `${9000 + index}`);
  const page = JSON.stringify({
    data: {
      bookmark_timeline_v2: {
        timeline: {
          instructions: [
            {
              entries: [
                ...ids.map((id) => ({ entryId: `tweet-${id}` })),
                { entryId: "cursor-bottom-long" },
              ],
            },
          ],
        },
      },
    },
  });

  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", page);

  const frontierIds = adapter.summary().frontierIds;
  assert.equal(frontierIds.length, 20);
  assert.equal(frontierIds[0], "9000");
  assert.equal(frontierIds[19], "9019");
});

test("nothing is scanned or reported after the observable end", async () => {
  const { adapter } = harness({ frontier: ["1011048574412526505"] });
  await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_1);
  const after = await adapter.onResponse("/i/api/graphql/abc/Bookmarks", PAGE_2);
  assert.equal(after.accepted, false);
  assert.equal(adapter.summary().observedEnd, true);
});
