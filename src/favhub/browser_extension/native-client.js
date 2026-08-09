// A closed request/response client over Chrome's Native Messaging port.
//
// Native Messaging is a stream of independent messages, so replies are matched
// back to their requests by id. Every pending request carries its own timeout:
// a relay that dies mid-request would otherwise leave the session waiting
// forever with nothing to show the user.
//
// Keeping this port open is also what keeps the MV3 service worker alive during
// a long collection run, which is why the controller connects once and holds it
// rather than reconnecting per message.

export const DEFAULT_TIMEOUT_MS = 30_000;
export const NATIVE_HOST = "com.favhub.browser";

export class NativeClientError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

export class NativeClient {
  /**
   * @param {object} options
   * @param {() => object} options.connect returns a chrome.runtime.Port
   * @param {number} [options.protocolVersion]
   * @param {number} [options.timeoutMs]
   * @param {() => number} [options.now]
   */
  constructor({ connect, protocolVersion = 1, timeoutMs = DEFAULT_TIMEOUT_MS, now = Date.now }) {
    this.connect = connect;
    this.protocolVersion = protocolVersion;
    this.timeoutMs = timeoutMs;
    this.now = now;
    this.port = null;
    this.pending = new Map();
    this.sequence = 0;
    this.onDisconnect = null;
  }

  open() {
    if (this.port) return this.port;
    const port = this.connect();
    port.onMessage.addListener((message) => this.#receive(message));
    port.onDisconnect.addListener(() => this.#disconnected());
    this.port = port;
    return port;
  }

  close() {
    const port = this.port;
    this.port = null;
    if (port && typeof port.disconnect === "function") port.disconnect();
    this.#rejectAll("mcp_unavailable", "The FavHub connection was closed.");
  }

  get isOpen() {
    return this.port !== null;
  }

  /**
   * Send one request and resolve with its reply payload.
   * @param {string} type
   * @param {object} payload
   */
  request(type, payload) {
    this.open();
    this.sequence += 1;
    const requestId = `r-${String(this.sequence).padStart(6, "0")}`;
    const message = {
      protocolVersion: this.protocolVersion,
      requestId,
      type,
      payload,
    };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new NativeClientError("mcp_unavailable", "FavHub did not answer in time."));
      }, this.timeoutMs);
      this.pending.set(requestId, { resolve, reject, timer });
      try {
        this.port.postMessage(message);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(requestId);
        reject(new NativeClientError("mcp_unavailable", String(error)));
      }
    });
  }

  #receive(message) {
    if (!message || typeof message !== "object") return;
    const entry = message.requestId ? this.pending.get(message.requestId) : undefined;
    if (!entry) {
      // An unmatched reply is either a duplicate or a relay that lost track;
      // dropping it is safer than guessing which request it belongs to.
      return;
    }
    this.pending.delete(message.requestId);
    clearTimeout(entry.timer);
    if (message.error) {
      entry.reject(new NativeClientError(message.error.code, message.error.message));
      return;
    }
    entry.resolve(message.result ?? {});
  }

  #disconnected() {
    this.port = null;
    this.#rejectAll("mcp_unavailable", "FavHub is not running for this data root.");
    if (this.onDisconnect) this.onDisconnect();
  }

  #rejectAll(code, message) {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(new NativeClientError(code, message));
    }
    this.pending.clear();
  }
}
