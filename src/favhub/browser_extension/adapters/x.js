// X bookmarks adapter — passive mode.
//
// X authenticates its GraphQL endpoints with request headers the page holds, so
// building a request here would mean reading those credentials. This adapter
// therefore never issues a request: it observes the responses the page makes on
// its own while the user's bookmarks list scrolls, and forwards the bodies.
// X is the only platform that needs this; Bilibili and Zhihu authenticate with
// same-origin cookies and paginate themselves from the isolated world.
//
// Content parsing is not done here. This file reads only the structure needed
// to answer three questions — is there another page, have we reached what the
// last run already had, and did the platform hand us an error instead of a page
// — and leaves tweets, quotes, and tombstones to the Python parsers.

/** The page's own bookmark requests all live under the GraphQL prefix. */
export const RESPONSE_PATTERNS = ["/i/api/graphql/"];

const BOOKMARKS_OPERATION = /\/i\/api\/graphql\/[A-Za-z0-9_-]+\/Bookmarks(?:$|[?#])/;
const ALLOWED_HOSTS = new Set(["x.com", "twitter.com", "www.x.com", "www.twitter.com"]);
const BOOKMARKS_PATH = /^\/i\/bookmarks(?:\/[A-Za-z0-9_-]*)?\/?$/;

// X reuses one error envelope for very different situations; only these two are
// worth resuming from, everything else is a schema the adapter no longer knows.
const LOGIN_CODES = new Set([32, 89, 215, 353]);
const RATE_LIMIT_CODES = new Set([88, 130, 420]);

// How many of the newest ids become the next run's stopping line.
//
// More than one, because a frontier is only as good as its weakest id: the
// next incremental run stops when it *recognises* something, and a single id
// stops recognising the moment that one bookmark is removed. The run then
// scrolls the whole timeline to reach an end it was supposed to skip — 1317
// scanned pages for a 499-item library, measured on a live run. Twenty ids
// means twenty removals in a row before that happens.
const FRONTIER_ID_LIMIT = 20;

/** True only for the page's own Bookmarks query. */
export function matchesBookmarksResponse(url) {
  if (typeof url !== "string" || url.length === 0) return false;
  const path = pathOf(url);
  if (path === null) return false;
  return BOOKMARKS_OPERATION.test(path);
}

/** True only on the bookmarks route itself, so other pages never collect. */
export function isBookmarksRoute(url) {
  if (typeof url !== "string") return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (!ALLOWED_HOSTS.has(parsed.host)) return false;
  return BOOKMARKS_PATH.test(parsed.pathname);
}

function pathOf(url) {
  if (url.startsWith("/")) return url;
  try {
    const parsed = new URL(url);
    if (!ALLOWED_HOSTS.has(parsed.host)) return null;
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return null;
  }
}

/**
 * Read the structural facts a run needs from one response body.
 * @returns {{kind:"page", bottomCursor: string|null, tweetIds: string[], atEnd: boolean}
 *          |{kind:"error", code: string}}
 */
export function inspectPage(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    return { kind: "error", code: "page_changed" };
  }
  if (!parsed || typeof parsed !== "object") {
    return { kind: "error", code: "page_changed" };
  }
  if (Array.isArray(parsed.errors) && parsed.errors.length > 0) {
    return { kind: "error", code: envelopeCode(parsed.errors) };
  }
  const instructions = parsed?.data?.bookmark_timeline_v2?.timeline?.instructions;
  if (!Array.isArray(instructions)) {
    // A renamed container is exactly the case that must not read as "no more
    // bookmarks"; saying page_changed keeps the frontier where it is.
    return { kind: "error", code: "page_changed" };
  }
  const tweetIds = [];
  let bottomCursor = null;
  for (const instruction of instructions) {
    const entries = Array.isArray(instruction?.entries) ? instruction.entries : [];
    for (const entry of entries) {
      const entryId = typeof entry?.entryId === "string" ? entry.entryId : "";
      if (entryId.startsWith("tweet-")) {
        tweetIds.push(entryId.slice("tweet-".length));
      } else if (entryId.startsWith("cursor-bottom-")) {
        bottomCursor = entryId;
      }
    }
  }
  return { kind: "page", bottomCursor, tweetIds, atEnd: bottomCursor === null };
}

function envelopeCode(errors) {
  for (const error of errors) {
    const code = typeof error?.code === "number" ? error.code : null;
    if (code !== null && LOGIN_CODES.has(code)) return "login_required";
    if (code !== null && RATE_LIMIT_CODES.has(code)) return "rate_limited";
    const message = String(error?.message ?? "").toLowerCase();
    if (message.includes("rate limit")) return "rate_limited";
    if (message.includes("authenticate") || message.includes("logged out")) {
      return "login_required";
    }
  }
  return "page_changed";
}

/**
 * Drive one bookmarks run.
 *
 * @param {object} options
 * @param {{offer: Function, pause: Function}} options.controller
 * @param {string[]} options.frontier ids the previous run already confirmed
 * @param {number|null} options.maxScanItems smoke-run bound
 * @param {(reason: string) => Promise<void>} options.scroll
 */
export function createXAdapter({ controller, frontier = [], maxScanItems = null, scroll }) {
  const known = new Set(frontier);
  const seenCursors = new Set();
  const newestIds = [];
  let scanned = 0;
  let observedEnd = false;
  let maxScanReached = false;

  async function stop(reason) {
    observedEnd = true;
    return { accepted: true, atEnd: true, reason };
  }

  return {
    /** The patterns the page-world hook should watch for this session. */
    patterns: RESPONSE_PATTERNS,

    async onResponse(url, body) {
      if (observedEnd) return { accepted: false, atEnd: true, reason: "already_finished" };
      if (!matchesBookmarksResponse(url)) {
        // Some other GraphQL call the page happened to make; not ours to read.
        return { accepted: false, atEnd: false, reason: "not_bookmarks" };
      }

      const page = inspectPage(body);
      if (page.kind === "error") {
        await controller.pause(page.code, "X returned an error envelope instead of a page.");
        return { accepted: false, atEnd: false, reason: page.code };
      }

      // The body is forwarded verbatim and alone: no url, no headers, no
      // request body. Python decides what any of it means.
      await controller.offer({ kind: "x.bookmarks_page", platform: "x", body });

      if (newestIds.length < FRONTIER_ID_LIMIT) {
        newestIds.push(...page.tweetIds.slice(0, FRONTIER_ID_LIMIT - newestIds.length));
      }
      scanned += page.tweetIds.length;

      if (page.tweetIds.some((id) => known.has(id))) {
        return stop("frontier_reached");
      }
      if (page.atEnd) {
        return stop("observable_end");
      }
      if (maxScanItems !== null && scanned >= maxScanItems) {
        maxScanReached = true;
        return stop("max_scan_reached");
      }
      if (seenCursors.has(page.bottomCursor)) {
        // The same cursor twice means scrolling is no longer producing new
        // pages; continuing would spin against the platform for nothing.
        return stop("cursor_repeated");
      }
      seenCursors.add(page.bottomCursor);

      // Only now: the page is safely handed over, so asking for more cannot
      // outrun what FavHub has accepted.
      await scroll("next_page");
      return { accepted: true, atEnd: false, reason: "scrolled" };
    },

    summary() {
      return {
        observedEnd,
        maxScanReached,
        scanned,
        // Only the newest confirmed ids; a truncated run must not advance this.
        frontierIds: maxScanReached ? [] : newestIds.slice(0, FRONTIER_ID_LIMIT),
      };
    },
  };
}
