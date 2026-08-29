const RANGES = [
  ["24h", "24 小时"],
  ["7d", "7 天"],
  ["30d", "30 天"],
  ["all", "全部"],
];

const STATUS_LABELS = {
  active: "活跃",
  acknowledged: "已确认",
  ignored: "已忽略",
};

const SOURCE_LABELS = {
  log: "主动日志",
  uncaught: "未捕获异常",
  asyncio: "asyncio 任务",
  thread: "子线程",
};

export function activate(ctx) {
  return ctx.ui.inject("workbench.panels.v1", (mount) => mount.register({
    id: "observe",
    label: "运行监测",
    order: 40,
    render(host) {
      return mountObservePanel(host, ctx);
    },
  }));
}

function mountObservePanel(host, ctx) {
  const state = {
    range: "24h",
    overview: null,
    points: [],
    globalErrors: null,
    updatedAt: 0,
    refreshing: false,
    error: "",
    disposed: false,
  };
  let mainRequest = new AbortController();
  let drillDispose = null;
  let refreshTimer = 0;
  let clockTimer = 0;
  let updatedLabel = null;
  let drillTrigger = null;
  const surface = element("div", "observe-surface");
  const overlay = element("div", "observe-overlay-root");

  host.classList.add("observe-web");
  host.replaceChildren(surface, overlay);

  const closeDrill = () => {
    if (drillDispose === null) return;
    const dispose = drillDispose;
    drillDispose = null;
    dispose();
    host.classList.remove("observe-web--drill-open");
    surface.inert = false;
    if (drillTrigger?.isConnected) drillTrigger.focus();
    drillTrigger = null;
  };

  const openDrill = () => {
    if (state.disposed || drillDispose !== null) return;
    drillTrigger = document.activeElement;
    host.classList.add("observe-web--drill-open");
    surface.inert = true;
    drillDispose = mountErrorDrill(ctx, state.range, overlay, closeDrill, loadMain);
  };

  const render = () => {
    const page = element("section", "observe-page");
    const header = element("header", "observe-header");
    const heading = element("div", "observe-heading");
    const titleLine = element("div", "observe-title-line");
    titleLine.append(
      element("h1", "observe-title", "Observe · 监测"),
      element(
        "span",
        `observe-live${state.error ? " observe-live--error" : ""}`,
        state.error ? "更新失败" : state.refreshing ? "正在刷新" : "实时更新",
      ),
    );
    const subtitle = element("p", "observe-subtitle", "Agent 主循环遥测 · Token / 迭代 / 错误");
    updatedLabel = element("span", "observe-updated");
    subtitle.append(updatedLabel);
    heading.append(titleLine, subtitle);
    header.append(heading, buildHeaderActions());
    page.append(header);

    if (state.error) {
      page.append(errorNotice(state.error));
    }
    if (state.overview === null) {
      page.append(buildSkeleton());
    } else {
      page.append(buildOverview(openDrill));
    }
    surface.replaceChildren(page);
    renderUpdatedTime();
  };

  const renderUpdatedTime = () => {
    if (updatedLabel === null) return;
    if (state.updatedAt === 0) {
      updatedLabel.textContent = "正在读取…";
      return;
    }
    updatedLabel.textContent = `更新于 ${ago(Date.now() - state.updatedAt)}`;
  };

  const loadMain = async () => {
    mainRequest.abort();
    mainRequest = new AbortController();
    const request = mainRequest;
    state.refreshing = true;
    render();
    try {
      const range = encodeURIComponent(state.range);
      const [overview, series, globalErrors] = await Promise.all([
        json(ctx, `/api/dashboard/observe/overview?range=${range}`, { signal: request.signal }),
        json(ctx, `/api/dashboard/observe/timeseries?range=${range}`, { signal: request.signal }),
        json(ctx, `/api/dashboard/observe/global_errors/overview?range=${range}`, { signal: request.signal }),
      ]);
      if (state.disposed || request !== mainRequest || request.signal.aborted) return;
      state.overview = overview;
      state.points = Array.isArray(series.points) ? series.points : [];
      state.globalErrors = globalErrors;
      state.updatedAt = Date.now();
      state.error = "";
    } catch (reason) {
      if (!request.signal.aborted && !state.disposed) {
        state.error = `无法刷新监测数据：${errorMessage(reason)}`;
      }
    } finally {
      if (!state.disposed && request === mainRequest) {
        state.refreshing = false;
        render();
      }
    }
  };

  function buildHeaderActions() {
    const actions = element("div", "observe-actions");
    const refresh = element("button", "observe-icon-button", "↻");
    refresh.type = "button";
    refresh.title = "刷新";
    refresh.setAttribute("aria-label", "刷新监测数据");
    refresh.disabled = state.refreshing;
    refresh.addEventListener("click", () => void loadMain());
    const ranges = element("div", "observe-ranges");
    for (const [key, label] of RANGES) {
      const button = element("button", "observe-range", label);
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.range === key));
      if (state.range === key) button.classList.add("is-selected");
      button.addEventListener("click", () => {
        if (state.range === key) return;
        state.range = key;
        state.overview = null;
        state.points = [];
        state.globalErrors = null;
        state.updatedAt = 0;
        void loadMain();
      });
      ranges.append(button);
    }
    actions.append(refresh, ranges);
    return actions;
  }

  function buildOverview(onOpenDrill) {
    const overview = state.overview;
    const points = state.points;
    const page = document.createDocumentFragment();
    const globalTotal = number(state.globalErrors?.total, number(overview.errors));
    const health = element(
      "section",
      `observe-health${globalTotal > 0 ? " observe-health--attention" : ""}`,
    );
    const healthText = element("div", "observe-health-text");
    healthText.append(
      element(
        "strong",
        "",
        globalTotal > 0 ? `${globalTotal} 条错误需要查看` : "当前区间没有采集到错误",
      ),
      element(
        "p",
        "",
        globalTotal > 0
          ? `${number(state.globalErrors?.types)} 个错误类型，先查看爆发和新出现的类型。`
          : "主循环遥测持续更新，缓存与迭代指标见下方。",
      ),
    );
    health.append(healthText);
    if (globalTotal > 0) {
      const drill = element("button", "observe-drill-button", "查看错误分析");
      drill.type = "button";
      drill.addEventListener("click", onOpenDrill);
      health.append(drill);
    }
    page.append(health);

    const turns = points.map((point) => number(point.turns));
    const errors = points.map((point) => number(point.errors));
    const inputTokens = points.map((point) => number(point.input_tokens));
    const passiveRate = points.map((point) => number(point.passive_cache_hit_rate) * 100);
    const proactiveRate = points.map((point) => number(point.proactive_cache_hit_rate) * 100);
    const iterations = points.map((point) => number(point.avg_iteration));
    const metrics = element("div", "observe-metrics");
    metrics.append(
      metricTile("对话轮数", compact(number(overview.turns)), {
        sub: overview.last_ts ? `最近 ${shortTimestamp(overview.last_ts)}` : "无记录",
        delta: delta(turns),
        values: turns,
        tone: "accent",
      }),
      metricTile("被动 KV 命中率", percent(overview.passive_cache_hit_rate), {
        sub: `主动 ${percent(overview.proactive_cache_hit_rate)}`,
        values: passiveRate,
        tone: "success",
      }),
      metricTile("平均迭代", overview.avg_iteration == null ? "—" : number(overview.avg_iteration).toFixed(1), {
        sub: `峰 ${number(overview.max_iteration)} · 每轮 LLM 调用次数`,
        values: iterations,
        tone: "warning",
      }),
    );
    page.append(metrics);

    const charts = element("div", "observe-charts");
    charts.append(
      chartCard("输入 Token 趋势", points, inputTokens, compact, "accent"),
      chartCard("平均迭代趋势", points, iterations, (value) => value.toFixed(1), "warning"),
      chartCard("全局被动链路命中率趋势", points, passiveRate, (value) => `${value.toFixed(0)}%`, "success"),
      chartCard("全局主动链路命中率趋势", points, proactiveRate, (value) => `${value.toFixed(0)}%`, "accent"),
      chartCard("错误趋势", points, errors, String, "danger", "所选区间内没有错误"),
    );
    page.append(charts);
    return page;
  }

  render();
  void loadMain();
  refreshTimer = window.setInterval(() => void loadMain(), 15_000);
  clockTimer = window.setInterval(renderUpdatedTime, 1_000);
  return () => {
    state.disposed = true;
    window.clearInterval(refreshTimer);
    window.clearInterval(clockTimer);
    mainRequest.abort();
    closeDrill();
    host.classList.remove("observe-web", "observe-web--drill-open");
    host.replaceChildren();
  };
}

function mountErrorDrill(ctx, range, overlay, requestClose, onStatusSaved) {
  const state = {
    overview: null,
    sections: [],
    selected: null,
    detail: null,
    facet: "type",
    query: "",
    tab: "trace",
    variant: 0,
    listError: "",
    detailError: "",
    statusError: "",
    loadingList: false,
    loadingDetail: false,
    savingStatus: false,
    disposed: false,
  };
  let listRequest = new AbortController();
  let detailRequest = new AbortController();
  let statusRequest = new AbortController();
  let searchTimer = 0;
  let focused = false;
  const backdrop = element("div", "observe-dialog-backdrop");
  const dialog = element("section", "observe-dialog");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "observe-error-dialog-title");
  backdrop.append(dialog);
  overlay.replaceChildren(backdrop);

  const onKeyDown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      requestClose();
      return;
    }
    if (event.key === "Tab") {
      const focusable = [...dialog.querySelectorAll("button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])")];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  };
  const onBackdropClick = (event) => {
    if (event.target === backdrop) requestClose();
  };
  backdrop.addEventListener("keydown", onKeyDown);
  backdrop.addEventListener("click", onBackdropClick);

  const select = (fingerprint) => {
    if (state.selected === fingerprint) return;
    state.selected = fingerprint;
    state.detail = null;
    state.detailError = "";
    state.tab = "trace";
    state.variant = 0;
    render();
    void loadDetail();
  };

  const loadList = async () => {
    listRequest.abort();
    listRequest = new AbortController();
    const request = listRequest;
    state.loadingList = true;
    state.listError = "";
    render();
    try {
      const base = `/api/dashboard/observe/global_errors?range=${encodeURIComponent(range)}`;
      const [overview, list] = await Promise.all([
        json(ctx, `/api/dashboard/observe/global_errors/overview?range=${encodeURIComponent(range)}`, { signal: request.signal }),
        json(ctx, `${base}&facet=${encodeURIComponent(state.facet)}&q=${encodeURIComponent(state.query)}`, { signal: request.signal }),
      ]);
      if (state.disposed || request !== listRequest || request.signal.aborted) return;
      state.overview = overview;
      state.sections = Array.isArray(list.sections) ? list.sections : [];
      const items = state.sections.flatMap((section) => Array.isArray(section.items) ? section.items : []);
      const selectedStillExists = items.some((item) => item.fingerprint === state.selected);
      const next = selectedStillExists ? state.selected : items[0]?.fingerprint ?? null;
      const selectionChanged = next !== state.selected;
      state.selected = next;
      if (selectionChanged) {
        state.detail = null;
        state.tab = "trace";
        state.variant = 0;
      }
      if (next && (selectionChanged || state.detail === null)) void loadDetail();
    } catch (reason) {
      if (!request.signal.aborted && !state.disposed) {
        state.listError = `无法读取错误列表：${errorMessage(reason)}`;
      }
    } finally {
      if (!state.disposed && request === listRequest) {
        state.loadingList = false;
        render();
      }
    }
  };

  const loadDetail = async () => {
    if (!state.selected) return;
    detailRequest.abort();
    detailRequest = new AbortController();
    const request = detailRequest;
    const fingerprint = state.selected;
    state.loadingDetail = true;
    state.detailError = "";
    render();
    try {
      const detail = await json(
        ctx,
        `/api/dashboard/observe/global_errors/${encodeURIComponent(fingerprint)}?range=${encodeURIComponent(range)}`,
        { signal: request.signal },
      );
      if (state.disposed || request !== detailRequest || request.signal.aborted || state.selected !== fingerprint) return;
      state.detail = detail;
      state.variant = 0;
      state.tab = "trace";
    } catch (reason) {
      if (!request.signal.aborted && !state.disposed) {
        state.detailError = `无法读取错误详情：${errorMessage(reason)}`;
      }
    } finally {
      if (!state.disposed && request === detailRequest) {
        state.loadingDetail = false;
        render();
      }
    }
  };

  const setStatus = async (value) => {
    if (!state.detail || state.savingStatus) return;
    statusRequest.abort();
    statusRequest = new AbortController();
    const request = statusRequest;
    const fingerprint = state.detail.fingerprint;
    state.savingStatus = true;
    state.statusError = "";
    render();
    try {
      const response = await json(
        ctx,
        `/api/dashboard/observe/global_errors/${encodeURIComponent(fingerprint)}/status?value=${encodeURIComponent(value)}`,
        { method: "POST", signal: request.signal },
      );
      if (response.ok === false) throw new Error("状态没有保存");
      if (state.disposed || request !== statusRequest || request.signal.aborted) return;
      if (state.detail?.fingerprint === fingerprint) state.detail.status = value;
      void loadList();
      void onStatusSaved();
    } catch (reason) {
      if (!request.signal.aborted && !state.disposed) {
        state.statusError = `无法更新错误状态：${errorMessage(reason)}`;
      }
    } finally {
      if (!state.disposed && request === statusRequest) {
        state.savingStatus = false;
        render();
      }
    }
  };

  const copyTraceback = async () => {
    if (!state.detail?.traceback_text || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(state.detail.traceback_text);
    } catch (reason) {
      if (!state.disposed) {
        state.statusError = `无法复制 Traceback：${errorMessage(reason)}`;
        render();
      }
    }
  };

  function render() {
    const focusWasInDialog = dialog.contains(document.activeElement);
    const previousSearch = document.activeElement?.classList?.contains("observe-search")
      ? document.activeElement
      : null;
    const selection = previousSearch
      ? [previousSearch.selectionStart, previousSearch.selectionEnd]
      : null;
    const heading = element("header", "observe-dialog-header");
    const headingText = element("div", "observe-dialog-heading");
    const title = element("h2", "", `错误 · ${RANGES.find(([key]) => key === range)?.[1] ?? range}`);
    title.id = "observe-error-dialog-title";
    headingText.append(
      element("strong", "observe-dialog-total", String(number(state.overview?.total))),
      title,
      element(
        "p",
        "",
        `${number(state.overview?.types)} 个类型 · ${number(state.overview?.new_types)} 个新类型 · ${number(state.overview?.spiking_types)} 个正在爆发`,
      ),
    );
    const close = element("button", "observe-icon-button", "×");
    close.type = "button";
    close.setAttribute("aria-label", "关闭错误分析");
    close.addEventListener("click", requestClose);
    heading.append(headingText, close);

    const controls = element("div", "observe-drill-controls");
    const facets = element("div", "observe-facets");
    for (const [key, label] of [["type", "按类型"], ["source", "按来源"], ["channel", "按通道"]]) {
      const facet = element("button", "observe-facet", label);
      facet.type = "button";
      facet.setAttribute("aria-pressed", String(state.facet === key));
      if (state.facet === key) facet.classList.add("is-selected");
      facet.addEventListener("click", () => {
        if (state.facet === key) return;
        state.facet = key;
        state.detail = null;
        void loadList();
      });
      facets.append(facet);
    }
    const search = element("input", "observe-search");
    search.type = "search";
    search.placeholder = "按消息 / 模块过滤…";
    search.value = state.query;
    search.setAttribute("aria-label", "搜索错误");
    search.addEventListener("input", () => {
      state.query = search.value;
      state.detail = null;
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => void loadList(), 200);
    });
    controls.append(facets, search);

    const body = element("div", "observe-drill-body");
    body.append(buildErrorList(state, select), buildErrorDetail(state, setStatus, copyTraceback));
    dialog.replaceChildren(heading, controls, body);
    if (previousSearch) {
      search.focus();
      if (selection[0] !== null && selection[1] !== null) {
        search.setSelectionRange(selection[0], selection[1]);
      }
    } else if (focusWasInDialog || !focused) {
      focused = true;
      close.focus();
    }
  }

  render();
  void loadList();
  return () => {
    state.disposed = true;
    listRequest.abort();
    detailRequest.abort();
    statusRequest.abort();
    window.clearTimeout(searchTimer);
    backdrop.removeEventListener("keydown", onKeyDown);
    backdrop.removeEventListener("click", onBackdropClick);
    overlay.replaceChildren();
  };
}

function buildErrorList(state, select) {
  const list = element("aside", "observe-error-list");
  if (state.listError) list.append(errorNotice(state.listError));
  if (state.loadingList && state.sections.length === 0) list.append(element("p", "observe-muted", "正在读取错误列表…"));
  for (const section of state.sections) {
    const group = element("section", "observe-error-section");
    if (section.label) {
      const sectionTitle = element("div", "observe-error-section-title");
      sectionTitle.append(element("strong", "", section.label), element("span", "", `${number(section.count)} 次`));
      group.append(sectionTitle);
    }
    for (const item of Array.isArray(section.items) ? section.items : []) {
      const row = element("button", "observe-error-row");
      row.type = "button";
      row.setAttribute("aria-pressed", String(item.fingerprint === state.selected));
      if (item.fingerprint === state.selected) row.classList.add("is-selected");
      const heading = element("div", "observe-error-row-heading");
      heading.append(
        element("strong", "", String(item.error_type || "未知错误")),
        item.is_new ? element("span", "observe-tag observe-tag--new", "新") : document.createDocumentFragment(),
        item.is_spiking ? element("span", "observe-tag observe-tag--danger", "爆发") : document.createDocumentFragment(),
      );
      row.append(
        heading,
        element("span", "observe-error-logger", String(item.logger_name || "")),
        element("span", "observe-error-row-meta", `${number(item.count)} 次 · ${number(item.sessions)} session · ${shortTimestamp(item.last_ts)}`),
      );
      row.addEventListener("click", () => select(item.fingerprint));
      group.append(row);
    }
    list.append(group);
  }
  if (!state.loadingList && state.sections.length === 0 && !state.listError) {
    list.append(element("p", "observe-muted", "所选区间内没有错误。"));
  }
  return list;
}

function buildErrorDetail(state, setStatus, copyTraceback) {
  const detail = element("section", "observe-error-detail");
  if (state.detailError) {
    detail.append(errorNotice(state.detailError));
    return detail;
  }
  if (state.loadingDetail) {
    detail.append(element("p", "observe-muted", "正在读取错误详情…"));
    return detail;
  }
  if (!state.detail) {
    detail.append(element("p", "observe-muted", "选择左侧一个错误查看现场。"));
    return detail;
  }
  const item = state.detail;
  const title = element("header", "observe-detail-heading");
  title.append(
    element("h3", "", String(item.error_type || "未知错误")),
    element("p", "observe-detail-message", String(item.message || "")),
  );
  const tags = element("div", "observe-tags");
  for (const text of [
    item.logger_name,
    `来源 · ${SOURCE_LABELS[item.source] || item.source || "未知"}`,
    item.channel,
    item.level,
    STATUS_LABELS[item.status] || item.status || "活跃",
  ]) {
    if (text) tags.append(element("span", "observe-tag", String(text)));
  }
  title.append(tags);
  detail.append(title, detailStats(item));

  const tabs = element("div", "observe-detail-tabs");
  for (const [key, label] of [["trend", "趋势"], ["trace", `Traceback${Array.isArray(item.variants) && item.variants.length > 1 ? ` · ${item.variants.length} 变体` : ""}`], ["occ", `现场 · ${Array.isArray(item.occurrences) ? item.occurrences.length : 0}`]]) {
    const tab = element("button", "observe-tab", label);
    tab.type = "button";
    tab.setAttribute("aria-selected", String(state.tab === key));
    if (state.tab === key) tab.classList.add("is-selected");
    tab.addEventListener("click", () => {
      state.tab = key;
      buildErrorDetailInto(detail, state, setStatus, copyTraceback);
    });
    tabs.append(tab);
  }
  detail.append(tabs);
  buildErrorDetailInto(detail, state, setStatus, copyTraceback);
  return detail;
}

function buildErrorDetailInto(detail, state, setStatus, copyTraceback) {
  const item = state.detail;
  const content = element("div", "observe-detail-content");
  if (state.tab === "trend") {
    const trend = Array.isArray(item.trend) ? item.trend : [];
    content.append(chartCard("错误趋势", trend, trend.map((point) => number(point.count)), String, "danger", "区间内无发作"));
  } else if (state.tab === "trace") {
    const variants = Array.isArray(item.variants) ? item.variants : [];
    if (variants.length > 1) {
      const choices = element("div", "observe-variants");
      variants.forEach((variant, index) => {
        const choice = element("button", "observe-variant", `${number(variant.count)} 次 · 变体 ${index + 1}`);
        choice.type = "button";
        if (state.variant === index) choice.classList.add("is-selected");
        choice.addEventListener("click", () => {
          state.variant = index;
          buildErrorDetailInto(detail, state, setStatus, copyTraceback);
        });
        choices.append(choice);
      });
      content.append(choices);
    }
    const trace = variants[state.variant]?.traceback_text || item.traceback_text || "没有 Traceback。";
    content.append(element("pre", "observe-traceback", String(trace)));
  } else {
    const occurrences = Array.isArray(item.occurrences) ? item.occurrences : [];
    if (occurrences.length === 0) {
      content.append(element("p", "observe-muted", "无可关联的 session 现场。"));
    }
    for (const occurrence of occurrences) {
      const row = element("article", "observe-occurrence");
      row.append(
        element("span", "observe-occurrence-time", shortTimestamp(occurrence.ts)),
        element("p", "", String(occurrence.user_preview || "（无用户消息）")),
        element("code", "observe-session-key", `session ${String(occurrence.session_key || "")}`),
      );
      content.append(row);
    }
  }
  const existing = detail.querySelector(".observe-detail-content");
  if (existing) existing.replaceWith(content);
  else detail.append(content);

  let actions = detail.querySelector(".observe-detail-actions");
  if (!actions) {
    actions = element("footer", "observe-detail-actions");
    detail.append(actions);
  }
  actions.replaceChildren();
  const copy = element("button", "observe-secondary-button", "复制 Traceback");
  copy.type = "button";
  copy.addEventListener("click", () => void copyTraceback());
  const spacer = element("span", "observe-actions-spacer");
  const acknowledged = element("button", "observe-secondary-button", "标记已确认");
  acknowledged.type = "button";
  acknowledged.disabled = state.savingStatus;
  acknowledged.addEventListener("click", () => void setStatus("acknowledged"));
  const ignored = element("button", "observe-secondary-button observe-secondary-button--danger", "忽略此类型");
  ignored.type = "button";
  ignored.disabled = state.savingStatus;
  ignored.addEventListener("click", () => void setStatus("ignored"));
  actions.append(copy, spacer, acknowledged, ignored);
  if (state.statusError) actions.append(errorNotice(state.statusError));
}

function detailStats(item) {
  const stats = element("div", "observe-detail-stats");
  for (const [label, value] of [
    ["累计次数", number(item.count)],
    ["独立 session", number(item.sessions)],
    ["首次", shortTimestamp(item.first_ts)],
    ["最近", shortTimestamp(item.last_ts)],
  ]) {
    const stat = element("div", "observe-detail-stat");
    stat.append(element("span", "", label), element("strong", "", String(value)));
    stats.append(stat);
  }
  return stats;
}

function buildSkeleton() {
  const skeleton = element("div", "observe-skeleton");
  for (let index = 0; index < 5; index += 1) skeleton.append(element("div", "observe-skeleton-block"));
  return skeleton;
}

function metricTile(label, value, options) {
  const tile = element("section", `observe-metric observe-metric--${options.tone}`);
  tile.append(element("span", "observe-metric-label", label), element("strong", "observe-metric-value", value));
  if (options.sub) tile.append(element("span", "observe-metric-sub", options.sub));
  if (options.delta != null) tile.append(element("span", "observe-metric-delta", `${options.delta >= 0 ? "+" : ""}${options.delta.toFixed(0)}%`));
  tile.append(sparkline(options.values));
  return tile;
}

function chartCard(title, points, values, format, tone, empty = "暂无数据") {
  const card = element("section", `observe-chart-card observe-chart-card--${tone}`);
  card.append(element("h2", "observe-chart-title", title));
  const chart = element("div", "observe-chart");
  const finite = values.filter(Number.isFinite);
  if (finite.length === 0 || finite.every((value) => value === 0)) {
    chart.append(element("p", "observe-muted", empty));
  } else {
    const maximum = Math.max(...finite, 1);
    chart.setAttribute("role", "img");
    chart.setAttribute(
      "aria-label",
      `${title}：最新 ${format(number(finite.at(-1)))}，最高 ${format(maximum)}`,
    );
    values.forEach((value, index) => {
      const bar = element("span", "observe-chart-bar");
      bar.style.setProperty("--observe-chart-height", `${Math.max(5, (number(value) / maximum) * 100)}%`);
      const label = points[index]?.bucket || "";
      bar.title = `${bucketLabel(label)} · ${format(number(value))}`;
      chart.append(bar);
    });
  }
  card.append(chart);
  return card;
}

function sparkline(values) {
  const spark = element("span", "observe-sparkline");
  spark.setAttribute("aria-hidden", "true");
  const finite = values.filter(Number.isFinite);
  const maximum = Math.max(...finite, 1);
  for (const value of values.slice(-24)) {
    const bar = element("i", "");
    bar.style.setProperty("--observe-spark-height", `${Math.max(8, (number(value) / maximum) * 100)}%`);
    spark.append(bar);
  }
  return spark;
}

function errorNotice(message) {
  const notice = element("p", "observe-load-error", message);
  notice.setAttribute("role", "alert");
  return notice;
}

async function json(ctx, path, options = {}) {
  const response = await ctx.http.request(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body?.detail || body?.message || `HTTP ${response.status}`);
  return body;
}

function element(name, className = "", text = null) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

function number(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function compact(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

function percent(value) {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

function delta(values) {
  if (values.length < 2 || values.at(-2) === 0) return null;
  return ((values.at(-1) - values.at(-2)) / values.at(-2)) * 100;
}

function bucketLabel(value) {
  if (value.includes("T")) return `${value.slice(11, 13)}:00`;
  const [, month, day] = value.split("-");
  return month && day ? `${Number(month)}-${day}` : value;
}

function shortTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function ago(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 3_000) return "刚刚";
  const seconds = Math.floor(milliseconds / 1_000);
  if (seconds < 60) return `${seconds}s 前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m 前`;
  return `${Math.floor(minutes / 60)}h 前`;
}

function errorMessage(reason) {
  return reason instanceof Error ? reason.message : String(reason);
}
