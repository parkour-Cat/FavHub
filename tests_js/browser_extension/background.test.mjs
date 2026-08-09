// The service worker had no coverage at all, because importing it needs a
// `chrome` global. Both bugs a live run caught — the hook arming a second too
// late, and a run that captured pages but never finished — lived exactly here.
// Stubbing `chrome` is cheap; leaving this module untested was not.

import assert from "node:assert/strict";
import { test } from "node:test";

const BOOKMARKS_URL = "https://x.com/i/bookmarks";
const TAB_ID = 7;

/** One page of the shape adapters/x.js reads: ids, and a cursor or not. */
function bookmarksBody({ ids, cursor }) {
  const entries = ids.map((id) => ({ entryId: `tweet-${id}` }));
  if (cursor !== null) entries.push({ entryId: cursor });
  return JSON.stringify({
    data: { bookmark_timeline_v2: { timeline: { instructions: [{ entries }] } } },
  });
}

/** A `chrome` stub that records what the worker asked the browser to do. */
function installChrome({
  claim = { session: { session_id: "s-1", job_id: "j-1" } },
  openTabs = [],
  connectNative = null,
} = {}) {
  const calls = {
    reloads: [],
    sentToTab: [],
    native: [],
    removed: [],
    activated: [],
    focusedWindows: [],
    stored: {},
  };
  let onMessage = null;

  const port = {
    listeners: [],
    onMessage: { addListener: (fn) => port.listeners.push(fn) },
    onDisconnect: { addListener: () => {} },
    disconnect: () => {},
    postMessage(message) {
      calls.native.push(message);
      const result = message.type === "session.claim" ? claim : {};
      // Replies arrive asynchronously, as they do over a real port.
      queueMicrotask(() =>
        port.listeners.forEach((fn) => fn({ requestId: message.requestId, result })),
      );
    },
  };

  globalThis.chrome = {
    runtime: {
      getManifest: () => ({ version: "0.1.0" }),
      connectNative: connectNative ?? (() => port),
      onMessage: { addListener: (fn) => (onMessage = fn) },
    },
    // A real store, not a sink: what the worker writes here is the only record
    // of a failure that the popup can still read after the worker is reaped.
    storage: {
      session: {
        set: (entries) => {
          Object.assign(calls.stored, entries);
          return Promise.resolve();
        },
        get: (key) => Promise.resolve(key in calls.stored ? { [key]: calls.stored[key] } : {}),
      },
    },
    tabs: {
      query: () => Promise.resolve(openTabs),
      remove: (tabId) => {
        calls.removed.push(tabId);
        return Promise.resolve();
      },
      update: (tabId, options) => {
        calls.activated.push({ tabId, options });
        return Promise.resolve();
      },
      reload: (tabId) => {
        calls.reloads.push(tabId);
        return Promise.resolve();
      },
      sendMessage: (tabId, message) => {
        calls.sentToTab.push({ tabId, message });
        return Promise.resolve();
      },
    },
    windows: {
      update: (windowId, options) => {
        calls.focusedWindows.push({ windowId, options });
        return Promise.resolve();
      },
    },
  };

  return {
    calls,
    /** Deliver one message the way Chrome does, and resolve with the reply. */
    send(message, sender = { tab: { id: TAB_ID } }) {
      return new Promise((resolve) => {
        const handled = onMessage(message, sender, resolve);
        if (!handled) resolve(undefined);
      });
    },
  };
}

/** Each test needs its own worker: the module holds the session in closure. */
async function loadWorker(options) {
  const chromeStub = installChrome(options);
  // A query string defeats the module cache without touching the source.
  await import(`../../src/favhub/browser_extension/background.js?case=${Math.random()}`);
  return chromeStub;
}

test("a claimed passive session reloads the tab before arming the hook", async () => {
  const { calls, send } = await loadWorker();

  const reply = await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });

  // The load that paid for the claim can only see requests X already made, so
  // it is told to stay dormant and is replaced instead.
  assert.deepEqual(reply, { claimed: false, patterns: [] });
  assert.deepEqual(calls.reloads, [TAB_ID]);
});

test("the load after the reload is answered from memory, and armed", async () => {
  const { calls, send } = await loadWorker();
  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });
  const before = calls.native.length;

  const reply = await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });

  assert.equal(reply.claimed, true);
  assert.deepEqual(reply.patterns, ["/i/api/graphql/"]);
  // One reload only: reloading again would loop the tab forever.
  assert.deepEqual(calls.reloads, [TAB_ID]);
  // And no second claim — going back to FavHub here is the delay being avoided.
  assert.equal(calls.native.length, before);
});

test("a page that is not the collection route is never claimed or reloaded", async () => {
  const { calls, send } = await loadWorker();

  const reply = await send({ kind: "page.ready", platform: "x", url: "https://x.com/home" });

  assert.equal(reply.claimed, false);
  assert.deepEqual(calls.reloads, []);
  assert.deepEqual(calls.native, []);
});

test("nothing is claimed when FavHub has no session waiting", async () => {
  const { calls, send } = await loadWorker({ claim: {} });

  const reply = await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });

  assert.equal(reply.claimed, false);
  assert.deepEqual(calls.reloads, []);
});

test("reaching the observable end finishes the session", async () => {
  const { calls, send } = await loadWorker();
  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });
  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });

  const result = await send({
    kind: "capture.response",
    url: "/i/api/graphql/abc123/Bookmarks?variables=%7B%7D",
    // No bottom cursor: X has no further page to offer.
    body: bookmarksBody({ ids: ["1001", "1002"], cursor: null }),
  });

  assert.equal(result.atEnd, true);
  const types = calls.native.map((message) => message.type);
  // Without this the run captured pages and then sat at `capturing` forever.
  assert.ok(types.includes("session.finish"), types.join(","));
  // The buffered page is submitted before the run is declared over.
  assert.ok(types.indexOf("capture.bundle") < types.indexOf("session.finish"));
  // And the hook is told to stand down rather than left watching the page.
  assert.ok(calls.sentToTab.some((sent) => sent.message.kind === "deactivate"));
});

test("a page that is not the end scrolls for the next one instead of finishing", async () => {
  const { calls, send } = await loadWorker();
  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });
  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });

  const result = await send({
    kind: "capture.response",
    url: "/i/api/graphql/abc123/Bookmarks",
    body: bookmarksBody({ ids: ["1001"], cursor: "cursor-bottom-9" }),
  });

  assert.equal(result.atEnd, false);
  assert.ok(!calls.native.map((message) => message.type).includes("session.finish"));
  assert.ok(calls.sentToTab.some((sent) => sent.message.kind === "scroll"));
});

// -- reusing a tab the user already had open -----------------------------------

const OLD_TAB = 3;

test("a reused tab is brought to the front, window and all", async () => {
  // A tab that is active in a window sitting behind another is still hidden,
  // and Chrome throttles a hidden tab hard enough that the page may never make
  // the request the run waits for. This stalled a real run until the lease
  // expired, which looks identical to a broken extension.
  const { calls, send } = await loadWorker({
    openTabs: [
      { id: OLD_TAB, url: BOOKMARKS_URL, windowId: 42 },
      { id: TAB_ID, url: `${BOOKMARKS_URL}#favhub-opened`, windowId: 43 },
    ],
  });

  await send({
    kind: "page.ready",
    platform: "x",
    url: `${BOOKMARKS_URL}#favhub-opened`,
    favhubOpened: true,
  });

  assert.deepEqual(calls.activated, [{ tabId: OLD_TAB, options: { active: true } }]);
  assert.deepEqual(calls.focusedWindows, [{ windowId: 42, options: { focused: true } }]);
});

test("a tab FavHub opened gives way to one the user already had open", async () => {
  const { calls, send } = await loadWorker({
    openTabs: [
      { id: OLD_TAB, url: BOOKMARKS_URL },
      { id: TAB_ID, url: `${BOOKMARKS_URL}#favhub-opened` },
    ],
  });

  await send({
    kind: "page.ready",
    platform: "x",
    url: `${BOOKMARKS_URL}#favhub-opened`,
    favhubOpened: true,
  });

  // The older tab collects, and the duplicate FavHub added is closed again.
  assert.deepEqual(calls.reloads, [OLD_TAB]);
  assert.deepEqual(calls.removed, [TAB_ID]);
  assert.deepEqual(calls.activated, [{ tabId: OLD_TAB, options: { active: true } }]);
});

test("a tab the user opened is never closed, even when it is the duplicate", async () => {
  // Losing someone's place on a page they opened is worse than an extra tab.
  const { calls, send } = await loadWorker({
    openTabs: [
      { id: OLD_TAB, url: BOOKMARKS_URL },
      { id: TAB_ID, url: BOOKMARKS_URL },
    ],
  });

  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL, favhubOpened: false });

  assert.deepEqual(calls.removed, []);
  assert.deepEqual(calls.reloads, [TAB_ID], "the tab the user is on collects");
});

test("with no tab already open, the one FavHub opened is kept", async () => {
  const { calls, send } = await loadWorker({
    openTabs: [{ id: TAB_ID, url: `${BOOKMARKS_URL}#favhub-opened` }],
  });

  await send({
    kind: "page.ready",
    platform: "x",
    url: `${BOOKMARKS_URL}#favhub-opened`,
    favhubOpened: true,
  });

  assert.deepEqual(calls.removed, []);
  assert.deepEqual(calls.reloads, [TAB_ID]);
});

test("the oldest tab wins when several are already open", async () => {
  const { calls, send } = await loadWorker({
    openTabs: [
      { id: 9, url: BOOKMARKS_URL },
      { id: OLD_TAB, url: BOOKMARKS_URL },
      { id: TAB_ID, url: `${BOOKMARKS_URL}#favhub-opened` },
    ],
  });

  await send({
    kind: "page.ready",
    platform: "x",
    url: `${BOOKMARKS_URL}#favhub-opened`,
    favhubOpened: true,
  });

  // Chrome hands out ids in creation order, so the lowest is the oldest tab.
  assert.deepEqual(calls.reloads, [OLD_TAB]);
  assert.deepEqual(calls.removed, [TAB_ID]);
});

test("a response arriving with no session is refused", async () => {
  const { calls, send } = await loadWorker();

  const result = await send({
    kind: "capture.response",
    url: "/i/api/graphql/abc123/Bookmarks",
    body: bookmarksBody({ ids: ["1001"], cursor: null }),
  });

  assert.deepEqual(result, { accepted: false });
  assert.deepEqual(calls.native, []);
});

// -- reporting a broken install to the popup ----------------------------------

test("the popup is told why FavHub is unreachable, not just that nothing is running", async () => {
  // A broken install writes its reason to storage, but the popup asks the
  // worker — so the two have to be joined here. They were not: the popup said
  // "inactive" while the real cause sat in a store nothing read, and a live
  // broken install took several rounds of digging to identify because of it.
  const dead = {
    onMessage: { addListener: () => {} },
    onDisconnect: { addListener: (fn) => queueMicrotask(fn) },
    disconnect: () => {},
    postMessage: () => {},
  };
  const { send } = await loadWorker({ connectNative: () => dead });

  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });
  const snapshot = await send({ kind: "popup.status" }, {});

  assert.ok(snapshot.error, "the popup must receive the failure, not a bare state");
  assert.equal(snapshot.error.code, "mcp_unavailable");
});

test("a healthy dormant worker reports no error to the popup", async () => {
  const { send } = await loadWorker({ claim: { session: null } });

  await send({ kind: "page.ready", platform: "x", url: BOOKMARKS_URL });
  const snapshot = await send({ kind: "popup.status" }, {});

  assert.equal(snapshot.error ?? null, null);
});
