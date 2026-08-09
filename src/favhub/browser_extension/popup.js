// Popup: show what the session is doing, and offer only the actions that are
// valid right now. Offering "cancel" on a finished run would invite a click
// that either does nothing or, worse, looks like it undid something.

export const ACTIONS_BY_STATE = Object.freeze({
  inactive: { pause: false, cancel: false },
  awaiting_browser: { pause: false, cancel: true },
  capturing: { pause: true, cancel: true },
  paused: { pause: false, cancel: true },
  completed: { pause: false, cancel: false },
  cancelled: { pause: false, cancel: false },
});

export function actionsFor(state) {
  return ACTIONS_BY_STATE[state] ?? ACTIONS_BY_STATE.inactive;
}

// Codes that mean FavHub itself is unreachable rather than the platform being
// unhappy. Only these earn a repair hint: sending someone to `favhub doctor`
// because they hit a rate limit points them at an install that is fine.
const INSTALL_FAULTS = new Set(["mcp_unavailable", "browser_unavailable"]);

export const REPAIR_HINT =
  "FavHub is not reachable. Run `favhub doctor` in a terminal to find out which " +
  "part of the install is broken.";

export function render(document, snapshot) {
  const state = snapshot?.state ?? "inactive";
  document.getElementById("state").textContent = state.replace(/_/g, " ");
  document.getElementById("platform").textContent = snapshot?.platform ?? "—";
  document.getElementById("scanned").textContent = String(snapshot?.counts?.scanned ?? 0);
  document.getElementById("submitted").textContent = String(snapshot?.counts?.submitted ?? 0);
  document.getElementById("pending").textContent = String(snapshot?.pending ?? 0);

  const errorNode = document.getElementById("error");
  const hintNode = document.getElementById("hint");
  if (snapshot?.error) {
    errorNode.hidden = false;
    errorNode.textContent = `${snapshot.error.code}: ${snapshot.error.message}`;
  } else {
    errorNode.hidden = true;
    errorNode.textContent = "";
  }
  // The hint is the whole point of showing an error here: a code alone leaves a
  // user with nowhere to go.
  const broken = snapshot?.error && INSTALL_FAULTS.has(snapshot.error.code);
  hintNode.hidden = !broken;
  hintNode.textContent = broken ? REPAIR_HINT : "";

  const allowed = actionsFor(state);
  document.getElementById("pause").disabled = !allowed.pause;
  document.getElementById("cancel").disabled = !allowed.cancel;
}

if (typeof chrome !== "undefined" && chrome.runtime && typeof document !== "undefined") {
  const ask = (kind) => chrome.runtime.sendMessage({ kind });
  ask("popup.status").then((snapshot) => render(document, snapshot));
  document
    .getElementById("pause")
    .addEventListener("click", () => ask("popup.pause").then((s) => render(document, s)));
  document
    .getElementById("cancel")
    .addEventListener("click", () => ask("popup.cancel").then((s) => render(document, s)));
}
