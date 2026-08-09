// Content script entry point. Deliberately NOT an ES module.
//
// MV3 has no `type: "module"` for manifest-declared content scripts, so a
// top-level `export` or `import` here is a syntax error and Chrome silently
// never runs the file — the extension then looks "loaded" while doing nothing.
// bridge.js is declared ahead of this file in the manifest and shares this
// isolated world, so its namespace is simply already here; see the note there
// for why it is not fetched with a dynamic import.

(() => {
  if (typeof chrome === "undefined" || !chrome.runtime) return;
  const namespace = globalThis.FavHubBridge;
  if (!namespace) return;

  const bridge = namespace.createBridge({
    location: window.location,
    postToPage: (message, origin) => window.postMessage(message, origin),
    sendToWorker: (payload) => chrome.runtime.sendMessage(payload),
  });
  if (bridge.platform === null) return;

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    bridge.handlePageMessage(event.data, event.origin);
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message !== "object") return;
    if (message.kind === "fetch") {
      // Active-mode pagination runs here, in the page's own context, so the
      // platform's cookies travel with the request and nothing has to read
      // them. The allowlist is enforced on this side: being asked is not
      // permission, or the worker could use the page as a fetch proxy.
      if (!namespace.isAllowedRequest(bridge.platform, message.url)) {
        sendResponse({ ok: false, code: "invalid_message" });
        return true;
      }
      fetch(message.url, {
        // Only the platform's own sites get the session; see sendsCredentials.
        credentials: namespace.sendsCredentials(bridge.platform, message.url) ? "include" : "omit",
        // No constructed headers, and no method but GET: this reads the user's
        // own saved items and must never be able to change account state.
        method: "GET",
        redirect: "follow",
      })
        .then((response) => response.text().then((body) => ({ status: response.status, body })))
        .then(({ status, body }) => sendResponse({ ok: status === 200, status, body }))
        .catch((error) =>
          sendResponse({ ok: false, code: "browser_unavailable", detail: String(error) }),
        );
      return true;
    }
    if (message.kind === "activate") bridge.activate(message.patterns ?? []);
    if (message.kind === "deactivate") bridge.deactivate();
    if (message.kind === "scroll") {
      // The adapter asks for the next page only after the previous one was
      // acknowledged, so this never outruns what FavHub has accepted.
      namespace.scrollToEnd(window);
    }
  });

  function announceOnce() {
    return chrome.runtime
      .sendMessage({
        kind: "page.ready",
        platform: bridge.platform,
        url: window.location.href,
        // A tab FavHub opened is disposable; one the user opened is not, and only
        // this marker tells them apart. It is a fragment on purpose: fragments are
        // never sent to the server, so the platform cannot see it.
        favhubOpened: window.location.hash === "#favhub-opened",
      })
      .then((reply) => {
        if (!reply || !reply.claimed || !bridge.passive) return;
        const script = document.createElement("script");
        script.src = chrome.runtime.getURL("main-hook.js");
        script.onload = () => {
          script.remove();
          // Activation must follow the load: the hook registers its listener when
          // it runs, so arming it earlier would post into a page that is not
          // listening yet and the first response page would be missed.
          bridge.activate(reply.patterns ?? []);
        };
        (document.head || document.documentElement).append(script);
      })
      .catch(() => {
        // No FavHub session, or an extension mid-reload: staying silent is
        // correct, and the popup reports the real state.
      });
  }

  const announcer = namespace.createAnnouncer({ send: announceOnce });
  announcer.announce(true);

  // A run is started from a chat window, not from this page, so by the time
  // FavHub is waiting the tab has usually been sitting in the background for a
  // while. Announcing again when it comes back to the front is what removes the
  // manual reload: the worker is woken by this very message, claims the waiting
  // session, and the run begins. Both events are watched because they do not
  // coincide — switching tabs fires visibilitychange, switching windows fires
  // focus — and the announcer's own interval is what keeps that from mattering.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") announcer.announce();
  });
  window.addEventListener("focus", () => announcer.announce());
})();
