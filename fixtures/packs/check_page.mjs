// Bun executes browser exports against a tiny document/bridge fixture, not a browser or WebSocket.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const [pagePath, python, handlerPath] = process.argv.slice(2);
const page = await import(pathToFileURL(pagePath).href);
const document = { documentElement: { dataset: {} } };
const bridge = {
  async request(request) {
    assert.deepEqual(request, { op: "echo", text: "hello" });
    const result = spawnSync(python, [handlerPath], {
      input: JSON.stringify(request) + "\n", encoding: "utf8", timeout: 5000,
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.stderr.trim().split("\n").map(line => JSON.parse(line).event), ["start", "stop"]);
    return JSON.parse(result.stdout);
  },
};
await page.start({ bridge, document });
const started = document.documentElement.dataset.tapFixture;
assert.equal(started, "local:hello");
page.stop({ document });
page.stop({ document });
assert.deepEqual(document.documentElement.dataset, {});
await assert.rejects(() => page.start({ document, bridge: { request: async () => ({ ok: false }) } }), /bad local reply/);
assert.deepEqual(document.documentElement.dataset, {});
console.log(JSON.stringify({ started, stopped: true, bad_reply_rejected: true }));
