import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../mobile_panel.js", import.meta.url), "utf8");
const panel = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("turn tail retries until observe writer exposes exact usage", async () => {
  globalThis.window = { setTimeout };
  globalThis.document = {
    createElement() {
      return {
        className: "",
        innerHTML: "",
        setAttribute() {},
      };
    },
  };
  let calls = 0;
  const host = {
    className: "",
    textContent: "",
    child: null,
    classList: { add() {} },
    replaceChildren(child) { this.child = child; },
  };
  const cleanup = panel.default.slots["turn.after_answer"].mount(host, {
    messageId: "mobile:demo:2",
    sessionId: "mobile:demo",
    async request() {
      calls += 1;
      return calls < 3 ? { usage: null } : { usage: { output_tokens: 321 } };
    },
  });

  await new Promise((resolve) => setTimeout(resolve, 500));

  assert.equal(calls, 3);
  assert.match(host.child.innerHTML, /本轮输出/);
  assert.match(host.child.innerHTML, /321/);
  cleanup();
});
