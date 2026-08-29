import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../web_module.js", import.meta.url), "utf8");
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

class Node {
  constructor(name, fragment = false) {
    this.name = name;
    this.fragment = fragment;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.textContent = "";
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = { setProperty() {} };
    this.classList = {
      add: (...names) => { this.className = [...new Set(`${this.className} ${names.join(" ")}`.trim().split(/\s+/))].join(" "); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter((name) => name && !names.includes(name)).join(" "); },
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node.fragment) {
        this.append(...node.children);
        continue;
      }
      node.parentNode = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.children) {
      if (child.contains(globalThis.document?.activeElement)) {
        globalThis.document.activeElement = globalThis.document.body;
      }
      child.parentNode = null;
    }
    this.children = [];
    this.append(...nodes);
  }

  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent.children.indexOf(this);
    parent.children[index] = node;
    node.parentNode = parent;
    this.parentNode = null;
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name, callback) { if (this.listeners.get(name) === callback) this.listeners.delete(name); }
  focus() {
    this.focused = true;
    globalThis.document.activeElement = this;
  }
  click() { this.listeners.get("click")?.({ target: this, preventDefault() {} }); }

  contains(node) {
    return this === node || this.children.some((child) => child.contains(node));
  }

  querySelector(selector) {
    if (!selector.startsWith(".")) return null;
    const name = selector.slice(1);
    return find(this, (node) => node.className.split(/\s+/).includes(name));
  }
}

function find(node, predicate) {
  for (const child of node.children) {
    if (predicate(child)) return child;
    const nested = find(child, predicate);
    if (nested) return nested;
  }
  return null;
}

async function flush() {
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
}

test("workbench panel uses ctx.http and disposes timers, listeners, and requests", async () => {
  const document = {
    body: new Node("body"),
    activeElement: null,
    createElement(name) { return new Node(name); },
    createDocumentFragment() { return new Node("fragment", true); },
  };
  const timers = new Set();
  const clearedTimers = new Set();
  const listeners = new Map();
  globalThis.document = document;
  globalThis.window = {
    setInterval(callback, milliseconds) {
      const timer = { callback, milliseconds };
      timers.add(timer);
      return timer;
    },
    clearInterval(timer) { clearedTimers.add(timer); },
    setTimeout(callback, milliseconds) { return { callback, milliseconds }; },
    clearTimeout() {},
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) { if (listeners.get(name) === callback) listeners.delete(name); },
  };
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { async writeText() {} } },
  });

  const requests = [];
  let releaseDrillList;
  const drillListReady = new Promise((resolve) => { releaseDrillList = resolve; });
  const item = {
    fingerprint: "fp-1",
    error_type: "RuntimeError",
    logger_name: "agent.loop",
    count: 3,
    sessions: 1,
    last_ts: "2026-08-30T00:00:00+00:00",
  };
  const detail = {
    ...item,
    source: "asyncio",
    level: "ERROR",
    status: "active",
    message: "background task failed",
    channel: "agent",
    first_ts: "2026-08-30T00:00:00+00:00",
    traceback_text: "Traceback",
    trend: [{ bucket: "2026-08-30T00", count: 3 }],
    variants: [],
    occurrences: [{ session_key: "mobile:demo", ts: "2026-08-30T00:00:00+00:00", user_preview: "hello" }],
  };
  const ctx = {
    http: {
      async request(path, options) {
        requests.push({ path, options });
        if (path.includes("/global_errors?")) await drillListReady;
        const body = path.includes("/timeseries")
          ? { points: [{ bucket: "2026-08-30T00", turns: 2, errors: 1, input_tokens: 44, passive_cache_hit_rate: 0.5, proactive_cache_hit_rate: 0.25, avg_iteration: 1.5 }] }
          : path.includes("/overview") && !path.includes("global_errors")
            ? { turns: 2, errors: 1, passive_cache_hit_rate: 0.5, proactive_cache_hit_rate: 0.25, avg_iteration: 1.5, max_iteration: 2, last_ts: "2026-08-30T00:00:00+00:00" }
            : path.includes("global_errors/overview")
              ? { total: 1, types: 1, new_types: 0, spiking_types: 0 }
              : path.includes("/status?")
                ? { ok: true }
                : path.includes("global_errors/fp-1?")
                  ? detail
                  : { sections: [{ key: "all", label: "", count: 3, items: [item] }] };
        return { ok: true, status: 200, async json() { return body; } };
      },
    },
    ui: {
      inject(contract, install) {
        assert.equal(contract, "workbench.panels.v1");
        install({ register(entry) { ctx.entry = entry; return () => {}; } });
        return ctx.activationDispose;
      },
    },
    activationDispose() {},
  };

  assert.equal(module.activate(ctx), ctx.activationDispose);
  assert.deepEqual(
    { id: ctx.entry.id, label: ctx.entry.label, order: ctx.entry.order, render: typeof ctx.entry.render },
    { id: "observe", label: "运行监测", order: 40, render: "function" },
  );

  const host = new Node("host");
  const dispose = ctx.entry.render(host);
  await flush();
  assert.equal(timers.size, 2);
  assert.equal(requests.filter((request) => request.path.includes("/overview")).length, 2);

  const open = find(host, (node) => node.name === "button" && node.textContent === "查看错误分析");
  assert.ok(open);
  open.click();
  await flush();
  const initialClose = find(host, (node) => node.attributes.get("aria-label") === "关闭错误分析");
  assert.equal(document.activeElement, initialClose);
  releaseDrillList();
  await flush();
  const settledClose = find(host, (node) => node.attributes.get("aria-label") === "关闭错误分析");
  assert.equal(document.activeElement, settledClose);
  assert.equal(
    find(host, (node) => node.id === "observe-error-dialog-title")?.id,
    "observe-error-dialog-title",
  );
  const acknowledge = find(host, (node) => node.name === "button" && node.textContent === "标记已确认");
  assert.ok(acknowledge);
  acknowledge.click();
  await flush();
  assert.ok(requests.some((request) => request.path.includes("/status?value=acknowledged") && request.options.method === "POST"));

  dispose();
  assert.equal(clearedTimers.size, 2);
  assert.equal(listeners.size, 0);
  assert.equal(document.body.children.length, 0);
  assert.equal(host.className, "");
  assert.ok(requests.every((request) => request.options.signal.aborted));
});
