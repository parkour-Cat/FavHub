// Page-world response hook. Passive-mode platforms only (X today).
//
// This file runs in the page's own JavaScript world, which is the only place a
// header-authenticated request's response can be observed without reading the
// credentials that authenticated it. It is injected per session and never from
// the manifest: Bilibili and Zhihu paginate themselves from the isolated world,
// so no page-world code is loaded on those origins at all.
//
// It starts dormant. Until the isolated bridge confirms an active session for
// this platform, matching responses are not copied, not stored, and not sent.
// Requests are never constructed here, and request headers are never read.

(() => {
  const CHANNEL = "favhub:page";
  const state = { active: false, patterns: [] };

  function post(kind, detail) {
    window.postMessage({ channel: CHANNEL, kind, detail }, window.location.origin);
  }

  function matches(url) {
    if (!state.active) return false;
    return state.patterns.some((pattern) => url.includes(pattern));
  }

  window.addEventListener("message", (event) => {
    // Only same-origin messages from this page; the isolated bridge is the sole
    // legitimate sender and a site could otherwise activate the hook itself.
    if (event.source !== window || event.origin !== window.location.origin) return;
    const data = event.data;
    if (!data || data.channel !== "favhub:control") return;
    if (data.kind === "activate" && Array.isArray(data.patterns)) {
      state.active = true;
      state.patterns = data.patterns.filter((p) => typeof p === "string").slice(0, 20);
    } else if (data.kind === "deactivate") {
      state.active = false;
      state.patterns = [];
    }
  });

  const originalFetch = window.fetch;
  window.fetch = async function favhubFetch(...args) {
    const response = await originalFetch.apply(this, args);
    try {
      const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
      if (matches(url)) {
        // Cloning leaves the page's own copy untouched; a consumed body would
        // break the site the user is looking at.
        response
          .clone()
          .text()
          .then((body) => post("response", { url, body }))
          .catch(() => {});
      }
    } catch {
      // Observation must never change page behaviour.
    }
    return response;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function favhubOpen(method, url, ...rest) {
    this.__favhubUrl = typeof url === "string" ? url : "";
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function favhubSend(...args) {
    this.addEventListener("load", () => {
      try {
        if (matches(this.__favhubUrl || "")) {
          post("response", { url: this.__favhubUrl, body: this.responseText });
        }
      } catch {
        // Same rule: never break the page.
      }
    });
    return originalSend.apply(this, args);
  };
})();
