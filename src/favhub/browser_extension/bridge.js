// Isolated-world bridge: validation and platform rules.
//
// This is a real trust boundary. The page world shares an origin with the site,
// so a site can post anything it likes on the same channel — including a forged
// "here is a bookmarks response" message. Nothing arriving here is believed:
// shape, origin, size, and platform are all checked, and the platform is taken
// from the URL this script is running on rather than from the message.
//
// The deeper checks still happen in Python. This layer only keeps obvious junk
// off the wire.
//
// Deliberately a classic script, not an ES module, and declared in the manifest
// ahead of content-isolated.js so the two share one isolated-world scope. The
// obvious alternative — keeping this a module and reaching it with a dynamic
// `import(chrome.runtime.getURL(...))` — is what an earlier version did, and it
// never ran on x.com: Chromium applies the *page's* CSP to a dynamic import
// from an isolated world, so a site with a strict policy silently blocks it and
// the extension does nothing at all. Declaring both files removes that
// dependency, and keeps bridge.js out of web_accessible_resources as a bonus.
globalThis.FavHubBridge = (() => {
  const PAGE_CHANNEL = "favhub:page";
  const CONTROL_CHANNEL = "favhub:control";
  const MAX_BODY_BYTES = 2 * 1024 * 1024;

  const PLATFORM_BY_HOST = Object.freeze({
    "x.com": "x",
    "twitter.com": "x",
    "www.bilibili.com": "bilibili",
    // Favourites live on the account's own space page, which is also where the
    // account id the folder listing needs can be read from the URL.
    "space.bilibili.com": "bilibili",
    "www.zhihu.com": "zhihu",
  });

  // Every URL an active-mode adapter may ask the page to fetch, by platform.
  // The check lives here, next to the code that performs the request, because
  // the service worker naming a URL is not authority to fetch it: a compromised
  // or confused worker must not be able to turn the page into a fetch proxy.
  const ALLOWED_REQUESTS = Object.freeze({
    bilibili: [
      // Anchored: `/x/web-interface/nav` as a prefix would also admit
      // `/nav/stat` and anything else hanging off the account.
      /^https:\/\/api\.bilibili\.com\/x\/web-interface\/nav$/,
      "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
      "https://api.bilibili.com/x/v3/fav/resource/list",
      "https://api.bilibili.com/x/web-interface/view",
      "https://api.bilibili.com/x/player/v2",
      // Subtitle documents are served from a separate CDN, not the API host.
      // The path is pinned as well as the host: this domain is reachable only
      // for the transcript a saved video already carries.
      "https://aisubtitle.hdslb.com/bfs/",
    ],
    // Zhihu puts its identifiers in the path rather than the query, so these
    // are patterns and not prefixes: `/api/v4/collections/` as a prefix would
    // admit every neighbouring operation on a collection, not just reading it.
    zhihu: [
      // Anchored, not a prefix: `/api/v4/me` as one would also admit
      // `/api/v4/me/collections` and everything else hanging off the account.
      /^https:\/\/www\.zhihu\.com\/api\/v4\/me$/,
      /^https:\/\/www\.zhihu\.com\/api\/v4\/people\/[A-Za-z0-9_-]+\/collections$/,
      /^https:\/\/www\.zhihu\.com\/api\/v4\/collections\/\d+\/items$/,
    ],
    // X is collected passively; it never asks the page to fetch anything.
    x: [],
  });

  /** True only for a read-only collection endpoint this platform declares. */
  function isAllowedRequest(platform, url) {
    if (typeof url !== "string") return false;
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return false;
    }
    if (parsed.protocol !== "https:") return false;
    const allowed = ALLOWED_REQUESTS[platform] ?? [];
    // Matched against origin+path only: an expiring auth_key or a paging offset
    // lives in the query and must not decide whether a url is collectable.
    const base = `${parsed.origin}${parsed.pathname}`;
    return allowed.some((rule) =>
      typeof rule === "string" ? base === rule || base.startsWith(rule) : rule.test(base),
    );
  }

  // The sites whose own cookies a platform's requests may carry. Everything
  // else an adapter is allowed to reach — a CDN handing out signed urls — is a
  // third party, and third parties do not get the user's session.
  const CREDENTIALLED_SITES = Object.freeze({
    bilibili: ["bilibili.com"],
    zhihu: ["zhihu.com"],
    x: [],
  });

  /** Whether this request carries the user's session, or goes out bare.
   *
   * Both answers are load-bearing. The API authenticates by cookie and returns
   * an empty library without one; the subtitle CDN answers
   * `Access-Control-Allow-Origin: *`, which the Fetch standard rejects for a
   * credentialled request — so sending cookies there fails the request outright
   * rather than merely oversharing.
   */
  function sendsCredentials(platform, url) {
    if (typeof url !== "string") return false;
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return false;
    }
    const sites = CREDENTIALLED_SITES[platform] ?? [];
    // Suffix matching on a dot boundary, so `api.bilibili.com.evil.test` is
    // not mistaken for the platform.
    return sites.some((site) => parsed.host === site || parsed.host.endsWith(`.${site}`));
  }

  // How often a page may re-announce itself. A page load is not the only moment
  // FavHub can be waiting: the run is usually started from a chat window while
  // the platform tab sits in the background, so the tab becoming visible again
  // is the natural second chance. The interval exists because each announcement
  // that finds no session costs a native host launch — the worker has almost
  // certainly been shut down by then — and a user flipping between tabs would
  // otherwise start one every time.
  const ANNOUNCE_INTERVAL_MS = 30_000;

  /**
   * Rate-limited "this page is here" announcements.
   *
   * Re-announcing is safe by construction: with no session waiting the worker
   * answers no and stays dormant, and during a run it answers from memory
   * without touching the native host at all.
   *
   * @param {object} options
   * @param {() => Promise<unknown>} options.send performs one announcement
   * @param {() => number} [options.now]
   * @param {number} [options.intervalMs]
   */
  function createAnnouncer({ send, now = () => Date.now(), intervalMs = ANNOUNCE_INTERVAL_MS }) {
    let lastAt = null;
    let inFlight = false;
    return {
      /** @param {boolean} [force] bypass the interval, for the initial load */
      announce(force = false) {
        if (inFlight) return false;
        const at = now();
        if (!force && lastAt !== null && at - lastAt < intervalMs) return false;
        lastAt = at;
        inFlight = true;
        let pending;
        try {
          pending = send();
        } catch {
          // A send that fails on the spot must still clear the way for the
          // next attempt; FavHub simply not running is the ordinary case.
          inFlight = false;
          return true;
        }
        Promise.resolve(pending)
          .catch(() => {})
          .then(() => {
            inFlight = false;
          });
        return true;
      },
    };
  }

  /** Resolve the platform from the page's own host, never from the message. */
  function platformForHost(host) {
    return PLATFORM_BY_HOST[host] ?? null;
  }

  /** The element that actually scrolls, or null when the document itself does.
   *
   * A timeline is not always in the document scroller. X lays the bookmarks
   * list out inside a container exactly as tall as the viewport, so the
   * document has nothing to scroll and `window.scrollTo` moves nothing at all:
   * the first page arrived because the platform loaded it, and no page ever
   * followed because the run had, in effect, never scrolled. Measured on a live
   * run as `before=0 after=0 docH=1112 innerH=1112`.
   *
   * Finding the tallest scrollable element beats hard-coding a selector, which
   * a redesign would break silently — and silence is exactly the failure mode
   * that cost the most here.
   */
  function findScroller(view) {
    const doc = view.document;
    if (doc.documentElement.scrollHeight > view.innerHeight + 1) return null;
    let best = null;
    for (const node of doc.querySelectorAll("div,main,section")) {
      if (node.scrollHeight <= node.clientHeight + 1) continue;
      const overflow = view.getComputedStyle(node).overflowY;
      if (overflow !== "auto" && overflow !== "scroll") continue;
      if (best === null || node.scrollHeight > best.scrollHeight) best = node;
    }
    return best;
  }

  /** Scroll to the end of whichever element is doing the scrolling.
   *
   * Instantly, not smoothly: a smooth scroll is animated through
   * requestAnimationFrame, which Chrome pauses in a hidden tab, and a run must
   * not depend on the user keeping the tab in front.
   *
   * @returns a description of what moved, for the caller to report
   */
  function scrollOnce(view) {
    const target = findScroller(view);
    if (target === null) {
      const before = view.scrollY;
      const height = view.document.documentElement.scrollHeight;
      view.scrollTo({ top: height, behavior: "auto" });
      return { where: "document", before, after: view.scrollY, height };
    }
    const before = target.scrollTop;
    target.scrollTop = target.scrollHeight;
    return { where: "container", before, after: target.scrollTop, height: target.scrollHeight };
  }

  /** Keep asking for the end until the page has something more to show.
   *
   * One attempt is not enough. The adapter scrolls the instant it accepts a
   * response, which is before the platform has rendered those items — at that
   * moment the document is still exactly viewport-height and there is nothing
   * to scroll, so a single attempt moves nothing and the run stalls with no
   * error anywhere. Retrying costs a few hundred milliseconds and removes the
   * dependency on winning that race.
   *
   * @param report called with each attempt, for tracing a live run
   */
  function scrollToEnd(view, { delays = [0, 400, 1200, 2500], report = () => {} } = {}) {
    let attempt = 0;
    const tick = () => {
      const moved = scrollOnce(view);
      report({ attempt, ...moved });
      attempt += 1;
      if (attempt < delays.length) view.setTimeout(tick, delays[attempt] - delays[attempt - 1]);
    };
    if (delays[0] === 0) tick();
    else view.setTimeout(tick, delays[0]);
  }

  /**
   * Validate one page-world message.
   * @returns {{ok: true, value: {url: string, body: string}} | {ok: false, reason: string}}
   */
  function validatePageMessage(message, { origin, expectedOrigin, maxBytes = MAX_BODY_BYTES }) {
    if (origin !== expectedOrigin) return { ok: false, reason: "cross_origin" };
    if (!message || typeof message !== "object") return { ok: false, reason: "not_an_object" };
    if (message.channel !== PAGE_CHANNEL) return { ok: false, reason: "wrong_channel" };
    if (message.kind !== "response") return { ok: false, reason: "unknown_kind" };
    const detail = message.detail;
    if (!detail || typeof detail !== "object") return { ok: false, reason: "no_detail" };
    const { url, body } = detail;
    if (typeof url !== "string" || url.length === 0) return { ok: false, reason: "bad_url" };
    if (typeof body !== "string") return { ok: false, reason: "bad_body" };
    if (body.length > maxBytes) return { ok: false, reason: "too_large" };
    // A page could name any URL; only same-origin collection endpoints are
    // forwarded, so a forged absolute URL cannot smuggle in foreign content.
    if (!url.startsWith("/") && !url.startsWith(expectedOrigin)) {
      return { ok: false, reason: "foreign_url" };
    }
    return { ok: true, value: { url, body } };
  }

  function createBridge({ location, postToPage, sendToWorker, onReject = () => {} }) {
    const expectedOrigin = location.origin;
    const platform = platformForHost(location.host);

    return {
      platform,
      // Passive platforms need a page-world hook because their endpoints are
      // header-authenticated; active ones paginate from here and get no injection.
      passive: platform === "x",

      /** Turn the page-world hook on for one session. Passive platforms only. */
      activate(patterns) {
        if (platform !== "x") {
          // Active-mode platforms paginate from this isolated world; injecting a
          // page-world hook there would add reach for no capability.
          return false;
        }
        postToPage({ channel: CONTROL_CHANNEL, kind: "activate", patterns }, expectedOrigin);
        return true;
      },

      deactivate() {
        postToPage({ channel: CONTROL_CHANNEL, kind: "deactivate" }, expectedOrigin);
      },

      handlePageMessage(message, origin) {
        if (platform === null) return false;
        const checked = validatePageMessage(message, { origin, expectedOrigin });
        if (!checked.ok) {
          onReject(checked.reason);
          return false;
        }
        sendToWorker({
          kind: "capture.response",
          platform,
          url: checked.value.url,
          body: checked.value.body,
        });
        return true;
      },
    };
  }

  return {
    PAGE_CHANNEL,
    CONTROL_CHANNEL,
    ANNOUNCE_INTERVAL_MS,
    MAX_BODY_BYTES,
    createAnnouncer,
    platformForHost,
    validatePageMessage,
    createBridge,
    findScroller,
    isAllowedRequest,
    scrollOnce,
    scrollToEnd,
    sendsCredentials,
  };
})();
