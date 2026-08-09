// One-time development helper: mint the extension's fixed identity.
//
// Chrome derives an unpacked extension's id from the manifest `key`, so pinning
// one public key is what lets the Native Messaging host allowlist a single id
// instead of trusting whatever connects. Only the public key is written; the
// private half is discarded immediately and never leaves this process.
import { generateKeyPairSync, createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const manifestPath = new URL("../src/favhub/browser_extension/manifest.json", import.meta.url);
const idPath = new URL("../src/favhub/browser_extension/EXTENSION_ID", import.meta.url);
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));

if (manifest.key) {
  console.log("manifest already has a key; refusing to change the extension id");
  process.exit(0);
}

const { publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const der = publicKey.export({ type: "spki", format: "der" });
manifest.key = der.toString("base64");

const digest = createHash("sha256").update(der).digest("hex").slice(0, 32);
const extensionId = [...digest].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join("");

writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
writeFileSync(idPath, `${extensionId}\n`, "utf8");
console.log(`extension id: ${extensionId}`);
