import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../mobile_panel.js", import.meta.url), "utf8");
const styles = await readFile(new URL("../mobile_panel.css", import.meta.url), "utf8");
const panel = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

function contrastRatio(first, second) {
  const luminance = (hex) => {
    const channels = hex.match(/[a-f\d]{2}/gi).map((value) => Number.parseInt(value, 16) / 255);
    const linear = channels.map((value) => value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  };
  const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

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
  assert.match(host.child.innerHTML, /输出/);
  assert.match(host.child.innerHTML, /321 tokens/);
  assert.doesNotMatch(host.child.innerHTML, /observe-kv-tail__marker/);
  cleanup();
});

test("dashboard only colors cache lanes with real data", async () => {
  const states = new Map();
  const section = (name) => {
    const state = {
      empty: null,
      strong: { textContent: "" },
      span: { textContent: "" },
    };
    states.set(name, state);
    return {
      classList: { toggle(_className, enabled) { state.empty = enabled; } },
      querySelector(selector) { return selector === "strong" ? state.strong : state.span; },
    };
  };
  const elements = {
    ".observe-kv-loading": { remove() {} },
    ".observe-kv-content": { hidden: true },
    ".observe-kv-ring": {
      style: { setProperty() {} },
      querySelector() { return { textContent: "" }; },
    },
    ".observe-kv-passive": section("passive"),
    ".observe-kv-proactive": section("proactive"),
    ".observe-kv-list header span": { textContent: "" },
    ".observe-kv-turns": { append() {} },
  };
  globalThis.document = {
    createElement() { return { className: "", textContent: "" }; },
  };
  const host = {
    className: "",
    innerHTML: "",
    querySelector(selector) { return elements[selector]; },
    querySelectorAll() { return []; },
  };
  const cleanup = panel.default.dashboard.mount(host, {
    async request(method) {
      if (method === "kvcache.overview") {
        return {
          passive: { tracked_turn_count: 1, hit_rate: 0.8 },
          proactive: { tracked_turn_count: 0, hit_rate: null },
        };
      }
      return { items: [], total: 0 };
    },
  });

  await Promise.resolve();
  await Promise.resolve();

  assert.equal(states.get("passive").empty, false);
  assert.equal(states.get("proactive").empty, true);
  assert.equal(states.get("proactive").strong.textContent, "暂无记录");
  cleanup();
});

test("health status uses color only for an actionable state", () => {
  assert.deepEqual(panel.healthState({ total: 0, types: 0 }), {
    tone: "steady",
    title: "运行平稳",
    description: "最近 24 小时没有收集到需要处理的错误。",
  });
  assert.deepEqual(panel.healthState({ total: 18, types: 3, new_types: 1, spiking_types: 2 }), {
    tone: "urgent",
    title: "2 类错误正在增加",
    description: "共 18 次，先查看爆发项。",
  });
  assert.equal(panel.default.navigation.label, "Observe");
  assert.equal(panel.default.navigation.description, "缓存效率与运行健康");
});

test("health details stay inside the mobile viewport", () => {
  assert.match(styles, /\.observe-health-error__detail\s*\{[^}]*max-width:\s*100%/s);
  assert.match(styles, /\.observe-health-error__detail pre\s*\{[^}]*max-width:\s*100%/s);
  assert.match(styles, /\.observe-health-error__detail pre\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(source, /aria-controls="observe-panel-health"/);
  assert.match(source, /event\.key === "ArrowRight"/);
});

test("urgent health fallback keeps small text at AA contrast", () => {
  const background = styles.match(/--observe-health-urgent:\s*(#[a-f\d]{6})/i)[1];
  const foreground = styles.match(/--observe-health-urgent-ink:\s*(#[a-f\d]{6})/i)[1];
  assert.ok(contrastRatio(background, foreground) >= 4.5);
});
