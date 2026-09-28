function number(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function rate(value) {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

function shortTime(value) {
  const date = new Date(String(value || ""));
  if (Number.isNaN(date.getTime())) return String(value || "—");
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function sourceLabel(value) {
  if (value === "agent") return "被动";
  if (value === "proactive" || value === "drift") return "主动";
  return String(value || "其他");
}

const usageRequests = new Map();

function wait(delay) {
  return new Promise((resolve) => window.setTimeout(resolve, delay));
}

async function requestMessageUsage(context, messageId) {
  const delays = [0, 250];
  for (const delay of delays) {
    if (delay > 0) await wait(delay);
    const result = await context.query(
      "kvcache.message_usage",
      { message_id: messageId },
      { cache: "immutable" },
    );
    if (result.usage) return result;
  }
  return { usage: null };
}

function messageUsage(host, context) {
  let active = true;
  const messageId = context.messageId;
  const sessionId = context.sessionId;
  if (!messageId || !sessionId) throw new Error("Turn token 统计缺少消息上下文");
  host.className += " observe-kv-tail-host";
  const key = `${sessionId}\n${messageId}`;
  let request = usageRequests.get(key);
  if (!request) {
    request = requestMessageUsage(context, messageId);
    usageRequests.set(key, request);
  }
  request.then((result) => {
    if (!result.usage) {
      usageRequests.delete(key);
      return;
    }
    if (!active) return;
    const usage = result.usage;
    const row = document.createElement("div");
    row.className = "observe-kv-tail";
    row.setAttribute("aria-label", "本轮输出 Token");
    row.innerHTML = `
      <span>输出 <strong>${number(usage.output_tokens)} tokens</strong></span>`;
    host.replaceChildren(row);
  }).catch((error) => {
    usageRequests.delete(key);
    if (!active) return;
    host.classList.add("observe-kv-tail-host--error");
    host.textContent = error instanceof Error ? `Token 统计不可用：${error.message}` : "Token 统计不可用";
  });
  return () => { active = false; };
}

function metric(host, summary, className, title) {
  const section = host.querySelector(className);
  const count = Number(summary?.tracked_turn_count || 0);
  section.classList.toggle("empty", count === 0);
  section.querySelector("strong").textContent = count === 0 ? "暂无记录" : rate(summary?.hit_rate);
  section.querySelector("span").textContent = count === 0 ? title : `${title} · ${number(count)} 轮`;
}

function turnRow(turn) {
  const item = document.createElement("article");
  item.className = "observe-kv-turn";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "observe-kv-turn__trigger";
  trigger.setAttribute("aria-expanded", "false");
  const copy = document.createElement("span");
  copy.className = "observe-kv-turn__copy";
  const title = document.createElement("strong");
  title.textContent = turn.user_preview || "（无内容）";
  const meta = document.createElement("small");
  meta.textContent = `${sourceLabel(turn.source)} · ${shortTime(turn.ts)}`;
  copy.append(title, meta);
  const values = document.createElement("span");
  values.className = "observe-kv-turn__values";
  const hitRate = document.createElement("strong");
  hitRate.textContent = rate(turn.hit_rate);
  if (typeof turn.hit_rate === "number" && turn.hit_rate < 0.5) hitRate.className = "low";
  const token = document.createElement("small");
  token.textContent = `${number(turn.hit_tokens)} / ${number(turn.prompt_tokens)}`;
  values.append(hitRate, token);
  trigger.append(copy, values);

  const detail = document.createElement("dl");
  detail.className = "observe-kv-turn__detail";
  detail.hidden = true;
  const fields = [
    ["Session", turn.session_key || "—"],
    ["Prompt", number(turn.prompt_tokens)],
    ["Hit", number(turn.hit_tokens)],
    ["Miss", number(turn.miss_tokens)],
    ["时间", shortTime(turn.ts)],
  ];
  for (const [label, value] of fields) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    detail.append(term, description);
  }
  trigger.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    trigger.setAttribute("aria-expanded", String(!detail.hidden));
  });
  item.append(trigger, detail);
  return item;
}

export function healthState(summary) {
  const types = Number(summary?.types || 0);
  const spikes = Number(summary?.spiking_types || 0);
  const fresh = Number(summary?.new_types || 0);
  if (types === 0) {
    return {
      tone: "steady",
      title: "运行平稳",
      description: "最近 24 小时没有收集到需要处理的错误。",
    };
  }
  if (spikes > 0) {
    return {
      tone: "urgent",
      title: `${spikes} 类错误正在增加`,
      description: `共 ${number(summary.total)} 次，先查看爆发项。`,
    };
  }
  return {
    tone: fresh > 0 ? "attention" : "active",
    title: fresh > 0 ? `${fresh} 类新问题` : `${types} 类问题待查看`,
    description: `最近 24 小时共 ${number(summary.total)} 次。`,
  };
}

function healthDetailRow(label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = String(value || "—");
  return [term, description];
}

function healthErrorRow(error, context) {
  const item = document.createElement("article");
  item.className = "observe-health-error";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "observe-health-error__trigger";
  trigger.setAttribute("aria-expanded", "false");
  const copy = document.createElement("span");
  copy.className = "observe-health-error__copy";
  const title = document.createElement("strong");
  title.textContent = error.error_type || "Error";
  const message = document.createElement("span");
  message.textContent = error.message || "没有错误摘要";
  const meta = document.createElement("small");
  meta.textContent = `${shortTime(error.last_ts)} · ${number(error.sessions)} 个会话`;
  copy.append(title, message, meta);
  const signal = document.createElement("span");
  signal.className = "observe-health-error__signal";
  const count = document.createElement("strong");
  count.textContent = `${number(error.count)} 次`;
  const state = document.createElement("small");
  state.textContent = error.is_spiking ? "正在增加" : error.is_new ? "新出现" : "最近出现";
  if (error.is_spiking) signal.dataset.tone = "urgent";
  else if (error.is_new) signal.dataset.tone = "attention";
  signal.append(count, state);
  trigger.append(copy, signal);

  const detail = document.createElement("div");
  detail.className = "observe-health-error__detail";
  detail.hidden = true;
  let loaded = false;
  trigger.addEventListener("click", async () => {
    detail.hidden = !detail.hidden;
    trigger.setAttribute("aria-expanded", String(!detail.hidden));
    if (detail.hidden || loaded) return;
    loaded = true;
    detail.textContent = "正在读取现场…";
    try {
      const result = await context.query("health.error_detail", {
        range: "24h",
        fingerprint: error.fingerprint,
      });
      if (!result.error) {
        detail.textContent = "这条错误已不在 Observe 中。";
        return;
      }
      const data = result.error;
      const fields = document.createElement("dl");
      fields.append(
        ...healthDetailRow("来源", data.logger_name || data.source),
        ...healthDetailRow("首次出现", shortTime(data.first_ts)),
        ...healthDetailRow("最近出现", shortTime(data.last_ts)),
      );
      detail.replaceChildren(fields);
      if (data.traceback) {
        const trace = document.createElement("pre");
        trace.textContent = data.traceback;
        detail.append(trace);
      }
    } catch (requestError) {
      loaded = false;
      detail.textContent = requestError instanceof Error
        ? `现场读取失败：${requestError.message}`
        : "现场读取失败";
    }
  });
  item.append(trigger, detail);
  return item;
}

function renderHealth(host, context, snapshot) {
  const summary = snapshot;
  const page = snapshot;
  const state = healthState(summary);
  const hero = host.querySelector(".observe-health-hero");
  hero.dataset.tone = state.tone;
  hero.querySelector("strong").textContent = state.title;
  hero.querySelector("p").textContent = state.description;
  const metrics = [
    [".observe-health-total strong", summary.total],
    [".observe-health-new strong", summary.new_types],
    [".observe-health-spiking strong", summary.spiking_types],
  ];
  for (const [selector, value] of metrics) {
    host.querySelector(selector).textContent = number(value);
  }
  host.querySelector(".observe-health-new").classList.toggle("empty", Number(summary.new_types || 0) === 0);
  host.querySelector(".observe-health-spiking").classList.toggle("empty", Number(summary.spiking_types || 0) === 0);
  const list = host.querySelector(".observe-health-errors");
  const items = Array.isArray(page.items) ? page.items : [];
  host.querySelector(".observe-health-list header span").textContent = `${number(page.types)} 类`;
  if (items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "observe-health-empty";
    empty.textContent = "这里会显示需要关注的运行问题。";
    list.append(empty);
  } else {
    list.append(...items.map((error) => healthErrorRow(error, context)));
  }
}

const dashboard = {
  mount(host, context) {
    let active = true;
    let healthLoaded = false;
    host.className += " observe-kv";
    host.innerHTML = `
      <div class="observe-view-switch" role="tablist" aria-label="Observe 看板">
        <button id="observe-tab-cache" type="button" role="tab" aria-controls="observe-panel-cache" aria-selected="true" tabindex="0" data-view="cache">缓存效率</button>
        <button id="observe-tab-health" type="button" role="tab" aria-controls="observe-panel-health" aria-selected="false" tabindex="-1" data-view="health">运行健康</button>
      </div>
      <div id="observe-panel-cache" class="observe-view" role="tabpanel" aria-labelledby="observe-tab-cache" data-panel="cache">
        <div class="observe-kv-loading" role="status">正在读取 KV Cache…</div>
        <div class="observe-kv-content" hidden>
          <section class="observe-kv-overview" aria-label="KV Cache 概览">
            <div class="observe-kv-current">
              <div class="observe-kv-ring"><strong>—</strong></div>
              <span>近期被动复用</span>
            </div>
            <div class="observe-kv-sources">
              <div class="observe-kv-passive"><strong>—</strong><span>被动总览</span></div>
              <div class="observe-kv-proactive"><strong>—</strong><span>主动链路</span></div>
            </div>
          </section>
          <section class="observe-kv-list" aria-labelledby="observe-kv-list-title">
            <header><h2 id="observe-kv-list-title">最近 Turn</h2><span></span></header>
            <div class="observe-kv-turns"></div>
          </section>
        </div>
      </div>
      <div id="observe-panel-health" class="observe-view" role="tabpanel" aria-labelledby="observe-tab-health" data-panel="health" hidden>
        <div class="observe-health-loading" role="status">正在读取运行状态…</div>
        <div class="observe-health-content" hidden>
          <section class="observe-health-hero" data-tone="steady" aria-label="运行状态">
            <span>最近 24 小时</span>
            <strong>—</strong>
            <p></p>
          </section>
          <section class="observe-health-metrics" aria-label="错误指标">
            <div class="observe-health-total"><strong>0</strong><span>出现次数</span></div>
            <div class="observe-health-new"><strong>0</strong><span>新类型</span></div>
            <div class="observe-health-spiking"><strong>0</strong><span>正在增加</span></div>
          </section>
          <section class="observe-health-list" aria-labelledby="observe-health-list-title">
            <header><h2 id="observe-health-list-title">最近问题</h2><span></span></header>
            <div class="observe-health-errors"></div>
          </section>
        </div>
      </div>`;
    const selectView = (view) => {
      for (const button of host.querySelectorAll(".observe-view-switch button")) {
        const selected = button.dataset.view === view;
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
      }
      for (const panel of host.querySelectorAll(".observe-view")) {
        panel.hidden = panel.dataset.panel !== view;
      }
      if (view !== "health" || healthLoaded) return;
      healthLoaded = true;
      context.query("health.snapshot", { range: "24h" }).then((snapshot) => {
        if (!active) return;
        renderHealth(host, context, snapshot);
        host.querySelector(".observe-health-loading").remove();
        host.querySelector(".observe-health-content").hidden = false;
      }).catch((error) => {
        if (!active) return;
        healthLoaded = false;
        const loading = host.querySelector(".observe-health-loading");
        loading.className = "observe-health-loading error";
        loading.textContent = error instanceof Error
          ? `运行状态读取失败：${error.message}`
          : "运行状态读取失败";
      });
    };
    const viewButtons = [...host.querySelectorAll(".observe-view-switch button")];
    for (const button of viewButtons) {
      button.addEventListener("click", () => selectView(button.dataset.view));
      button.addEventListener("keydown", (event) => {
        const current = viewButtons.indexOf(button);
        const target = event.key === "Home"
          ? 0
          : event.key === "End"
            ? viewButtons.length - 1
            : event.key === "ArrowRight"
              ? (current + 1) % viewButtons.length
              : event.key === "ArrowLeft"
                ? (current - 1 + viewButtons.length) % viewButtons.length
                : -1;
        if (target < 0) return;
        event.preventDefault();
        const next = viewButtons[target];
        selectView(next.dataset.view);
        next.focus();
      });
    }
    const loading = host.querySelector(".observe-kv-loading");
    const content = host.querySelector(".observe-kv-content");
    context.query("kvcache.bootstrap").then((bootstrap) => {
      if (!active) return;
      const overview = bootstrap.overview || {};
      const page = bootstrap.recent || { items: [], total: 0 };
      const passivePage = bootstrap.recent_agent || { items: [], total: 0 };
      const turns = Array.isArray(page.items) ? page.items : [];
      const recent = Array.isArray(passivePage.items) ? passivePage.items : [];
      const recentHit = recent.reduce((sum, turn) => sum + Number(turn.hit_tokens || 0), 0);
      const recentPrompt = recent.reduce((sum, turn) => sum + Number(turn.prompt_tokens || 0), 0);
      const recentRate = recentPrompt > 0 ? recentHit / recentPrompt : null;
      const ring = host.querySelector(".observe-kv-ring");
      ring.style.setProperty("--observe-kv-rate", `${Math.max(0, Math.min(1, recentRate || 0)) * 100}%`);
      ring.querySelector("strong").textContent = rate(recentRate);
      metric(host, overview.passive, ".observe-kv-passive", "被动总览");
      metric(host, overview.proactive, ".observe-kv-proactive", "主动链路");
      host.querySelector(".observe-kv-list header span").textContent = `${number(page.total)} 轮`;
      const list = host.querySelector(".observe-kv-turns");
      if (turns.length === 0) {
        const empty = document.createElement("p");
        empty.className = "observe-kv-empty";
        empty.textContent = "暂无 KV Cache 记录。";
        list.append(empty);
      } else {
        list.append(...turns.map(turnRow));
      }
      loading.remove();
      content.hidden = false;
    }).catch((error) => {
      if (!active) return;
      loading.className = "observe-kv-loading error";
      loading.textContent = error instanceof Error ? `KV Cache 读取失败：${error.message}` : "KV Cache 读取失败";
    });
    return () => { active = false; };
  },
};

export default {
  slots: {
    "turn.after_answer": { mount: messageUsage },
  },
  dashboard,
};
