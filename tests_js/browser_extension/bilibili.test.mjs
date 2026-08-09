// Bilibili is active-mode: it issues its own requests rather than watching the
// page. That makes the request list itself part of the contract — what it asks
// for, how often, and what it refuses to ask for — so those are pinned here
// alongside the pagination rules.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInThisContext } from "node:vm";

import {
  DETAIL_ENDPOINT,
  FOLDERS_ENDPOINT,
  PAGE_SIZE,
  PLAYER_ENDPOINT,
  REQUEST_INTERVAL_MS,
  RESOURCES_ENDPOINT,
  NAV_ENDPOINT,
  accountIdFromUrl,
  createBilibiliAdapter,
  detailUrl,
  foldersUrl,
  isCollectionRoute,
  navUrl,
  playerAnswersAbout,
  playerUrl,
  readEnvelope,
  readFolders,
  readResourcePage,
  readAccountId,
  readSubtitleTrack,
  resourcesUrl,
  subtitleDocumentUrl,
  subtitleObjectBelongsTo,
} from "../../src/favhub/browser_extension/adapters/bilibili.js";

runInThisContext(
  readFileSync(new URL("../../src/favhub/browser_extension/bridge.js", import.meta.url), "utf8"),
);
const { isAllowedRequest, sendsCredentials } = globalThis.FavHubBridge;

const FAVLIST = "https://space.bilibili.com/90000001/favlist";

function envelope(data) {
  return JSON.stringify({ code: 0, message: "0", ttl: 1, data });
}

function media(bvid, extra = {}) {
  return { bvid, title: `video ${bvid}`, upper: { mid: 1, name: "u" }, ...extra };
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

  const adapter = createBilibiliAdapter({
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

/** Answers the folder list, then one page per folder, then details. */
function standardReplies({ folders, pages }) {
  return (url) => {
    if (url.startsWith(NAV_ENDPOINT)) {
      return { ok: true, body: envelope({ isLogin: true, mid: 90000001 }) };
    }
    if (url.startsWith(FOLDERS_ENDPOINT)) return { ok: true, body: envelope({ list: folders }) };
    if (url.startsWith(RESOURCES_ENDPOINT)) {
      const parsed = new URL(url);
      const key = `${parsed.searchParams.get("media_id")}:${parsed.searchParams.get("pn")}`;
      const page = pages[key];
      return page === undefined
        ? { ok: true, body: envelope({ medias: [], has_more: false }) }
        : { ok: true, body: envelope(page) };
    }
    if (url.startsWith(DETAIL_ENDPOINT)) {
      return { ok: true, body: envelope({ bvid: "BV1", title: "t" }) };
    }
    return null;
  };
}

// -- routing and identity -----------------------------------------------------

test("a run starts from the home page as well as the favourites page", () => {
  // The account id comes from the identity endpoint, not the route, so the tab
  // no longer has to be somewhere FavHub cannot name in advance. The home page
  // is a fixed url, which is what lets a run open its own page.
  assert.equal(isCollectionRoute("https://www.bilibili.com"), true);
  assert.equal(isCollectionRoute("https://www.bilibili.com/"), true);
  assert.equal(isCollectionRoute(FAVLIST), true);
  assert.equal(isCollectionRoute(`${FAVLIST}?fid=1`), true);
  for (const url of [
    "https://www.bilibili.com/video/BV1",
    "https://space.bilibili.com/90000001",
    "https://space.bilibili.com/90000001/video",
    "https://space.bilibili.example.com/1/favlist",
    "not a url",
  ]) {
    assert.equal(isCollectionRoute(url), false, url);
  }
});

test("the account id is read from the identity endpoint, never guessed", () => {
  assert.equal(readAccountId({ isLogin: true, mid: 90000001 }), "90000001");
  // A response that says "not logged in" must never yield an id: collecting
  // with the wrong mid would mirror somebody else's public favourites.
  assert.equal(readAccountId({ isLogin: false, mid: 0 }), null);
  assert.equal(readAccountId({ isLogin: true }), null);
  assert.equal(readAccountId({}), null);
  assert.equal(readAccountId(null), null);
});

test("the favourites route still yields an id, for a tab the user opened", () => {
  assert.equal(accountIdFromUrl(FAVLIST), "90000001");
  assert.equal(accountIdFromUrl("https://www.bilibili.com"), null);
});

// -- the request allowlist ----------------------------------------------------

test("every url the adapter builds is one the page is allowed to fetch", () => {
  for (const url of [
    navUrl(),
    foldersUrl("42"),
    resourcesUrl("108963847", 3),
    detailUrl("BV1bkz2gvaz6"),
  ]) {
    assert.equal(isAllowedRequest("bilibili", url), true, url);
  }
});

test("nothing outside the collection endpoints is allowed", () => {
  for (const url of [
    "https://api.bilibili.com/x/v3/fav/resource/deal",
    "https://api.bilibili.com/x/web-interface/nav/stat",
    "https://passport.bilibili.com/login",
    "https://evil.example.com/x/v3/fav/resource/list",
    "http://api.bilibili.com/x/v3/fav/resource/list",
    "javascript:alert(1)",
  ]) {
    assert.equal(isAllowedRequest("bilibili", url), false, url);
  }
});

test("a platform collected passively may not ask the page to fetch at all", () => {
  assert.equal(isAllowedRequest("x", "https://x.com/i/api/graphql/abc/Bookmarks"), false);
});

// -- envelopes ----------------------------------------------------------------

test("a login page instead of json reads as login_required, never as empty", () => {
  // Treating this as "no favourites" would advance a frontier past everything.
  assert.deepEqual(readEnvelope("<!doctype html><title>登录</title>"), {
    ok: false,
    code: "login_required",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ code: -101, data: null })), {
    ok: false,
    code: "login_required",
  });
});

test("an unexpected envelope reads as page_changed", () => {
  assert.deepEqual(readEnvelope(JSON.stringify({ code: -509 })), {
    ok: false,
    code: "page_changed",
  });
  assert.deepEqual(readEnvelope(JSON.stringify({ code: 0 })), { ok: false, code: "page_changed" });
});

test("folders without an id or title are skipped rather than guessed at", () => {
  const folders = readFolders({
    list: [
      { id: 1, title: "默认收藏夹" },
      { id: 2 },
      { title: "no id" },
      null,
      { id: 3, title: "技术" },
    ],
  });
  assert.deepEqual(folders, [
    { scopeId: "1", scopeName: "默认收藏夹" },
    { scopeId: "3", scopeName: "技术" },
  ]);
});

test("a media entry without a bvid is skipped, and bv_id is accepted", () => {
  const page = readResourcePage({
    medias: [media("BV1"), { title: "no id" }, { bv_id: "BV2", title: "old key" }],
    has_more: true,
  });
  assert.deepEqual(
    page.entries.map((entry) => entry.bvid),
    ["BV1", "BV2"],
  );
  assert.equal(page.hasMore, true);
});

// -- pagination ---------------------------------------------------------------

test("folders are declared before any bundle is offered", async () => {
  const { adapter, offered, scopes } = harness({
    replies: standardReplies({
      folders: [{ id: 7, title: "默认收藏夹" }],
      pages: { "7:1": { medias: [media("BV1")], has_more: false } },
    }),
  });

  await adapter.run();

  assert.deepEqual(scopes(), [{ scopeId: "7", scopeName: "默认收藏夹" }]);
  assert.equal(offered.length, 1);
  assert.equal(offered[0].scopeId, "7");
  assert.equal(offered[0].scopeName, "默认收藏夹");
});

test("pages are requested by incrementing pn until has_more is false", async () => {
  const { adapter, requested } = harness({
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      pages: {
        "7:1": { medias: [media("BV1")], has_more: true },
        "7:2": { medias: [media("BV2")], has_more: false },
      },
    }),
  });

  await adapter.run();

  const pages = requested
    .filter((url) => url.startsWith(RESOURCES_ENDPOINT))
    .map((url) => new URL(url).searchParams.get("pn"));
  assert.deepEqual(pages, ["1", "2"]);
  const firstPage = requested.find((url) => url.startsWith(RESOURCES_ENDPOINT));
  assert.equal(new URL(firstPage).searchParams.get("ps"), String(PAGE_SIZE));
});

test("a folder stops at the frontier the previous run confirmed", async () => {
  const { adapter, offered } = harness({
    frontierScopes: { 7: ["BV2"] },
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      pages: { "7:1": { medias: [media("BV1"), media("BV2"), media("BV3")], has_more: true } },
    }),
  });

  await adapter.run();

  // BV3 is older than the frontier, so it was already collected before.
  assert.deepEqual(
    offered.map((event) => event.resource.bvid),
    ["BV1"],
  );
});

test("stopping at the frontier counts as having reached the end", async () => {
  const { adapter } = harness({
    frontierScopes: { 7: ["BV2"] },
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      // has_more stays true: the folder has older pages, and the run
      // deliberately never asks for them.
      pages: { "7:1": { medias: [media("BV1"), media("BV2")], has_more: true } },
    }),
  });

  await adapter.run();

  // An incremental run stops early in every folder — that is the whole point.
  // Reporting that as "did not reach the end" files the run as `partial`, and
  // the Skill then tells the user a complete scan was truncated.
  const summary = adapter.summary();
  assert.equal(summary.observedEnd, true);
  assert.equal(summary.maxScanReached, false);
  assert.deepEqual(summary.frontierScopes, { 7: ["BV1"] });
});

test("the scan cap truncates the run and refuses to advance any frontier", async () => {
  const { adapter, offered } = harness({
    maxScanItems: 2,
    replies: standardReplies({
      folders: [
        { id: 7, title: "a" },
        { id: 8, title: "b" },
      ],
      pages: {
        "7:1": { medias: [media("BV1"), media("BV2"), media("BV3")], has_more: true },
        "8:1": { medias: [media("BV9")], has_more: false },
      },
    }),
  });

  await adapter.run();
  const summary = adapter.summary();

  assert.equal(offered.length, 2);
  assert.equal(summary.maxScanReached, true);
  assert.equal(summary.observedEnd, false);
  // Absent, not empty: FavHub refuses a scope that reports a cap and names a
  // frontier at once, because the next run would skip what this one missed.
  assert.deepEqual(summary.frontierScopes, {});
  assert.equal(summary.scopeResults["8"].maxScanReached, true);
});

test("a run that finished every folder reports the end and its frontiers", async () => {
  const { adapter } = harness({
    replies: standardReplies({
      folders: [
        { id: 7, title: "a" },
        { id: 8, title: "b" },
      ],
      pages: {
        "7:1": { medias: [media("BV1")], has_more: false },
        "8:1": { medias: [media("BV9")], has_more: false },
      },
    }),
  });

  await adapter.run();
  const summary = adapter.summary();

  assert.equal(summary.observedEnd, true);
  assert.equal(summary.maxScanReached, false);
  assert.deepEqual(summary.frontierScopes, { 7: ["BV1"], 8: ["BV9"] });
  // Flat frontier ids belong to unscoped platforms; a folder's progress is its
  // own, and collapsing them would advance folders that were never scanned.
  assert.deepEqual(summary.frontierIds, []);
});

// -- subtitles ----------------------------------------------------------------
//
// Shapes here are copied from a live logged-in probe, not invented: the track
// url is protocol-relative and carries an expiring auth_key, and the document
// spells its language "lang" while the track spells it "lan".

const LIVE_TRACK = {
  id: 1609595299544771000,
  lan: "ai-zh",
  lan_doc: "中文",
  is_lock: false,
  // Bilibili names the object `<aid><cid><hash>`; this one is a live name with
  // the aid and cid of the video the rest of these fixtures are about.
  subtitle_url:
    "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/11699618434521940610696130985e4d9d9cebf7" +
    "?auth_key=1785937228-x-0-y",
  type: 1,
  ai_type: 1,
  ai_status: 2,
};

const LIVE_DOCUMENT = {
  font_size: 0.4,
  lang: "ai-zh",
  version: "v1",
  body: [{ content: "♪ 音乐 ♪", from: 7.52, to: 9.04, location: 2, sid: 1 }],
};

/** Answers folders, one page, a detail carrying cid, player, and the document. */
function subtitleReplies({
  track = LIVE_TRACK,
  document = LIVE_DOCUMENT,
  player = { bvid: "BV1", cid: 40610696130 },
} = {}) {
  return (url) => {
    if (url.startsWith(NAV_ENDPOINT)) {
      return { ok: true, body: envelope({ isLogin: true, mid: 90000001 }) };
    }
    if (url.startsWith(FOLDERS_ENDPOINT)) {
      return { ok: true, body: envelope({ list: [{ id: 7, title: "f" }] }) };
    }
    if (url.startsWith(RESOURCES_ENDPOINT)) {
      return { ok: true, body: envelope({ medias: [media("BV1")], has_more: false }) };
    }
    if (url.startsWith(DETAIL_ENDPOINT)) {
      return { ok: true, body: envelope({ bvid: "BV1", title: "t", cid: 40610696130 }) };
    }
    if (url.startsWith(PLAYER_ENDPOINT)) {
      return {
        ok: true,
        // The live response echoes the video it is answering about; the
        // adapter checks that echo before trusting the track.
        body: envelope({
          bvid: player.bvid,
          aid: 117041013004944,
          cid: player.cid,
          subtitle: { subtitles: track === null ? [] : [track] },
        }),
      };
    }
    // The document is served by a CDN and is not wrapped in an envelope.
    return document === null ? { ok: false, code: "browser_unavailable" } : { ok: true, body: JSON.stringify(document) };
  };
}

test("a protocol-relative track url becomes https, never the page's scheme", () => {
  assert.equal(
    subtitleDocumentUrl("//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x?auth_key=y"),
    "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x?auth_key=y",
  );
  assert.equal(subtitleDocumentUrl(""), null);
  assert.equal(subtitleDocumentUrl(undefined), null);
});

test("the player and the subtitle cdn are both allowed, and nothing else on that host", () => {
  assert.equal(isAllowedRequest("bilibili", playerUrl("BV1", 42)), true);
  assert.equal(isAllowedRequest("bilibili", subtitleDocumentUrl(LIVE_TRACK.subtitle_url)), true);
  // An expiring auth_key lives in the query, which must not affect the decision.
  assert.equal(
    isAllowedRequest("bilibili", "https://aisubtitle.hdslb.com/bfs/ai_subtitle/p?auth_key=zzz"),
    true,
  );
  for (const url of [
    "https://aisubtitle.hdslb.com/other/path",
    "http://aisubtitle.hdslb.com/bfs/ai_subtitle/x",
    "https://evil.example.com/bfs/ai_subtitle/x",
  ]) {
    assert.equal(isAllowedRequest("bilibili", url), false, url);
  }
});

test("the subtitle cdn is fetched without credentials, and the api with them", () => {
  // Not a preference — a requirement in both directions. The cdn answers
  // `Access-Control-Allow-Origin: *`, which the Fetch standard refuses to
  // accept for a credentialled request, so sending cookies would make every
  // subtitle fail CORS. Silently, too: a failed download reads as "no
  // subtitle". And the cdn has no business receiving Bilibili's cookies when
  // the url it hands out already carries its own auth_key.
  assert.equal(sendsCredentials("bilibili", resourcesUrl("7", 1)), true);
  assert.equal(sendsCredentials("bilibili", detailUrl("BV1")), true);
  assert.equal(sendsCredentials("bilibili", playerUrl("BV1", 42)), true);
  assert.equal(sendsCredentials("bilibili", subtitleDocumentUrl(LIVE_TRACK.subtitle_url)), false);
  assert.equal(sendsCredentials("bilibili", "https://aisubtitle.hdslb.com/bfs/x"), false);
  // A host that merely ends in the platform's name is not the platform.
  assert.equal(sendsCredentials("bilibili", "https://api.bilibili.com.evil.example.com/x"), false);
  assert.equal(sendsCredentials("bilibili", "not a url"), false);
});

test("a human track is preferred over an ai one, and a locked track is skipped", () => {
  const human = { ...LIVE_TRACK, lan: "zh-CN", ai_type: 0, subtitle_url: "//aisubtitle.hdslb.com/bfs/h" };
  assert.equal(readSubtitleTrack({ subtitle: { subtitles: [LIVE_TRACK, human] } }).lan, "zh-CN");
  assert.equal(readSubtitleTrack({ subtitle: { subtitles: [LIVE_TRACK] } }).lan, "ai-zh");
  assert.equal(
    readSubtitleTrack({ subtitle: { subtitles: [{ ...LIVE_TRACK, is_lock: true }] } }),
    null,
  );
  assert.equal(readSubtitleTrack({ subtitle: { subtitles: [] } }), null);
  assert.equal(readSubtitleTrack({}), null);
});

test("a video with a subtitle carries both the parsed document and its raw text", async () => {
  const { adapter, offered, requested } = harness({ replies: subtitleReplies() });

  await adapter.run();

  assert.equal(offered.length, 1);
  // Python's parse_subtitle reads `lang` off the document itself, so the
  // document travels unmodified rather than being rewritten here.
  assert.deepEqual(offered[0].subtitle, LIVE_DOCUMENT);
  assert.equal(offered[0].subtitleRaw, JSON.stringify(LIVE_DOCUMENT));
  assert.ok(requested.some((url) => url.startsWith(PLAYER_ENDPOINT)));
});

test("a video with no subtitle track costs no document request and still collects", async () => {
  const { adapter, offered, requested, paused } = harness({
    replies: subtitleReplies({ track: null }),
  });

  await adapter.run();

  assert.equal(offered.length, 1);
  assert.equal(offered[0].subtitle, null);
  assert.equal(offered[0].subtitleRaw, null);
  assert.deepEqual(paused, []);
  assert.ok(!requested.some((url) => url.startsWith("https://aisubtitle.hdslb.com")));
});

test("a player answering about another video costs the subtitle, not the run", async () => {
  // Measured, not hypothetical: nine of the thirteen videos this adapter first
  // collected with a transcript got one belonging to a different video, none of
  // which this library had ever asked about. The request was verifiably right —
  // the detail for the case checked live carried exactly the title, duration
  // and cid that were sent — so the check has to be on the answer.
  const { adapter, offered, paused } = harness({
    replies: subtitleReplies({ player: { bvid: "BV1", cid: 99999999 } }),
  });

  await adapter.run();

  assert.equal(offered.length, 1);
  assert.equal(offered[0].subtitle, null);
  assert.equal(offered[0].subtitleRaw, null);
  // The video itself still collects; only the transcript is refused.
  assert.equal(offered[0].resource.bvid, "BV1");
  assert.deepEqual(paused, []);
});

test("a track pointing at another video's transcript costs the subtitle, not the run", async () => {
  // This is the bug the echo check above did not catch, and it is what actually
  // happened: the player answered about the right video and then named an
  // object belonging to a different one. Verified against the live api — the
  // object served for a video whose transcript was wrong carried aid 113226…
  // where the video's own is 116879…, while two correct captures named their
  // own aid and cid exactly.
  const stranger = {
    ...LIVE_TRACK,
    subtitle_url: "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/11322627673704526385123092abc?auth_key=z",
  };
  const { adapter, offered, paused, requested } = harness({
    replies: subtitleReplies({ track: stranger }),
  });

  await adapter.run();

  assert.equal(offered.length, 1);
  assert.equal(offered[0].subtitle, null);
  assert.equal(offered[0].subtitleSource, null);
  // Refused before it is downloaded: the wrong words never enter the library.
  assert.ok(!requested.some((url) => url.startsWith("https://aisubtitle.hdslb.com")));
  // And the refusal is reported, so it does not store as "this video has no
  // transcript" — a different and much less interesting fact.
  assert.equal(
    offered[0].subtitleMismatch,
    "/bfs/ai_subtitle/prod/11322627673704526385123092abc",
  );
  assert.deepEqual(paused, []);
});

test("an object naming this video's own cid is what gets kept", () => {
  assert.equal(subtitleObjectBelongsTo("/bfs/ai_subtitle/prod/11699618434521940368868964h", 40368868964), true);
  assert.equal(subtitleObjectBelongsTo("/bfs/ai_subtitle/prod/11322627673704526385123092h", 40368868964), false);
  assert.equal(subtitleObjectBelongsTo("", 40368868964), false);
  assert.equal(subtitleObjectBelongsTo(null, 40368868964), false);
});

test("a player response that names no video at all is still trusted", () => {
  // Absence is not contradiction. Refusing here would turn any reshaping of the
  // response into a silent library-wide loss of transcripts.
  assert.equal(playerAnswersAbout({ subtitle: { subtitles: [] } }, "BV1", 42), true);
  assert.equal(playerAnswersAbout({ bvid: "BV1", cid: 42 }, "BV1", 42), true);
  assert.equal(playerAnswersAbout({ bvid: "BV2", cid: 42 }, "BV1", 42), false);
  assert.equal(playerAnswersAbout({ bvid: "BV1", cid: 43 }, "BV1", 42), false);
  assert.equal(playerAnswersAbout(null, "BV1", 42), false);
});

test("a subtitle that will not download costs its own text, not the run", async () => {
  // The auth_key expires, so a download can fail on a video that is otherwise
  // perfectly collectable.
  const { adapter, offered, paused } = harness({ replies: subtitleReplies({ document: null }) });

  await adapter.run();

  assert.equal(offered.length, 1);
  assert.equal(offered[0].subtitle, null);
  assert.deepEqual(paused, []);
});

test("a subtitle document that is not a cue list is refused rather than stored", async () => {
  for (const document of [{ body: "not a list" }, { no: "body" }]) {
    const { adapter, offered } = harness({ replies: subtitleReplies({ document }) });
    await adapter.run();
    assert.equal(offered[0].subtitle, null, JSON.stringify(document));
  }
});

test("no player request is made for a video whose detail carried no cid", async () => {
  // Detail is best-effort; without a cid there is nothing to ask the player.
  const { adapter, offered, requested } = harness({
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      pages: { "7:1": { medias: [media("BV1")], has_more: false } },
    }),
  });

  await adapter.run();

  assert.equal(offered.length, 1);
  assert.equal(offered[0].subtitle, null);
  assert.ok(!requested.some((url) => url.startsWith(PLAYER_ENDPOINT)));
});

// -- throttling and failure ---------------------------------------------------

test("requests are spaced, so a run stays a guest on someone else's service", async () => {
  const { adapter, waits } = harness({
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      pages: {
        "7:1": { medias: [media("BV1")], has_more: true },
        "7:2": { medias: [media("BV2")], has_more: false },
      },
    }),
  });

  await adapter.run();

  assert.ok(waits.length >= 4, `expected pauses between requests, saw ${waits.length}`);
  assert.ok(waits.every((ms) => ms >= REQUEST_INTERVAL_MS));
});

test("a missing video detail does not stop the folder", async () => {
  // A video that was taken down must cost its own metadata, not the whole run.
  const { adapter, offered, paused } = harness({
    replies: (url) => {
      if (url.startsWith(NAV_ENDPOINT)) {
        return { ok: true, body: envelope({ isLogin: true, mid: 90000001 }) };
      }
      if (url.startsWith(FOLDERS_ENDPOINT)) {
        return { ok: true, body: envelope({ list: [{ id: 7, title: "f" }] }) };
      }
      if (url.startsWith(RESOURCES_ENDPOINT)) {
        return {
          ok: true,
          body: envelope({ medias: [media("BV1"), media("BV2")], has_more: false }),
        };
      }
      return { ok: true, body: JSON.stringify({ code: -404, data: null }) };
    },
  });

  await adapter.run();

  assert.equal(offered.length, 2);
  assert.deepEqual(paused, []);
  assert.equal(offered[0].detail, null);
});

test("a logged-out session pauses with its stable code and collects nothing", async () => {
  const { adapter, offered, paused } = harness({
    replies: () => ({ ok: true, body: "<!doctype html>" }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "login_required");
  assert.deepEqual(offered, []);
});

test("an unreachable page pauses rather than reporting an empty library", async () => {
  const { adapter, paused } = harness({
    replies: () => ({ ok: false, code: "browser_unavailable" }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "browser_unavailable");
});

// -- what the adapter must never do -------------------------------------------

/** Strip comments so the scan judges code, not the prose explaining it. */
function codeOnly(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const SOURCE = codeOnly(
  readFileSync(
    new URL("../../src/favhub/browser_extension/adapters/bilibili.js", import.meta.url),
    "utf8",
  ),
);

test("the adapter never scrolls, clicks, or injects page-world code", () => {
  // Active mode has no reason to touch the page at all; §5.2 requires that
  // pagination be requested, not driven through the interface.
  for (const forbidden of ["scrollTo", "scrollIntoView", "click(", "createElement", "innerHTML"]) {
    assert.ok(!SOURCE.includes(forbidden), `bilibili.js must not use ${forbidden}`);
  }
});

test("the adapter never names a header or a credential", () => {
  const lowered = SOURCE.toLowerCase();
  for (const forbidden of ["headers", "sessdata", "document.cookie", "authorization", "bili_jct"]) {
    assert.ok(!lowered.includes(forbidden), `bilibili.js must not mention ${forbidden}`);
  }
});

test("the adapter issues only GETs, and never a write endpoint", () => {
  // A favourites API has delete and move operations on neighbouring paths; a
  // collector that could reach them would be able to destroy the library it
  // exists to mirror.
  for (const forbidden of ["method: \"POST\"", "fav/resource/deal", "fav/resource/batch"]) {
    assert.ok(!SOURCE.includes(forbidden), `bilibili.js must not use ${forbidden}`);
  }
});


// -- identifying the account --------------------------------------------------

test("a run asks who it is before asking for anything else", async () => {
  // This is what lets FavHub open the page itself: without it the route had to
  // carry the account id, and only the user knew that url.
  const { adapter, requested } = harness({
    replies: standardReplies({
      folders: [{ id: 7, title: "f" }],
      pages: { "7:1": { medias: [media("BV1")], has_more: false } },
    }),
  });

  await adapter.run();

  assert.ok(requested[0].startsWith(NAV_ENDPOINT), `first request was ${requested[0]}`);
  assert.equal(new URL(requested[1]).searchParams.get("up_mid"), "90000001");
});

test("a logged-out account stops before listing anything", async () => {
  const { adapter, requested, paused } = harness({
    replies: () => ({ ok: true, body: JSON.stringify({ code: -101, data: null }) }),
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "login_required");
  assert.ok(!requested.some((url) => url.startsWith(FOLDERS_ENDPOINT)));
});

test("an identity response without a mid pauses rather than collecting nobody", async () => {
  const { adapter, paused } = harness({
    replies: (url) =>
      url.startsWith(NAV_ENDPOINT)
        ? { ok: true, body: envelope({ isLogin: false }) }
        : { ok: true, body: envelope({ list: [] }) },
  });

  const result = await adapter.run();

  assert.equal(result.ok, false);
  assert.equal(paused[0].code, "login_required");
});
