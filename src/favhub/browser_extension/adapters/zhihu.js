// Zhihu collections adapter — active mode.
//
// Zhihu authenticates with same-origin cookies, so the page asks for its own
// next page: nothing is intercepted and no page-world code is injected. Same
// shape as the Bilibili adapter, and the opposite of X.
//
// One rule here is worth more than the rest: `paging.is_end` is the only end
// signal. Deleted favourites shrink pages below the limit *in the middle* of a
// collection, so a short page is ordinary. Stopping on one would truncate the
// scan and then advance a frontier past everything it never saw — the failure
// would look like a clean, successful, and much smaller library.
//
// Content parsing stays in Python: pages are forwarded verbatim. This file
// reads only what a run needs to decide — which collections exist, whether a
// page has a successor, and whether the previous run already had an item.

const LOGIN_ERROR_CODES = new Set([100, 101]);
const RATE_ERROR_CODES = new Set([4039]);

export const API_ORIGIN = "https://www.zhihu.com";
export const ME_ENDPOINT = `${API_ORIGIN}/api/v4/me`;
export const COLLECTIONS_PATH = "/collections";
export const ITEMS_PATH = "/items";

/** Zhihu's own page size; matching it keeps offsets aligned with its paging. */
export const PAGE_SIZE = 20;
/** Slow enough to stay a guest on someone else's service. */
export const REQUEST_INTERVAL_MS = 600;

/** True only on the account's own collections page.
 *
 * `/collections/mine` is where the account's own shelves are; bare
 * `/collections` is the discovery page. Both start a run, because a run only
 * needs the tab to be on the right site — the adapter fetches everything
 * through the API and never reads the page — but one collection's own page
 * (`/collections/<id>`) does not, since that is somebody's shelf being read
 * rather than a request to mirror the account.
 */
export function isCollectionsRoute(url) {
  if (typeof url !== "string") return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.host !== "www.zhihu.com") return false;
  const path = parsed.pathname.replace(/\/$/, "");
  return path === "/collections" || path === "/collections/mine";
}

export function collectionsUrl(urlToken, offset) {
  return (
    `${API_ORIGIN}/api/v4/people/${encodeURIComponent(urlToken)}/collections` +
    `?offset=${offset}&limit=${PAGE_SIZE}`
  );
}

export function itemsUrl(collectionId, offset) {
  return (
    `${API_ORIGIN}/api/v4/collections/${encodeURIComponent(collectionId)}/items` +
    `?offset=${offset}&limit=${PAGE_SIZE}`
  );
}

/**
 * Read one Zhihu response envelope.
 *
 * The codes produced here must agree with `favhub.zhihu_parsers`, which parses
 * the very same body again on the Python side: disagreeing would mean pausing
 * a run on something Python accepts, or collecting something it rejects.
 *
 * @returns {{ok: true, data: object} | {ok: false, code: string}}
 */
export function readEnvelope(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    return { ok: false, code: "page_changed" };
  }
  if (!parsed || typeof parsed !== "object") return { ok: false, code: "page_changed" };
  const error = parsed.error;
  if (error && typeof error === "object") {
    const message = typeof error.message === "string" ? error.message : "";
    const name = typeof error.name === "string" ? error.name : "";
    if (LOGIN_ERROR_CODES.has(error.code) || message.includes("登录") || name.includes("Authentication")) {
      return { ok: false, code: "login_required" };
    }
    if (RATE_ERROR_CODES.has(error.code) || message.includes("频繁") || message.includes("异常")) {
      return { ok: false, code: "rate_limited" };
    }
    return { ok: false, code: "page_changed" };
  }
  return { ok: true, data: parsed };
}

/** Structural facts one page carries; its contents stay for Python.
 *
 * A missing `paging.is_end` is refused rather than read as the end: a dropped
 * paging block would otherwise stop a scan silently.
 */
export function readItemsPage(data) {
  if (!data || typeof data !== "object" || !Array.isArray(data.data)) {
    return { ok: false, code: "page_changed" };
  }
  const paging = data.paging;
  if (!paging || typeof paging !== "object" || typeof paging.is_end !== "boolean") {
    return { ok: false, code: "page_changed" };
  }
  return { ok: true, entries: data.data, isEnd: paging.is_end };
}

/** The collections a run may collect, in the order Zhihu lists them. */
export function readCollectionsPage(data) {
  const page = readItemsPage(data);
  if (!page.ok) return page;
  const collections = [];
  for (const entry of page.entries) {
    if (!entry || typeof entry !== "object") continue;
    if (typeof entry.id !== "number" || typeof entry.title !== "string") continue;
    collections.push({ scopeId: String(entry.id), scopeName: entry.title });
  }
  return { ok: true, collections, isEnd: page.isEnd };
}

/**
 * The id Python will store this favourite under, or null.
 *
 * This mirrors `zhihu_mapping.source_id_for` — `{type}-{id}` — and has to: the
 * frontier FavHub hands back is a list of those ids, so a different spelling
 * here would silently never match and every incremental run would rescan the
 * whole library.
 */
export function sourceIdOf(entry) {
  const content = entry && typeof entry === "object" ? entry.content : null;
  if (!content || typeof content !== "object") return null;
  const type = typeof content.type === "string" ? content.type : "";
  const id = content.id;
  if (type === "" || (typeof id !== "number" && typeof id !== "string")) return null;
  return `${type}-${id}`;
}

/**
 * Drive one Zhihu run across every selected collection.
 *
 * @param {object} options
 * @param {object} options.controller session controller
 * @param {Record<string, string[]>} options.frontierScopes per-collection known ids
 * @param {number|null} options.maxScanItems
 * @param {(url: string) => Promise<{ok: boolean, body?: string, code?: string}>} options.request
 * @param {(ms: number) => Promise<void>} options.wait
 * @param {string[]} [options.only] collection ids to collect; all by default
 */
export function createZhihuAdapter({
  controller,
  frontierScopes = {},
  maxScanItems = null,
  request,
  wait,
  only = null,
}) {
  const scopeResults = {};
  const frontierByScope = {};
  // Tracked separately because a run's `observedEnd` is only true when every
  // collection reached its end; one truncated collection makes the run partial.
  const endedByScope = {};
  let scanned = 0;
  let maxScanReached = false;

  async function fetchJson(url) {
    const reply = await request(url);
    if (!reply || reply.ok !== true || typeof reply.body !== "string") {
      return { ok: false, code: reply?.code ?? "browser_unavailable" };
    }
    return { ...readEnvelope(reply.body), body: reply.body };
  }

  /** Stop the whole run on a platform condition, reporting the stable code. */
  async function fail(code, detail) {
    await controller.pause(code, detail);
    return { ok: false, code };
  }

  async function readUrlToken() {
    const envelope = await fetchJson(ME_ENDPOINT);
    if (!envelope.ok) return envelope;
    const token = envelope.data.url_token;
    if (typeof token !== "string" || token.length === 0) {
      return { ok: false, code: "page_changed" };
    }
    return { ok: true, token };
  }

  async function listCollections(token) {
    const collections = [];
    let offset = 0;
    for (;;) {
      const envelope = await fetchJson(collectionsUrl(token, offset));
      if (!envelope.ok) return envelope;
      const parsed = readCollectionsPage(envelope.data);
      if (!parsed.ok) return parsed;
      collections.push(...parsed.collections);
      if (parsed.isEnd) return { ok: true, collections };
      offset += PAGE_SIZE;
      await wait(REQUEST_INTERVAL_MS);
    }
  }

  async function collectScope(scope) {
    const known = new Set(frontierScopes[scope.scopeId] ?? []);
    const newest = [];
    let offset = 0;
    let observedEnd = false;
    let reachedFrontier = false;

    while (!observedEnd && !reachedFrontier && !maxScanReached) {
      const envelope = await fetchJson(itemsUrl(scope.scopeId, offset));
      if (!envelope.ok) return fail(envelope.code, `collection ${scope.scopeId} offset ${offset}`);
      const parsed = readItemsPage(envelope.data);
      if (!parsed.ok) return fail(parsed.code, `collection ${scope.scopeId} offset ${offset}`);

      for (const entry of parsed.entries) {
        const sourceId = sourceIdOf(entry);
        if (sourceId !== null && known.has(sourceId)) {
          reachedFrontier = true;
          break;
        }
        if (sourceId !== null && newest.length < 20) newest.push(sourceId);
        scanned += 1;
        if (maxScanItems !== null && scanned >= maxScanItems) {
          maxScanReached = true;
          break;
        }
      }

      // Forwarded whole and unaltered: what a page contains is Python's to
      // decide, and a page carrying the frontier still holds items above it.
      await controller.offer({
        kind: "zhihu.items_page",
        platform: "zhihu",
        scopeId: scope.scopeId,
        scopeName: scope.scopeName,
        body: envelope.body,
      });

      // The only end signal. A page shorter than the limit is not one.
      if (parsed.isEnd) observedEnd = true;
      offset += PAGE_SIZE;
      if (!observedEnd && !reachedFrontier && !maxScanReached) await wait(REQUEST_INTERVAL_MS);
    }

    scopeResults[scope.scopeId] = { maxScanReached, visibleTotal: null };
    // Reaching the frontier is a complete scan: everything below it was
    // collected by an earlier run.
    endedByScope[scope.scopeId] = observedEnd || reachedFrontier;
    // A truncated collection is left out of the frontier entirely rather than
    // given an empty one: FavHub refuses a scope that both reports a cap and
    // names a frontier, because the next incremental run would otherwise skip
    // everything this one never reached.
    if (!maxScanReached) frontierByScope[scope.scopeId] = newest;
    return { ok: true };
  }

  return {
    /** Active mode issues its own requests and injects no page-world code. */
    mode: "active",

    async run() {
      const identified = await readUrlToken();
      if (!identified.ok) return fail(identified.code, "identifying the account");
      await wait(REQUEST_INTERVAL_MS);

      const listed = await listCollections(identified.token);
      if (!listed.ok) return fail(listed.code, "listing collections");
      const selected =
        only === null
          ? listed.collections
          : listed.collections.filter((scope) => only.includes(scope.scopeId));
      // An account with no collections is indistinguishable from a library that
      // could not be read, and finishing here would advance a frontier past it.
      if (selected.length === 0) return fail("page_changed", "no collections were listed");

      // Collections are declared before the first page so Python can hand back
      // a frontier per collection; collecting first would have nothing to
      // compare against.
      const declared = await controller.declareScopes(selected);
      for (const [scopeId, ids] of Object.entries(declared ?? {})) {
        frontierScopes[scopeId] = ids;
      }

      for (const scope of selected) {
        if (maxScanReached) {
          // The cap was spent on an earlier collection; this one was never
          // looked at, so it is truncated rather than finished.
          scopeResults[scope.scopeId] = { maxScanReached: true, visibleTotal: null };
          endedByScope[scope.scopeId] = false;
          continue;
        }
        await wait(REQUEST_INTERVAL_MS);
        const collected = await collectScope(scope);
        if (!collected.ok) return collected;
      }
      return { ok: true };
    },

    summary() {
      const ends = Object.values(endedByScope);
      return {
        observedEnd: ends.length > 0 && ends.every(Boolean),
        maxScanReached,
        scanned,
        frontierIds: [],
        frontierScopes: frontierByScope,
        scopeResults,
      };
    },
  };
}
