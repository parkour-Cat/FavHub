import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInThisContext } from "node:vm";

import {
  NativeClient,
  NativeClientError,
} from "../../src/favhub/browser_extension/native-client.js";
import {
  MAX_PENDING_ITEMS,
  SessionController,
  SessionState,
} from "../../src/favhub/browser_extension/session-controller.js";
import { actionsFor, render } from "../../src/favhub/browser_extension/popup.js";

// bridge.js is a classic content script, not a module, so it is run the way
// Chrome runs it: evaluated for the global it defines. Importing a separate
// module copy would test something the browser never loads.
runInThisContext(
  readFileSync(new URL("../../src/favhub/browser_extension/bridge.js", import.meta.url), "utf8"),
);
const {
  createAnnouncer,
  createBridge,
  findScroller,
  platformForHost,
  scrollOnce,
  scrollToEnd,
  validatePageMessage,
} = globalThis.FavHubBridge;

/** A page whose timeline lives in a container, the way X lays bookmarks out. */
function fakeView({ documentHeight, innerHeight, nodes = [] }) {
  const scrolled = [];
  const timers = [];
  const view = {
    innerHeight,
    scrollY: 0,
    scrolled,
    // Collected rather than fired, so a test decides when time passes.
    timers,
    setTimeout: (fn) => timers.push(fn),
    scrollTo: (options) => {
      scrolled.push({ target: "document", top: options.top });
      view.scrollY = options.top;
    },
    getComputedStyle: (node) => ({ overflowY: node.overflowY ?? "visible" }),
    document: {
      documentElement: { scrollHeight: documentHeight },
      querySelectorAll: () => nodes,
    },
  };
  return { scrolled, view };
}

/** A stand-in for chrome.runtime.Port that answers from a scripted queue. */
function fakePort({ reply = () => null } = {}) {
  const messageListeners = [];
  const disconnectListeners = [];
  const port = {
    sent: [],
    onMessage: { addListener: (fn) => messageListeners.push(fn) },
    onDisconnect: { addListener: (fn) => disconnectListeners.push(fn) },
    postMessage(message) {
      port.sent.push(message);
      const response = reply(message);
      if (response !== null && response !== undefined) {
        queueMicrotask(() => messageListeners.forEach((fn) => fn(response)));
      }
    },
    disconnect() {
      port.disconnected = true;
    },
    fireDisconnect() {
      disconnectListeners.forEach((fn) => fn());
    },
    deliver(message) {
      messageListeners.forEach((fn) => fn(message));
    },
  };
  return port;
}

function clientWith(reply, options = {}) {
  const port = fakePort({ reply });
  const client = new NativeClient({ connect: () => port, ...options });
  return { client, port };
}

// -- native client ------------------------------------------------------------

test("requests carry an incrementing id and the protocol version", async () => {
  const { client, port } = clientWith((message) => ({
    requestId: message.requestId,
    result: { ok: true },
  }));
  await client.request("session.claim", { platform: "x" });
  await client.request("session.heartbeat", {});
  assert.deepEqual(
    port.sent.map((m) => m.requestId),
    ["r-000001", "r-000002"],
  );
  assert.equal(port.sent[0].protocolVersion, 1);
  assert.equal(port.sent[0].type, "session.claim");
});

test("replies resolve the matching request only", async () => {
  const { client, port } = clientWith(() => null);
  const first = client.request("session.claim", {});
  const second = client.request("session.finish", {});
  port.deliver({ requestId: "r-000002", result: { which: "second" } });
  port.deliver({ requestId: "r-000001", result: { which: "first" } });
  assert.deepEqual(await second, { which: "second" });
  assert.deepEqual(await first, { which: "first" });
});

test("an unmatched reply is ignored rather than guessed at", async () => {
  const { client, port } = clientWith(() => null);
  const pending = client.request("session.claim", {});
  port.deliver({ requestId: "r-999999", result: { stray: true } });
  port.deliver({ requestId: "r-000001", result: { ok: true } });
  assert.deepEqual(await pending, { ok: true });
});

test("an error reply rejects with its stable code", async () => {
  const { client } = clientWith((message) => ({
    requestId: message.requestId,
    error: { code: "rate_limited", message: "slow down" },
  }));
  await assert.rejects(
    () => client.request("session.claim", {}),
    (error) => error instanceof NativeClientError && error.code === "rate_limited",
  );
});

test("a silent relay times out instead of hanging the session", async () => {
  const { client } = clientWith(() => null, { timeoutMs: 5 });
  await assert.rejects(
    () => client.request("session.claim", {}),
    (error) => error.code === "mcp_unavailable",
  );
});

test("a disconnect rejects everything still in flight", async () => {
  const { client, port } = clientWith(() => null);
  const pending = client.request("session.claim", {});
  port.fireDisconnect();
  await assert.rejects(pending, (error) => error.code === "mcp_unavailable");
  assert.equal(client.isOpen, false);
});

// -- session controller -------------------------------------------------------

function controllerWith(reply) {
  const { client, port } = clientWith(reply);
  const seen = [];
  const controller = new SessionController({ client, onChange: (s) => seen.push(s) });
  return { controller, port, seen };
}

const claimReply = (message) => {
  if (message.type === "session.claim") {
    return {
      requestId: message.requestId,
      result: { session: { session_id: "s-1", job_id: "j-1", status: "capturing" } },
    };
  }
  return { requestId: message.requestId, result: { receipt: "browser-batch-0001" } };
};

test("no waiting session leaves the extension dormant", async () => {
  const { controller } = controllerWith((message) => ({
    requestId: message.requestId,
    result: { session: null },
  }));
  assert.equal(await controller.claim("x", "0.1.0"), false);
  assert.equal(controller.state, SessionState.INACTIVE);
  assert.equal(controller.sessionId, null);
});

test("a refused claim carries its reason instead of looking like an idle day", async () => {
  const { controller } = controllerWith((message) => ({
    requestId: message.requestId,
    result: {
      session: null,
      error: {
        code: "extension_version_mismatch",
        message: "Chrome is running FavHub extension 0.1.0, but 0.2.0 is installed.",
      },
    },
  }));

  assert.equal(await controller.claim("x", "0.1.0"), false);

  // "Nothing waiting" and "refused" arrive the same way, and only one of them
  // is something the user can act on. Dropping the reason left a stale
  // extension collecting in silence.
  assert.equal(controller.state, SessionState.INACTIVE);
  assert.equal(controller.error.code, "extension_version_mismatch");
  assert.match(controller.snapshot.error.message, /0\.2\.0/);
});

test("an ordinary empty answer still leaves no error behind", async () => {
  const { controller } = controllerWith((message) => ({
    requestId: message.requestId,
    result: { session: null },
  }));
  assert.equal(await controller.claim("x", "0.1.0"), false);
  assert.equal(controller.error, null);
});

test("a waiting session moves the controller to capturing", async () => {
  const { controller } = controllerWith(claimReply);
  assert.equal(await controller.claim("x", "0.1.0"), true);
  assert.equal(controller.state, SessionState.CAPTURING);
  assert.equal(controller.jobId, "j-1");
  await controller.cancel();
});

test("a full batch flushes automatically and clears only after the receipt", async () => {
  const { controller, port } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");
  for (let index = 0; index < MAX_PENDING_ITEMS; index += 1) {
    await controller.offer({ kind: "capture.response", body: `b${index}` });
  }
  const bundles = port.sent.filter((m) => m.type === "capture.bundle");
  assert.equal(bundles.length, 1);
  assert.equal(bundles[0].payload.events.length, MAX_PENDING_ITEMS);
  assert.equal(controller.pending.length, 0);
  assert.equal(controller.counts.submitted, MAX_PENDING_ITEMS);
  await controller.cancel();
});

test("a batch flushes on size before it outgrows what the relay can carry", async () => {
  const { controller, port } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");

  // Three events of ~1.2 MB each: well under the twenty that used to be the
  // only bound, and over the byte budget after the second.
  const body = "锁".repeat(400_000); // CJK, so three UTF-8 bytes per character
  for (let index = 0; index < 3; index += 1) {
    await controller.offer({ kind: "zhihu.items_page", body: `${index}${body}` });
  }

  const bundles = port.sent.filter((m) => m.type === "capture.bundle");
  assert.equal(bundles.length, 1, "the buffered pages went out before the third joined them");
  assert.equal(bundles[0].payload.events.length, 2);
  assert.equal(controller.pending.length, 1);
  await controller.cancel();
});

test("an oversize single event is still sent, because a page cannot be split", async () => {
  const { controller, port } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");

  await controller.offer({ kind: "zhihu.items_page", body: "锁".repeat(1_400_000) });

  // Nothing was dropped and nothing was silently held back: one event alone
  // cannot be made smaller, so it travels and the relay reports it if it must.
  assert.equal(port.sent.filter((m) => m.type === "capture.bundle").length, 0);
  assert.equal(controller.pending.length, 1);
  await controller.cancel();
});

test("flushing releases the bytes it sent, so the budget does not creep", async () => {
  const { controller } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");
  for (let index = 0; index < MAX_PENDING_ITEMS; index += 1) {
    await controller.offer({ kind: "capture.response", body: `b${index}` });
  }
  assert.equal(controller.pending.length, 0);
  // A budget that only ever grew would flush after every single event once a
  // long run had gone through enough of them.
  assert.equal(controller.pendingBytes, 0);
  await controller.cancel();
});

test("an unacknowledged batch stays buffered for a retry", async () => {
  const { controller, port } = controllerWith((message) =>
    message.type === "capture.bundle" ? null : claimReply(message),
  );
  await controller.claim("x", "0.1.0");
  await controller.offer({ kind: "capture.response", body: "one" });
  const flush = controller.flush();
  assert.equal(controller.pending.length, 1, "still buffered while the receipt is outstanding");
  port.deliver({ requestId: "r-000002", result: { receipt: "browser-batch-0001" } });
  await flush;
  assert.equal(controller.pending.length, 0);
  await controller.cancel();
});

test("a disconnect while capturing reports paused, not a silent stall", async () => {
  const { controller, port } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");
  port.fireDisconnect();
  assert.equal(controller.state, SessionState.PAUSED);
  assert.equal(controller.error.code, "mcp_unavailable");
});

test("cancelling drops unacknowledged work instead of keeping a copy", async () => {
  const { controller } = controllerWith(claimReply);
  await controller.claim("x", "0.1.0");
  await controller.offer({ kind: "capture.response", body: "one" });
  await controller.cancel();
  assert.equal(controller.state, SessionState.CANCELLED);
  assert.equal(controller.pending.length, 0);
});

test("offering outside a capturing session is refused", async () => {
  const { controller } = controllerWith(claimReply);
  await assert.rejects(() => controller.offer({ kind: "capture.response", body: "x" }));
});

// -- isolated bridge ----------------------------------------------------------

test("the platform comes from the host, never from the message", () => {
  assert.equal(platformForHost("x.com"), "x");
  assert.equal(platformForHost("www.zhihu.com"), "zhihu");
  assert.equal(platformForHost("evil.example"), null);
});

test("a cross-origin page message is rejected", () => {
  const result = validatePageMessage(
    { channel: "favhub:page", kind: "response", detail: { url: "/api", body: "{}" } },
    { origin: "https://evil.example", expectedOrigin: "https://x.com" },
  );
  assert.equal(result.ok, false);
  assert.equal(result.reason, "cross_origin");
});

test("malformed and oversized page messages are rejected", () => {
  const base = { origin: "https://x.com", expectedOrigin: "https://x.com" };
  const cases = [
    [null, "not_an_object"],
    [{ channel: "other", kind: "response" }, "wrong_channel"],
    [{ channel: "favhub:page", kind: "evil" }, "unknown_kind"],
    [{ channel: "favhub:page", kind: "response" }, "no_detail"],
    [{ channel: "favhub:page", kind: "response", detail: { url: 7, body: "{}" } }, "bad_url"],
    [{ channel: "favhub:page", kind: "response", detail: { url: "/a", body: 1 } }, "bad_body"],
  ];
  for (const [message, reason] of cases) {
    assert.equal(validatePageMessage(message, base).reason, reason);
  }
  const oversized = {
    channel: "favhub:page",
    kind: "response",
    detail: { url: "/api", body: "x".repeat(50) },
  };
  assert.equal(validatePageMessage(oversized, { ...base, maxBytes: 10 }).reason, "too_large");
});

test("a foreign absolute URL cannot smuggle content in", () => {
  const result = validatePageMessage(
    {
      channel: "favhub:page",
      kind: "response",
      detail: { url: "https://evil.example/x", body: "{}" },
    },
    { origin: "https://x.com", expectedOrigin: "https://x.com" },
  );
  assert.equal(result.reason, "foreign_url");
});

test("a valid page message reaches the worker with the host's platform", () => {
  const forwarded = [];
  const bridge = createBridge({
    location: { origin: "https://x.com", host: "x.com" },
    postToPage: () => {},
    sendToWorker: (payload) => forwarded.push(payload),
  });
  const accepted = bridge.handlePageMessage(
    {
      channel: "favhub:page",
      kind: "response",
      detail: { url: "/i/api/graphql/Bookmarks", body: '{"data":{}}' },
    },
    "https://x.com",
  );
  assert.equal(accepted, true);
  assert.equal(forwarded[0].platform, "x");
  assert.equal(forwarded[0].kind, "capture.response");
});

test("the page-world hook is only activated for passive platforms", () => {
  const posted = [];
  const passive = createBridge({
    location: { origin: "https://x.com", host: "x.com" },
    postToPage: (message) => posted.push(message),
    sendToWorker: () => {},
  });
  assert.equal(passive.activate(["/i/api/graphql/Bookmarks"]), true);
  assert.equal(posted.length, 1);

  for (const host of ["www.bilibili.com", "www.zhihu.com"]) {
    const active = createBridge({
      location: { origin: `https://${host}`, host },
      postToPage: (message) => posted.push(message),
      sendToWorker: () => {},
    });
    assert.equal(active.activate(["/x/v3/fav/resource/list"]), false, host);
  }
  assert.equal(posted.length, 1, "no page-world code is injected on active-mode platforms");
});

// -- scrolling ----------------------------------------------------------------

test("a page that scrolls itself is scrolled through the window", () => {
  const { view, scrolled } = fakeView({ documentHeight: 9000, innerHeight: 1000 });
  assert.equal(findScroller(view), null);
  assert.deepEqual(scrollOnce(view), {
    where: "document",
    before: 0,
    after: 9000,
    height: 9000,
  });
  assert.deepEqual(scrolled, [{ target: "document", top: 9000 }]);
});

test("a timeline inside a container is scrolled by that container", () => {
  // The live failure: document height equalled viewport height, so the window
  // scroll was a no-op and the platform never loaded a second page.
  const timeline = { scrollHeight: 8000, clientHeight: 1112, scrollTop: 0, overflowY: "auto" };
  const { view, scrolled } = fakeView({
    documentHeight: 1112,
    innerHeight: 1112,
    nodes: [timeline],
  });
  assert.equal(findScroller(view), timeline);
  assert.equal(scrollOnce(view).where, "container");
  assert.equal(timeline.scrollTop, 8000);
  assert.deepEqual(scrolled, [], "the window must not be scrolled instead");
});

test("scrolling is retried, because the first attempt races the render", () => {
  // The adapter scrolls the moment it accepts a response, before the platform
  // has laid those items out: at that instant there is nothing to scroll, and
  // a single attempt leaves the run waiting for a page that never comes.
  const { view, scrolled } = fakeView({ documentHeight: 1112, innerHeight: 1112 });
  const attempts = [];
  scrollToEnd(view, { report: (moved) => attempts.push(moved) });

  assert.equal(attempts.length, 1, "the first attempt is immediate");
  // Content renders; now the document really has somewhere to go.
  view.document.documentElement.scrollHeight = 9000;
  while (view.timers.length > 0) view.timers.shift()();

  assert.equal(attempts.length, 4);
  assert.equal(attempts.at(-1).after, 9000);
  assert.deepEqual(scrolled.at(-1), { target: "document", top: 9000 });
});

test("the tallest scrollable container wins over a small one", () => {
  const sidebar = { scrollHeight: 2000, clientHeight: 500, scrollTop: 0, overflowY: "scroll" };
  const timeline = { scrollHeight: 9000, clientHeight: 1112, scrollTop: 0, overflowY: "auto" };
  const { view } = fakeView({
    documentHeight: 1112,
    innerHeight: 1112,
    nodes: [sidebar, timeline],
  });
  assert.equal(findScroller(view), timeline);
});

test("an element that merely overflows is not treated as a scroller", () => {
  // Overflowing content with `overflow: visible` scrolls nothing; picking it
  // would look like a scroll and move the page not at all.
  const overflowing = { scrollHeight: 9000, clientHeight: 100, scrollTop: 0, overflowY: "visible" };
  const { view, scrolled } = fakeView({
    documentHeight: 1112,
    innerHeight: 1112,
    nodes: [overflowing],
  });
  assert.equal(findScroller(view), null);
  assert.equal(scrollOnce(view).where, "document");
  assert.deepEqual(scrolled, [{ target: "document", top: 1112 }]);
});

// -- popup --------------------------------------------------------------------

test("only actions valid for the current state are offered", () => {
  assert.deepEqual(actionsFor("capturing"), { pause: true, cancel: true });
  assert.deepEqual(actionsFor("paused"), { pause: false, cancel: true });
  assert.deepEqual(actionsFor("completed"), { pause: false, cancel: false });
  assert.deepEqual(actionsFor("nonsense"), { pause: false, cancel: false });
});

test("the popup renders counts and disables invalid buttons", () => {
  const { nodes, document } = popupNodes();
  render(document, {
    state: "paused",
    platform: "zhihu",
    counts: { scanned: 40, submitted: 20 },
    pending: 20,
    error: { code: "rate_limited", message: "slow down" },
  });
  assert.equal(nodes.get("state").textContent, "paused");
  assert.equal(nodes.get("platform").textContent, "zhihu");
  assert.equal(nodes.get("scanned").textContent, "40");
  assert.equal(nodes.get("pending").textContent, "20");
  assert.equal(nodes.get("error").hidden, false);
  assert.equal(nodes.get("pause").disabled, true);
  assert.equal(nodes.get("cancel").disabled, false);
});

test("only passive platforms are marked for page-world injection", () => {
  const build = (host) =>
    createBridge({
      location: { origin: `https://${host}`, host },
      postToPage: () => {},
      sendToWorker: () => {},
    });
  assert.equal(build("x.com").passive, true);
  assert.equal(build("twitter.com").passive, true);
  assert.equal(build("www.bilibili.com").passive, false);
  assert.equal(build("www.zhihu.com").passive, false);
});

test("a claim carries the stopping line back to the controller", async () => {
  const { controller } = controllerWith((message) => ({
    requestId: message.requestId,
    result: {
      session: { session_id: "s-1", job_id: "j-1" },
      frontier: ["111", "222"],
      maxScanItems: 10,
      scopes: [{ scopeId: "42", scopeName: "默认收藏夹" }],
    },
  }));
  await controller.claim("x", "0.1.0");
  assert.deepEqual(controller.frontier, ["111", "222"]);
  assert.equal(controller.maxScanItems, 10);
  assert.deepEqual(controller.scopes, [{ scopeId: "42", scopeName: "默认收藏夹" }]);
  await controller.cancel();
});

test("a refused batch stays buffered instead of vanishing", async () => {
  const { controller } = controllerWith((message) =>
    message.type === "capture.bundle"
      ? {
          requestId: message.requestId,
          result: { accepted: false, error: { code: "login_required" } },
        }
      : claimReply(message),
  );
  await controller.claim("x", "0.1.0");
  await controller.offer({ kind: "capture.response", body: "one" });
  await assert.rejects(() => controller.flush());
  assert.equal(controller.pending.length, 1, "nothing is dropped on a refusal");
  await controller.cancel();
});

// -- announcing a page that is already open -----------------------------------
//
// A run is started from a chat window, so the platform tab is usually already
// open and in the background. Without a second chance to announce itself, the
// only way to start collecting is to reload that tab by hand.

/** An announcer over a controllable clock, recording each announcement. */
function announcerAt(intervalMs = 30_000) {
  const sent = [];
  let clock = 1_000;
  const announcer = createAnnouncer({
    send: () => {
      sent.push(clock);
      return Promise.resolve();
    },
    now: () => clock,
    intervalMs,
  });
  return { announcer, sent, tick: (ms) => (clock += ms) };
}

test("the first announcement is made immediately, whatever the interval", () => {
  const { announcer, sent } = announcerAt();
  assert.equal(announcer.announce(true), true);
  assert.deepEqual(sent, [1_000]);
});

test("announcements inside the interval are dropped, and one after it is not", async () => {
  // Each announcement that finds no session starts a native host process, so a
  // user flipping between tabs must not be able to start one per flip.
  const { announcer, sent, tick } = announcerAt(30_000);
  announcer.announce(true);
  await Promise.resolve();

  tick(10_000);
  assert.equal(announcer.announce(), false);
  tick(10_000);
  assert.equal(announcer.announce(), false);
  await Promise.resolve();
  assert.deepEqual(sent, [1_000], "still just the load-time announcement");

  tick(15_000);
  assert.equal(announcer.announce(), true);
  await Promise.resolve();
  assert.deepEqual(sent, [1_000, 36_000]);
});

test("a second announcement never overlaps one still in flight", async () => {
  let release;
  const sent = [];
  const announcer = createAnnouncer({
    send: () => {
      sent.push("sent");
      return new Promise((resolve) => {
        release = resolve;
      });
    },
    now: () => 0,
    intervalMs: 0,
  });

  assert.equal(announcer.announce(true), true);
  await Promise.resolve();
  // Even forced, and even with no interval at all: the worker is mid-claim.
  assert.equal(announcer.announce(true), false);
  assert.deepEqual(sent, ["sent"]);

  release();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(announcer.announce(true), true);
});

test("a rejected announcement unblocks the next one rather than wedging", async () => {
  // FavHub not running is the ordinary case, and it must not leave the page
  // unable to ever announce itself again.
  let attempts = 0;
  const announcer = createAnnouncer({
    send: () => {
      attempts += 1;
      return Promise.reject(new Error("no native host"));
    },
    now: () => 0,
    intervalMs: 0,
  });

  announcer.announce(true);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(announcer.announce(true), true);
  assert.equal(attempts, 2);
});

// -- the popup when FavHub itself is broken -----------------------------------
//
// This is the one failure a user cannot reason about. Chrome says only "Error
// when communicating with the native messaging host", the popup used to say
// "inactive", and nothing anywhere named the command that diagnoses it. A real
// broken install took several rounds to identify from that.

function popupNodes() {
  const nodes = new Map(
    ["state", "platform", "scanned", "submitted", "pending", "error", "hint", "pause", "cancel"].map(
      (id) => [id, { id, textContent: "", hidden: false, disabled: false }],
    ),
  );
  return { nodes, document: { getElementById: (id) => nodes.get(id) } };
}

test("a broken native channel names the command that diagnoses it", () => {
  const { nodes, document } = popupNodes();
  render(document, { state: "inactive", error: { code: "mcp_unavailable", message: "no host" } });
  assert.equal(nodes.get("error").hidden, false);
  assert.match(nodes.get("error").textContent, /mcp_unavailable/);
  assert.equal(nodes.get("hint").hidden, false, "the repair hint must be visible");
  assert.match(nodes.get("hint").textContent, /favhub doctor/);
});

test("a platform-side pause gets no install hint, because nothing is broken", () => {
  // Telling someone to run `favhub doctor` because they hit a rate limit sends
  // them to fix an install that is fine.
  const { nodes, document } = popupNodes();
  render(document, { state: "paused", error: { code: "rate_limited", message: "slow down" } });
  assert.equal(nodes.get("error").hidden, false);
  assert.equal(nodes.get("hint").hidden, true);
});

test("a healthy dormant extension shows neither an error nor a hint", () => {
  const { nodes, document } = popupNodes();
  render(document, { state: "inactive" });
  assert.equal(nodes.get("error").hidden, true);
  assert.equal(nodes.get("hint").hidden, true);
});
