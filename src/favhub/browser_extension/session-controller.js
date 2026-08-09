// Session lifecycle shared by every platform adapter.
//
// The controller owns the rule that makes collection safe to interrupt: a batch
// is only forgotten once FavHub returns a durable receipt for it. Anything not
// yet acknowledged is retried, and nothing is ever written to extension storage
// — a browser that dies mid-run leaves no copy of the user's saved content
// behind, it just rescans.
//
// Adapters supply platform behaviour; this file never parses a response.

export const MAX_PENDING_ITEMS = 20;
export const HEARTBEAT_MS = 15_000;

/** How many bytes of buffered events may travel in one batch.
 *
 * Under the relay's own 4 MiB frame limit, with room for the envelope the
 * batch is wrapped in. A single event larger than this is still sent alone —
 * a platform page cannot be split — and the relay reports it rather than
 * dying, which is the other half of this fix.
 */
export const MAX_BATCH_BYTES = 3 * 1024 * 1024;

/** The size this event will actually be on the wire, not its character count.
 *
 * Article text is mostly CJK, where one character is three UTF-8 bytes, so a
 * length-based estimate understates a Zhihu batch by roughly threefold —
 * exactly the margin that decides whether the frame fits.
 */
function measureBytes(event) {
  try {
    return new TextEncoder().encode(JSON.stringify(event)).length;
  } catch {
    // An unserialisable event cannot be sent at all; letting it count as huge
    // keeps it from silently joining a batch it would break.
    return MAX_BATCH_BYTES;
  }
}

export const SessionState = Object.freeze({
  INACTIVE: "inactive",
  AWAITING: "awaiting_browser",
  CAPTURING: "capturing",
  PAUSED: "paused",
  COMPLETED: "completed",
  CANCELLED: "cancelled",
});

export class SessionController {
  /**
   * @param {object} options
   * @param {import("./native-client.js").NativeClient} options.client
   * @param {(state: object) => void} [options.onChange]
   */
  constructor({ client, onChange = () => {} }) {
    this.client = client;
    this.onChange = onChange;
    this.state = SessionState.INACTIVE;
    this.platform = null;
    this.sessionId = null;
    this.jobId = null;
    this.error = null;
    // Where to stop, learned from the claim rather than asked for per page.
    this.frontier = [];
    this.maxScanItems = null;
    this.scopes = [];
    this.tabId = null;
    this.pending = [];
    // Sizes kept in lockstep with `pending`, so a flush never has to
    // re-serialise megabytes of buffered text just to know what it dropped.
    this.pendingSizes = [];
    this.pendingBytes = 0;
    this.counts = { scanned: 0, submitted: 0 };
    this.heartbeatTimer = null;
    this.client.onDisconnect = () => this.#onDisconnect();
  }

  get snapshot() {
    return {
      state: this.state,
      platform: this.platform,
      jobId: this.jobId,
      error: this.error,
      counts: { ...this.counts },
      pending: this.pending.length,
    };
  }

  /**
   * Ask FavHub whether this platform has work waiting.
   * Returns false when there is nothing to do, which is the common case: the
   * extension stays dormant unless the user started a run from their Agent.
   */
  async claim(platform, extensionVersion) {
    const result = await this.client.request("session.claim", { platform, extensionVersion });
    if (!result || !result.session) {
      this.#set({
        state: SessionState.INACTIVE,
        platform: null,
        sessionId: null,
        jobId: null,
        // A refusal and an empty answer arrive the same way, and only one of
        // them is something the user can act on. Carrying the reason is what
        // turns "nothing happened" into "click Reload".
        error: result && result.error ? result.error : null,
      });
      return false;
    }
    this.#set({
      state: SessionState.CAPTURING,
      platform,
      sessionId: result.session.session_id,
      jobId: result.session.job_id,
      frontier: Array.isArray(result.frontier) ? result.frontier : [],
      maxScanItems: typeof result.maxScanItems === "number" ? result.maxScanItems : null,
      scopes: Array.isArray(result.scopes) ? result.scopes : [],
      error: null,
    });
    this.#startHeartbeat();
    return true;
  }

  /** Register the folders the browser found, and learn each one's frontier.
   *
   * Scoped platforms cannot be asked for a frontier up front: which folders
   * exist is something only the browser can see. Declaring them before the
   * first bundle is what lets an incremental run stop per folder rather than
   * rescanning every one of them to the end.
   */
  async declareScopes(scopes) {
    this.#requireActive();
    const result = await this.client.request("scope.declare", {
      sessionId: this.sessionId,
      platform: this.platform,
      scopes,
    });
    const frontiers = result && result.frontiers ? result.frontiers : {};
    this.scopes = scopes;
    return frontiers;
  }

  /** Buffer one mapped observation, flushing whenever a full batch exists. */
  async offer(event) {
    this.#requireActive();
    const bytes = measureBytes(event);
    // Flush what is already buffered before this event joins it. A batch was
    // bounded only by how many events it held, while the relay that carries it
    // is bounded by how many bytes one frame may be — and nothing reconciled
    // the two. Twenty Zhihu pages are twenty pages of article text, which
    // crosses the frame limit; the relay then refused the frame and exited,
    // taking the extension's only channel with it. No pause, no code, just a
    // session frozen until its lease ran out.
    if (this.pending.length > 0 && this.pendingBytes + bytes > MAX_BATCH_BYTES) {
      await this.flush();
    }
    this.pending.push(event);
    this.pendingSizes.push(bytes);
    this.pendingBytes += bytes;
    this.counts.scanned += 1;
    if (this.pending.length >= MAX_PENDING_ITEMS) {
      await this.flush();
    }
    this.#notify();
  }

  /** Submit the buffered batch and only then drop it. */
  async flush() {
    this.#requireActive();
    if (this.pending.length === 0) return null;
    const batch = this.pending.slice();
    const receipt = await this.client.request("capture.bundle", {
      sessionId: this.sessionId,
      platform: this.platform,
      events: batch,
    });
    if (receipt && receipt.accepted === false) {
      // FavHub refused the batch (a platform condition it turned into a pause);
      // keep the items buffered so nothing is silently dropped.
      throw new Error(`batch refused: ${receipt.error?.code ?? "unknown"}`);
    }
    // Dropping before the receipt would silently lose whatever FavHub did not
    // persist, so the splice happens strictly after the round trip resolves.
    this.pending.splice(0, batch.length);
    const sent = this.pendingSizes.splice(0, batch.length);
    this.pendingBytes -= sent.reduce((total, size) => total + size, 0);
    this.counts.submitted += batch.length;
    this.#notify();
    return receipt;
  }

  async pause(code, message) {
    if (this.state !== SessionState.CAPTURING) return;
    this.#stopHeartbeat();
    try {
      await this.client.request("session.pause", {
        jobId: this.jobId,
        platform: this.platform,
        code,
        message,
      });
    } finally {
      this.#set({ state: SessionState.PAUSED, error: { code, message } });
    }
  }

  async finish(summary) {
    this.#requireActive();
    await this.flush();
    this.#stopHeartbeat();
    await this.client.request("session.finish", {
      sessionId: this.sessionId,
      jobId: this.jobId,
      platform: this.platform,
      ...summary,
    });
    this.#set({ state: SessionState.COMPLETED, error: null });
  }

  async cancel() {
    if (this.state === SessionState.INACTIVE) return;
    this.#stopHeartbeat();
    try {
      await this.client.request("session.cancel", {
        jobId: this.jobId,
        platform: this.platform,
      });
    } finally {
      // Whatever was buffered was never acknowledged, so it is dropped rather
      // than kept: the next run rescans it.
      this.pending = [];
      this.pendingSizes = [];
      this.pendingBytes = 0;
      this.#set({ state: SessionState.CANCELLED, error: null });
    }
  }

  #requireActive() {
    if (this.state !== SessionState.CAPTURING) {
      throw new Error(`session is not capturing: ${this.state}`);
    }
  }

  #startHeartbeat() {
    this.#stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.client
        .request("session.heartbeat", { sessionId: this.sessionId })
        .catch(() => this.#onDisconnect());
    }, HEARTBEAT_MS);
    // No-op in a service worker, but under `node --test` a live interval keeps
    // the process alive after the assertions finish and hangs the suite.
    this.heartbeatTimer?.unref?.();
  }

  #stopHeartbeat() {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  #onDisconnect() {
    this.#stopHeartbeat();
    if (this.state === SessionState.CAPTURING) {
      // FavHub's lease will expire and pause the session on its side too; this
      // just makes the popup honest immediately.
      this.#set({
        state: SessionState.PAUSED,
        error: { code: "mcp_unavailable", message: "FavHub is no longer reachable." },
      });
    }
  }

  #set(patch) {
    Object.assign(this, patch);
    this.#notify();
  }

  #notify() {
    this.onChange(this.snapshot);
  }
}
