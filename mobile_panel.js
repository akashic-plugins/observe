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

function messageUsage(host, context) {
  let active = true;
  const messageId = context.messageId;
  const sessionId = context.sessionId;
  if (!messageId || !sessionId) throw new Error("Turn token 统计缺少消息上下文");
  host.className += " observe-kv-tail-host";
  const key = `${sessionId}\n${messageId}`;
  let request = usageRequests.get(key);
  if (!request) {
    request = context.request("kvcache.message_usage", { message_id: messageId });
    usageRequests.set(key, request);
  }
  request.then((result) => {
    if (!active || !result.usage) return;
    const usage = result.usage;
    const row = document.createElement("div");
    row.className = "observe-kv-tail";
    row.setAttribute("aria-label", "本轮输出 Token");
    row.innerHTML = `
      <span class="observe-kv-tail__marker" aria-hidden="true"></span>
      <span>本轮输出 <strong>${number(usage.output_tokens)}</strong> tokens</span>`;
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
  section.querySelector("strong").textContent = rate(summary?.hit_rate);
  section.querySelector("span").textContent = `${title} · ${number(summary?.tracked_turn_count)} 轮`;
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

const dashboard = {
  mount(host, context) {
    let active = true;
    host.className += " observe-kv";
    host.innerHTML = `
      <div class="observe-kv-loading" role="status">正在读取 KV Cache…</div>
      <div class="observe-kv-content" hidden>
        <section class="observe-kv-overview" aria-label="KV Cache 概览">
          <div class="observe-kv-current">
            <div class="observe-kv-ring"><strong>—</strong></div>
            <span>最近 10 次被动链路</span>
          </div>
          <div class="observe-kv-sources">
            <div class="observe-kv-passive"><strong>—</strong><span>全局被动</span></div>
            <div class="observe-kv-proactive"><strong>—</strong><span>全局主动</span></div>
          </div>
        </section>
        <section class="observe-kv-list" aria-labelledby="observe-kv-list-title">
          <header><h2 id="observe-kv-list-title">最近 Turn</h2><span></span></header>
          <div class="observe-kv-turns"></div>
        </section>
      </div>`;
    const loading = host.querySelector(".observe-kv-loading");
    const content = host.querySelector(".observe-kv-content");
    Promise.all([
      context.request("kvcache.overview"),
      context.request("kvcache.turns", { page: 1, page_size: 50 }),
      context.request("kvcache.turns", { page: 1, page_size: 10, source: "agent" }),
    ]).then(([overview, page, passivePage]) => {
      if (!active) return;
      const turns = Array.isArray(page.items) ? page.items : [];
      const recent = Array.isArray(passivePage.items) ? passivePage.items : [];
      const recentHit = recent.reduce((sum, turn) => sum + Number(turn.hit_tokens || 0), 0);
      const recentPrompt = recent.reduce((sum, turn) => sum + Number(turn.prompt_tokens || 0), 0);
      const recentRate = recentPrompt > 0 ? recentHit / recentPrompt : null;
      const ring = host.querySelector(".observe-kv-ring");
      ring.style.setProperty("--observe-kv-rate", `${Math.max(0, Math.min(1, recentRate || 0)) * 100}%`);
      ring.querySelector("strong").textContent = rate(recentRate);
      metric(host, overview.passive, ".observe-kv-passive", "全局被动");
      metric(host, overview.proactive, ".observe-kv-proactive", "全局主动");
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
  navigation: {
    label: "KV Cache",
    description: "Token 复用与 Turn 明细",
  },
  dashboard,
};
