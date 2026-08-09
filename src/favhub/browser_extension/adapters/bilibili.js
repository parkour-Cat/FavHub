// Bilibili favourites adapter — active mode.
//
// Bilibili authenticates with same-origin cookies, so the page can simply ask
// for the next page itself. That makes this the opposite shape from the X
// adapter: nothing is intercepted, no page-world code is injected, and the run
// is driven from here rather than reacting to whatever the site happens to
// fetch. §3.2 of the design makes active pagination the default; X is the
// exception, not the pattern.
//
// Requests are read-only GETs to a fixed endpoint list, issued through the
// content script so the user's cookies travel with them and nothing here ever
// reads a credential. The content script re-checks every URL against the same
// allowlist — see bridge.js — because naming a URL is not permission to fetch
// it.
//
// Content parsing stays in Python. This file reads only what a run needs to
// decide: which folders exist, whether a page has a successor, and whether the
// previous run already had this video.

/** Bilibili returns its own error envelope with HTTP 200, so code is read. */
const LOGIN_CODES = new Set([-101, -400]);

export const API_ORIGIN = "https://api.bilibili.com";
// Who the logged-in user is. Reading the account id here rather than off the
// page URL is what lets FavHub open the page itself: the favourites route is
// under the account's own id, which FavHub cannot know in advance, but the
// home page is a fixed address. Zhihu is identified the same way.
export const NAV_ENDPOINT = `${API_ORIGIN}/x/web-interface/nav`;
export const FOLDERS_ENDPOINT = `${API_ORIGIN}/x/v3/fav/folder/created/list-all`;
export const RESOURCES_ENDPOINT = `${API_ORIGIN}/x/v3/fav/resource/list`;
export const DETAIL_ENDPOINT = `${API_ORIGIN}/x/web-interface/view`;
export const PLAYER_ENDPOINT = `${API_ORIGIN}/x/player/v2`;

/** Bilibili serves 20 per page and rejects larger; matching it keeps pages whole. */
export const PAGE_SIZE = 20;
/** Slow enough to stay a guest on someone else's service. */
export const REQUEST_INTERVAL_MS = 600;

const FAVLIST_PATH = /^\/(\d+)\/favlist\/?$/;

/** True where a run may start: the site's home page, or a favourites page.
 *
 * The home page counts because the account id no longer comes from the route.
 * That matters for more than tidiness — a fixed url is the only kind FavHub can
 * open on its own, and opening the page is the only way it can wake a sleeping
 * extension. A single collection's page is not a start signal: that is somebody
 * reading a shelf, not asking for the account to be mirrored.
 */
export function isCollectionRoute(url) {
  if (typeof url !== "string") return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.host === "www.bilibili.com") return parsed.pathname.replace(/\/$/, "") === "";
  return accountIdFromUrl(url) !== null;
}

/** The account id the folder listing needs, read from the page's own URL.
 *
 * Reading it here rather than asking an identity endpoint keeps the endpoint
 * list to the four that actually carry saved items.
 */
export function accountIdFromUrl(url) {
  if (typeof url !== "string") return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.host !== "space.bilibili.com") return null;
  const matched = FAVLIST_PATH.exec(parsed.pathname);
  return matched === null ? null : matched[1];
}

export function navUrl() {
  return NAV_ENDPOINT;
}

/**
 * The logged-in account's id, or null when nobody is logged in.
 *
 * A response that says "not logged in" must never yield an id: collecting under
 * the wrong mid would mirror a stranger's public favourites into the user's own
 * library, which is worse than collecting nothing.
 */
export function readAccountId(data) {
  if (!data || typeof data !== "object") return null;
  if (data.isLogin !== true) return null;
  const mid = data.mid;
  if (typeof mid !== "number" || !Number.isInteger(mid) || mid <= 0) return null;
  return String(mid);
}

export function foldersUrl(accountId) {
  return `${FOLDERS_ENDPOINT}?up_mid=${encodeURIComponent(accountId)}`;
}

export function resourcesUrl(folderId, page) {
  return (
    `${RESOURCES_ENDPOINT}?media_id=${encodeURIComponent(folderId)}` +
    `&pn=${page}&ps=${PAGE_SIZE}&platform=web`
  );
}

export function detailUrl(bvid) {
  return `${DETAIL_ENDPOINT}?bvid=${encodeURIComponent(bvid)}`;
}

export function playerUrl(bvid, cid) {
  return `${PLAYER_ENDPOINT}?bvid=${encodeURIComponent(bvid)}&cid=${encodeURIComponent(cid)}`;
}

/** Absolute https url for a track, or null when there is nothing to fetch.
 *
 * Bilibili returns the track protocol-relative (`//host/...`). Inheriting the
 * page's scheme is the usual reading, but the collector never wants http, so
 * the scheme is named rather than borrowed.
 */
export function subtitleDocumentUrl(raw) {
  if (typeof raw !== "string" || raw.length === 0) return null;
  return raw.startsWith("//") ? `https:${raw}` : raw;
}

/**
 * Pick the one subtitle worth storing, or null when there is none.
 *
 * A video can carry several tracks. A human-authored one is preferred over an
 * AI transcript because it is what the uploader meant; a locked track is
 * skipped because its document is not served to us anyway.
 */
export function readSubtitleTrack(data) {
  const subtitle = data && typeof data === "object" ? data.subtitle : null;
  const tracks = subtitle && Array.isArray(subtitle.subtitles) ? subtitle.subtitles : [];
  const usable = tracks.filter(
    (track) =>
      track &&
      typeof track === "object" &&
      track.is_lock !== true &&
      typeof track.subtitle_url === "string" &&
      track.subtitle_url.length > 0,
  );
  if (usable.length === 0) return null;
  const chosen = usable.find((track) => track.ai_type !== 1) ?? usable[0];
  return {
    url: chosen.subtitle_url,
    lan: typeof chosen.lan === "string" ? chosen.lan : "unknown",
  };
}

/** The name of the subtitle object a track points at, without its signature.
 *
 * Which video a transcript belongs to is not otherwise recoverable: the
 * document names its language and nothing else, so a transcript from the wrong
 * video is indistinguishable from a right one until somebody reads it. The
 * object name is the only provenance on offer, and it is worth keeping for its
 * own sake — but it is also what tells a wrong url apart from a wrong object
 * served for a right one.
 *
 * The query string is dropped. It carries an expiring `auth_key` that grants
 * the read, and a signature has no place in a stored library.
 */
export function subtitleObjectName(raw) {
  const url = subtitleDocumentUrl(raw);
  if (url === null) return null;
  try {
    return new URL(url).pathname;
  } catch {
    return null;
  }
}

/** True when a subtitle object is the one belonging to this video.
 *
 * Bilibili names these objects `<aid><cid><hash>`, so the object says whose
 * words it holds before a byte of it is downloaded. That is the only check that
 * catches this: the player response echoes back the right bvid and cid and then
 * names an object belonging to some other video, and the CDN faithfully serves
 * the object it was asked for. Measured on three captures with the name
 * recorded — two named the cid that was asked about, and the third named a
 * different video's, which is exactly the one whose transcript was wrong.
 *
 * Containment rather than a prefix, so that a change in how the name is laid
 * out costs nothing. A cid is eleven digits; it does not appear by accident.
 */
export function subtitleObjectBelongsTo(name, cid) {
  if (typeof name !== "string" || name.length === 0) return false;
  return name.includes(String(cid));
}

/**
 * True when a player response is about the video that was asked for.
 *
 * The endpoint echoes `bvid`, `aid` and `cid` back, and until this was checked
 * nothing noticed when they disagreed with the request. Nine of the thirteen
 * videos collected with a transcript by the first runs of this adapter got a
 * transcript belonging to some other video — a marketing video came back with
 * League of Legends commentary, an AI tutorial with a truck vlog — and each
 * wrong transcript belonged to a video this library had never asked about, so
 * nothing downstream could have caught it either. A transcript is the bulk of
 * what is indexed for a video, and one from the wrong video is worse than none.
 *
 * Only a present and contradicting echo refuses. A field that is absent is not
 * evidence of a mismatch, and refusing on absence would turn any reshaping of
 * the response into a silent library-wide loss of transcripts.
 */
export function playerAnswersAbout(data, bvid, cid) {
  if (!data || typeof data !== "object") return false;
  if (typeof data.bvid === "string" && data.bvid !== bvid) return false;
  if (typeof data.cid === "number" && data.cid !== cid) return false;
  return true;
}

/**
 * Read the envelope Bilibili wraps every response in.
 * @returns {{ok: true, data: object} | {ok: false, code: string}}
 */
export function readEnvelope(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    // An HTML login page rather than JSON is the usual cause, and it must not
    // read as "no favourites".
    return { ok: false, code: "login_required" };
  }
  if (!parsed || typeof parsed !== "object") return { ok: false, code: "page_changed" };
  const code = parsed.code;
  if (code === 0) {
    return parsed.data && typeof parsed.data === "object"
      ? { ok: true, data: parsed.data }
      : { ok: false, code: "page_changed" };
  }
  if (LOGIN_CODES.has(code)) return { ok: false, code: "login_required" };
  return { ok: false, code: "page_changed" };
}

/** The folders a run may collect, in the order Bilibili lists them. */
export function readFolders(data) {
  const list = Array.isArray(data.list) ? data.list : [];
  const folders = [];
  for (const entry of list) {
    if (!entry || typeof entry !== "object") continue;
    if (typeof entry.id !== "number" || typeof entry.title !== "string") continue;
    folders.push({ scopeId: String(entry.id), scopeName: entry.title });
  }
  return folders;
}

/** Structural facts one resource page carries; contents stay for Python. */
export function readResourcePage(data) {
  const medias = Array.isArray(data.medias) ? data.medias : [];
  const entries = [];
  for (const media of medias) {
    if (!media || typeof media !== "object") continue;
    const bvid = typeof media.bvid === "string" ? media.bvid : media.bv_id;
    if (typeof bvid !== "string" || bvid.length === 0) continue;
    entries.push({ bvid, media });
  }
  return { entries, hasMore: data.has_more === true };
}

/**
 * Drive one Bilibili run across every selected folder.
 *
 * @param {object} options
 * @param {object} options.controller session controller
 * @param {string} options.accountId from the page URL
 * @param {Record<string, string[]>} options.frontierScopes per-folder known ids
 * @param {number|null} options.maxScanItems
 * @param {(url: string) => Promise<{ok: boolean, body?: string, code?: string}>} options.request
 * @param {(ms: number) => Promise<void>} options.wait
 * @param {string[]} [options.only] folder ids to collect; all folders by default
 */
export function createBilibiliAdapter({
  controller,
  accountId = null,
  frontierScopes = {},
  maxScanItems = null,
  request,
  wait,
  only = null,
}) {
  const scopeResults = {};
  const frontierByScope = {};
  // Tracked separately because a run's `observedEnd` is only true when every
  // folder reached its end; one truncated folder makes the whole run partial.
  const endedByScope = {};
  let scanned = 0;
  let maxScanReached = false;

  async function fetchJson(url) {
    const reply = await request(url);
    if (!reply || reply.ok !== true || typeof reply.body !== "string") {
      return { ok: false, code: reply?.code ?? "browser_unavailable" };
    }
    return readEnvelope(reply.body);
  }

  /**
   * The subtitle for one video, or a pair of nulls.
   *
   * Every step here is best-effort. A video with no transcript, a track whose
   * signed url expired, or a document that is not a cue list must each cost
   * their own text and nothing more — a run that stopped on any of them would
   * be stopped by most libraries.
   */
  async function fetchSubtitle(bvid, detail) {
    const empty = {
      subtitle: null,
      subtitleRaw: null,
      subtitleSource: null,
      subtitleMismatch: null,
      subtitleRequestedCid: null,
      subtitleTrackCount: null,
    };
    const cid = detail.ok ? detail.data.cid : null;
    if (typeof cid !== "number") return empty;

    await wait(REQUEST_INTERVAL_MS);
    const player = await fetchJson(playerUrl(bvid, cid));
    if (!player.ok) return empty;
    // Asking correctly is not the same as being answered correctly, and a
    // transcript is the one field here big enough to bury the mistake.
    if (!playerAnswersAbout(player.data, bvid, cid)) return empty;
    const track = readSubtitleTrack(player.data);
    if (track === null) return empty;
    // The echo above can be right while this is wrong, and this is the one that
    // decides whose words get downloaded.
    const source = subtitleObjectName(track.url);
    if (!subtitleObjectBelongsTo(source, cid)) {
      // Refusing silently would store as "this video has no transcript", which
      // is a different and much less interesting fact than "this video was
      // offered somebody else's". Both the name that was offered and the cid it
      // was measured against travel, because without the second one a refusal
      // cannot distinguish being answered wrongly from having asked wrongly —
      // the detail response supplies this cid, and if it were the wrong video's
      // then every check downstream agrees with itself and still ends up here.
      return {
        ...empty,
        subtitleMismatch: source ?? "",
        subtitleRequestedCid: String(cid),
        subtitleTrackCount: (player.data?.subtitle?.subtitles ?? []).length,
      };
    }

    await wait(REQUEST_INTERVAL_MS);
    // The document comes from a CDN and carries no Bilibili envelope, so it is
    // read directly rather than through fetchJson.
    const reply = await request(subtitleDocumentUrl(track.url));
    if (!reply || reply.ok !== true || typeof reply.body !== "string") return empty;
    let document;
    try {
      document = JSON.parse(reply.body);
    } catch {
      return empty;
    }
    if (!document || typeof document !== "object" || !Array.isArray(document.body)) return empty;
    // Passed through unchanged: the document names its own language in `lang`,
    // which is what the Python parser reads.
    return { subtitle: document, subtitleRaw: reply.body, subtitleSource: source };
  }

  /** Stop the whole run on a platform condition, reporting the stable code. */
  async function fail(code, detail) {
    await controller.pause(code, detail);
    return { ok: false, code };
  }

  async function collectFolder(folder) {
    const known = new Set(frontierScopes[folder.scopeId] ?? []);
    const newest = [];
    let page = 1;
    let observedEnd = false;
    let reachedFrontier = false;

    while (!observedEnd && !reachedFrontier && !maxScanReached) {
      const envelope = await fetchJson(resourcesUrl(folder.scopeId, page));
      if (!envelope.ok) return fail(envelope.code, `folder ${folder.scopeId} page ${page}`);

      const { entries, hasMore } = readResourcePage(envelope.data);
      if (entries.length === 0) {
        observedEnd = true;
        break;
      }

      for (const entry of entries) {
        if (known.has(entry.bvid)) {
          reachedFrontier = true;
          break;
        }
        if (newest.length < 20) newest.push(entry.bvid);

        // Detail is best-effort: a video that is gone or hidden must not stop a
        // folder, so its absence travels as a bundle without detail and the
        // Python side records what it could.
        await wait(REQUEST_INTERVAL_MS);
        const detail = await fetchJson(detailUrl(entry.bvid));
        const subtitleResult = await fetchSubtitle(entry.bvid, detail);

        await controller.offer({
          kind: "bilibili.video_bundle",
          platform: "bilibili",
          scopeId: folder.scopeId,
          scopeName: folder.scopeName,
          resource: entry.media,
          detail: detail.ok ? { code: 0, data: detail.data } : null,
          ...subtitleResult,
        });

        scanned += 1;
        if (maxScanItems !== null && scanned >= maxScanItems) {
          maxScanReached = true;
          break;
        }
      }

      if (!hasMore) observedEnd = true;
      page += 1;
      if (!observedEnd && !reachedFrontier && !maxScanReached) await wait(REQUEST_INTERVAL_MS);
    }

    scopeResults[folder.scopeId] = {
      maxScanReached,
      visibleTotal: null,
    };
    // Reaching the frontier is a complete scan: everything below it was
    // collected by an earlier run. Without this, every folder an incremental
    // run stops early — which is all of them, that being the point — reports
    // "not ended", and the run is filed as `partial`. The Skill then tells the
    // user the scan was cut short by a cap that was never set.
    endedByScope[folder.scopeId] = observedEnd || reachedFrontier;
    // A truncated folder is left out of the frontier entirely rather than
    // given an empty one: FavHub refuses a scope that both reports a cap and
    // names a frontier, because the next incremental run would otherwise skip
    // everything this one never reached.
    if (!maxScanReached) frontierByScope[folder.scopeId] = newest;
    return { ok: true };
  }

  return {
    /** Active mode issues its own requests and injects no page-world code. */
    mode: "active",

    /** Resolve who the run belongs to, preferring the platform's own answer. */
    async identify() {
      const envelope = await fetchJson(navUrl());
      if (!envelope.ok) return envelope;
      const identified = readAccountId(envelope.data);
      // A page that answers but names nobody is a logged-out session, not a
      // changed page: the endpoint is fine, the user is not signed in.
      if (identified === null) return { ok: false, code: "login_required" };
      accountId = identified;
      return { ok: true, accountId: identified };
    },

    async listFolders() {
      const envelope = await fetchJson(foldersUrl(accountId));
      if (!envelope.ok) return fail(envelope.code, "folder list");
      const folders = readFolders(envelope.data);
      const selected = only === null ? folders : folders.filter((f) => only.includes(f.scopeId));
      return { ok: true, folders: selected };
    },

    async run() {
      const identified = await this.identify();
      if (!identified.ok) return fail(identified.code, "identifying the account");
      await wait(REQUEST_INTERVAL_MS);

      const listed = await this.listFolders();
      if (!listed.ok) return listed;
      if (listed.folders.length === 0) return fail("page_changed", "no folders were listed");

      // Folders are declared before the first bundle so Python can hand back a
      // frontier per folder; collecting first would have nothing to compare to.
      const declared = await controller.declareScopes(listed.folders);
      for (const [scopeId, ids] of Object.entries(declared ?? {})) {
        frontierScopes[scopeId] = ids;
      }

      for (const folder of listed.folders) {
        if (maxScanReached) {
          // The cap was spent on an earlier folder; this one was never looked
          // at, so it is truncated rather than finished.
          scopeResults[folder.scopeId] = { maxScanReached: true, visibleTotal: null };
          endedByScope[folder.scopeId] = false;
          continue;
        }
        const collected = await collectFolder(folder);
        if (!collected.ok) return collected;
        await wait(REQUEST_INTERVAL_MS);
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
