// Service worker: owns the native connection and the current session.
//
// MV3 terminates an idle service worker, which would abandon a run mid-scan.
// An open Native Messaging port is what keeps this worker alive, so the port is
// opened once when a session is claimed and held for the whole run rather than
// reconnected per message.

import { NativeClient, NATIVE_HOST } from "./native-client.js";
import { SessionController, SessionState } from "./session-controller.js";
import { createXAdapter, isBookmarksRoute } from "./adapters/x.js";
import {
  accountIdFromUrl,
  createBilibiliAdapter,
  isCollectionRoute,
} from "./adapters/bilibili.js";
import { createZhihuAdapter, isCollectionsRoute } from "./adapters/zhihu.js";

const EXTENSION_VERSION = chrome.runtime.getManifest().version;

/** Adapters are keyed by platform; a platform without one simply never runs. */
const ADAPTERS = {
  x: {
    mode: "passive",
    accepts: (url) => isBookmarksRoute(url),
    create: (options) => createXAdapter(options),
    // The tabs this platform collects from, for finding one already open.
    tabPatterns: ["https://x.com/i/bookmarks*", "https://twitter.com/i/bookmarks*"],
  },
  bilibili: {
    mode: "active",
    accepts: (url) => isCollectionRoute(url),
    // The id from the route is a hint, not a requirement: on the home page
    // there is none, and the adapter asks the platform who it is.
    create: (options, url) =>
      createBilibiliAdapter({ ...options, accountId: accountIdFromUrl(url) }),
    tabPatterns: ["https://space.bilibili.com/*/favlist*", "https://www.bilibili.com/"],
  },
  zhihu: {
    mode: "active",
    accepts: (url) => isCollectionsRoute(url),
    // The account is identified through the API rather than the page URL, so
    // unlike Bilibili nothing has to be read off the route here.
    create: (options) => createZhihuAdapter(options),
    tabPatterns: ["https://www.zhihu.com/collections", "https://www.zhihu.com/collections/mine*"],
  },
};

let adapter = null;
/** The tab already reloaded to arm this session's hook; see needsArmingReload. */
let armedTabId = null;

export function createController({ connect, onChange }) {
  const client = new NativeClient({ connect });
  return new SessionController({ client, onChange });
}

const controller = createController({
  connect: () => chrome.runtime.connectNative(NATIVE_HOST),
  onChange: (snapshot) => {
    // The popup reads this on open; a dead worker simply shows "inactive".
    chrome.storage.session.set({ favhubSnapshot: snapshot }).catch(() => {});
  },
});

/** Ask FavHub whether this platform has a run waiting for the browser.
 *
 * Two things must both be true before anything is collected: FavHub says a
 * session is waiting, and the tab is actually on the platform's collection
 * route. Neither alone is enough — a session with the wrong page open would
 * scroll something the user did not ask to collect.
 */
async function tryClaim(platform, url) {
  const entry = ADAPTERS[platform];
  if (!entry || !entry.accepts(url)) return false;
  // The load that follows an arming reload: this session is already claimed, so
  // answer from memory. Asking FavHub again would reintroduce the very round
  // trip the reload exists to get out of the page's way.
  if (adapter !== null && controller.state === SessionState.CAPTURING) {
    return controller.platform === platform;
  }
  try {
    const claimed = await controller.claim(platform, EXTENSION_VERSION);
    if (!claimed) {
      adapter = null;
      armedTabId = null;
      return false;
    }
    adapter = entry.create(
      {
        controller,
        // Set by claim(), so the adapter knows its stopping line up front.
        frontier: controller.frontier,
        maxScanItems: controller.maxScanItems,
        scroll: async () => {
          await chrome.tabs.sendMessage(controller.tabId, { kind: "scroll" }).catch(() => {});
        },
        // Active mode fetches through the page, so the platform's own cookies
        // travel with the request and nothing here reads a credential. The
        // content script re-checks every URL against the allowlist.
        request: (target) =>
          chrome.tabs
            .sendMessage(controller.tabId, { kind: "fetch", url: target })
            .catch((error) => ({ ok: false, code: "browser_unavailable", detail: String(error) })),
        wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      },
      url,
    );
    return true;
  } catch (error) {
    adapter = null;
    armedTabId = null;
    // No FavHub, no session: staying dormant is the correct outcome, and the
    // Skill reports the real reason through MCP.
    await chrome.storage.session
      .set({ favhubSnapshot: { state: SessionState.INACTIVE, error: describe(error) } })
      .catch(() => {});
    return false;
  }
}

function describe(error) {
  return { code: error?.code ?? "mcp_unavailable", message: error?.message ?? String(error) };
}

/** Put the page-world hook ahead of the page's own first collection request.
 *
 * A passive platform is only observable through responses made *after* the hook
 * is installed, and installing it costs a claim: a round trip through the native
 * host, the pipe, and SQLite. X finishes its first Bookmarks fetch inside that
 * window, so the opening page — the newest bookmarks, the ones that matter most
 * — was missed every time, and since the adapter only scrolls once it accepts a
 * response, the run then had nothing to continue from and simply stalled.
 *
 * Reloading once, after the session is claimed, fixes the ordering: on the new
 * load `tryClaim` answers from memory, so the hook is armed in tens of
 * milliseconds rather than well over a second. Nothing is buffered and nothing
 * is copied before FavHub confirms a session — the guarantee the hook is built
 * around stays exactly as it was.
 */
function needsArmingReload(platform, tabId) {
  return ADAPTERS[platform]?.mode === "passive" && armedTabId !== tabId;
}

/** Prefer a tab the user already had open over the one FavHub just opened.
 *
 * FavHub opens the saved-items page because a page load is the only event it
 * can cause that wakes this worker. That would leave a duplicate behind every
 * run for anyone who keeps the page open, so the duplicate is resolved here,
 * where the tabs are actually visible: the older tab collects, and the one
 * FavHub opened is closed again.
 *
 * Only a tab FavHub opened is ever closed. A tab the user opened is left alone
 * even when it is the redundant one — losing someone's place on a page they
 * opened themselves is a far worse outcome than an extra tab.
 *
 * @returns the tab id that should collect
 */
async function preferExistingTab(platform, tabId, favhubOpened) {
  if (!favhubOpened) return tabId;
  const patterns = ADAPTERS[platform]?.tabPatterns ?? [];
  if (patterns.length === 0) return tabId;
  const open = await chrome.tabs.query({ url: patterns }).catch(() => []);
  // Lowest id wins: Chrome hands them out in creation order, so this is the tab
  // that was open before FavHub added one.
  const existing = open
    .filter((tab) => tab.id !== tabId)
    .sort((left, right) => left.id - right.id)[0];
  if (existing === undefined) return tabId;
  await chrome.tabs.remove(tabId).catch(() => {});
  await chrome.tabs.update(existing.id, { active: true }).catch(() => {});
  // Making it the active tab of its own window is not enough: if that window is
  // behind another, the tab is still not visible, and Chrome throttles a hidden
  // tab hard enough that the page may never issue the request the run is
  // waiting for. A reused tab that collects nothing looks exactly like a broken
  // extension, so the window is raised too.
  if (existing.windowId !== undefined) {
    await chrome.windows.update(existing.windowId, { focused: true }).catch(() => {});
  }
  return existing.id;
}

/** Run an active-mode platform to completion, then close the session.
 *
 * The adapter owns the loop here, which is why nothing else drives it: a
 * cookie-authenticated platform needs no interception and no scrolling, so
 * there is no page event to react to.
 */
async function driveActiveRun(tabId) {
  const running = adapter;
  try {
    const result = await running.run();
    // A pause has already been reported with its stable code; finishing on top
    // of it would overwrite the reason the user needs to see.
    if (result && result.ok === false) return;
    if (adapter === running) await finishRun(tabId);
  } catch (error) {
    adapter = null;
    armedTabId = null;
    await controller.pause("browser_unavailable", describe(error).message).catch(() => {});
  }
}

/** Complete the run the moment the adapter says it reached its stopping line. */
async function finishRun(tabId) {
  const summary = adapter.summary();
  adapter = null;
  armedTabId = null;
  await chrome.tabs.sendMessage(tabId, { kind: "deactivate" }).catch(() => {});
  await controller.finish(summary);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  if (message.kind === "page.ready" && sender.tab) {
    const tabId = sender.tab.id;
    controller.tabId = tabId;
    tryClaim(message.platform, message.url).then(async (claimed) => {
      if (claimed && needsArmingReload(message.platform, tabId)) {
        // Resolving the duplicate first means the reload below lands on the tab
        // that will actually collect, so the hook is armed only once.
        const collecting = await preferExistingTab(
          message.platform,
          tabId,
          message.favhubOpened === true,
        );
        controller.tabId = collecting;
        armedTabId = collecting;
        // Answering "not claimed" leaves this doomed load alone: its hook could
        // only ever see requests X has already made.
        sendResponse({ claimed: false, patterns: [] });
        chrome.tabs.reload(collecting).catch(() => {});
        return;
      }
      sendResponse({ claimed, patterns: claimed && adapter ? adapter.patterns : [] });
      // An active-mode platform waits for nothing: it fetches its own pages, so
      // the run starts as soon as the page is there to fetch through.
      if (claimed && adapter !== null && adapter.mode === "active") {
        driveActiveRun(tabId).catch(() => {});
      }
    });
    return true;
  }
  if (message.kind === "capture.response") {
    if (!adapter) {
      // No active session: the hook should be dormant, so this is either a
      // race during teardown or a page trying its luck. Neither is collected.
      sendResponse({ accepted: false });
      return false;
    }
    const tabId = sender.tab ? sender.tab.id : controller.tabId;
    adapter
      .onResponse(message.url, message.body)
      .then(async (result) => {
        if (result.atEnd && adapter !== null) await finishRun(tabId);
        sendResponse(result);
      })
      .catch((error) => sendResponse({ accepted: false, error: describe(error) }));
    return true;
  }
  if (message.kind === "popup.status") {
    const snapshot = controller.snapshot;
    if (snapshot.error) {
      sendResponse(snapshot);
      return false;
    }
    // A failure to reach FavHub at all leaves no session to carry it, so the
    // reason lives in storage instead — and the popup asks *here*. Without this
    // join the popup showed a bare "inactive" while the cause sat in a store
    // nothing read, which is exactly how a broken install becomes unsolvable
    // for the person looking at it.
    chrome.storage.session
      .get("favhubSnapshot")
      .then((stored) => {
        sendResponse({ ...snapshot, error: stored?.favhubSnapshot?.error ?? null });
      })
      .catch(() => sendResponse(snapshot));
    return true;
  }
  if (message.kind === "popup.cancel") {
    adapter = null;
    // A later session on this same tab must arm itself again.
    armedTabId = null;
    controller
      .cancel()
      .then(() => sendResponse(controller.snapshot))
      .catch((error) => sendResponse({ ...controller.snapshot, error: describe(error) }));
    return true;
  }
  if (message.kind === "popup.pause") {
    controller
      .pause("browser_unavailable", "paused from the FavHub popup")
      .then(() => sendResponse(controller.snapshot))
      .catch((error) => sendResponse({ ...controller.snapshot, error: describe(error) }));
    return true;
  }
  return false;
});

export { controller };
