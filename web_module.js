// dashboard_panel.tsx
import {
  useCallback,
  useEffect,
  useRef,
  useState
} from "react";
import { Grid, MetricTile, TrendChart, Sparkline, Chip } from "@akashic/dashboard-ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
var dashboardRequest = null;
async function api(path, init) {
  if (!dashboardRequest) throw new Error("Observe \u5DE5\u4F5C\u53F0\u9762\u677F\u672A\u6FC0\u6D3B");
  const response = await dashboardRequest(path, init);
  const body = await response.json();
  if (!response.ok) throw new Error(String(body.detail ?? body.message ?? `HTTP ${response.status}`));
  return body;
}
var RANGES = [
  { key: "24h", label: "24 \u5C0F\u65F6" },
  { key: "7d", label: "7 \u5929" },
  { key: "30d", label: "30 \u5929" },
  { key: "all", label: "\u5168\u90E8" }
];
var SOURCE_LABEL = {
  log: "\u4E3B\u52A8\u65E5\u5FD7",
  uncaught: "\u672A\u6355\u83B7\u5F02\u5E38",
  asyncio: "asyncio \u4EFB\u52A1",
  thread: "\u5B50\u7EBF\u7A0B"
};
var STATUS_META = {
  active: { label: "\u6D3B\u8DC3", tone: "warning" },
  acknowledged: { label: "\u5DF2\u786E\u8BA4", tone: "muted" },
  ignored: { label: "\u5DF2\u5FFD\u7565", tone: "success" }
};
var TONE_BG = {
  danger: "bg-danger",
  warning: "bg-warning",
  success: "bg-success",
  accent: "bg-accent",
  muted: "bg-subtle"
};
function _compact(value) {
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return String(Math.round(value));
}
function _pct(value) {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "\u2014";
}
function _bucketLabel(bucket) {
  if (bucket.includes("T")) return `${bucket.slice(11, 13)}:00`;
  const [, m, d] = bucket.split("-");
  return m && d ? `${Number(m)}-${d}` : bucket;
}
function _shortTs(value) {
  if (!value) return "\u2014";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value || "\u2014";
  return `${dt.getMonth() + 1}-${String(dt.getDate()).padStart(2, "0")} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
}
function _delta(values) {
  if (values.length < 2) return null;
  const last = values[values.length - 1];
  const prev = values[values.length - 2];
  if (!prev) return null;
  return (last - prev) / prev * 100;
}
function _ago(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "\u521A\u521A";
  const s = Math.floor(ms / 1e3);
  if (s < 3) return "\u521A\u521A";
  if (s < 60) return `${s}s \u524D`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m \u524D`;
  return `${Math.floor(m / 60)}h \u524D`;
}
function _severity(count, spiking) {
  if (spiking || count >= 20) return "danger";
  if (count >= 5) return "warning";
  return "muted";
}
function Card({ title, children, bodyClass, style }) {
  return /* @__PURE__ */ jsxs(
    "div",
    {
      className: "flex flex-col overflow-hidden border border-border bg-surface",
      style,
      children: [
        /* @__PURE__ */ jsx("div", { className: "flex items-center justify-between border-b border-border px-4 py-2.5", children: /* @__PURE__ */ jsx("h3", { className: "text-[12px] font-medium text-muted", children: title }) }),
        /* @__PURE__ */ jsx("div", { className: bodyClass ?? "p-4", children })
      ]
    }
  );
}
function ErrorDrill({
  portalRef,
  fallbackRef,
  range,
  onClose
}) {
  const drillRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [overview, setOverview] = useState(null);
  const [facet, setFacet] = useState("type");
  const [q, setQ] = useState("");
  const [sections, setSections] = useState([]);
  const [selFp, setSelFp] = useState(null);
  const [detail, setDetail] = useState(null);
  const [listError, setListError] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [savingStatus, setSavingStatus] = useState(false);
  const [tab, setTab] = useState("trace");
  const [variant, setVariant] = useState(0);
  const listReadRef = useRef(null);
  const statusReadRef = useRef(null);
  const loadList = useCallback(async () => {
    listReadRef.current?.abort();
    const controller = new AbortController();
    listReadRef.current = controller;
    setListError(null);
    try {
      const [ov, list] = await Promise.all([
        api(`/api/dashboard/observe/global_errors/overview?range=${range}`, { signal: controller.signal }),
        api(`/api/dashboard/observe/global_errors?range=${range}&facet=${facet}&q=${encodeURIComponent(q)}`, { signal: controller.signal })
      ]);
      if (controller.signal.aborted) return;
      setOverview(ov);
      setSections(list.sections ?? []);
      const flat = (list.sections ?? []).flatMap((s) => s.items);
      setSelFp((cur) => cur && flat.some((i) => i.fingerprint === cur) ? cur : flat[0]?.fingerprint ?? null);
    } catch (error) {
      if (!controller.signal.aborted) {
        setListError(error instanceof Error ? error.message : "\u9519\u8BEF\u5217\u8868\u8BFB\u53D6\u5931\u8D25");
      }
    } finally {
      if (listReadRef.current === controller) listReadRef.current = null;
    }
  }, [range, facet, q]);
  useEffect(() => {
    void loadList();
    return () => listReadRef.current?.abort();
  }, [loadList]);
  useEffect(() => {
    if (!selFp) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    setDetailError(null);
    void api(
      `/api/dashboard/observe/global_errors/${selFp}?range=${range}`,
      { signal: controller.signal }
    ).then((d) => {
      if (!controller.signal.aborted) {
        setDetail(d);
        setVariant(0);
        setTab("trace");
      }
    }, (error) => {
      if (!controller.signal.aborted) {
        setDetailError(error instanceof Error ? error.message : "\u9519\u8BEF\u8BE6\u60C5\u8BFB\u53D6\u5931\u8D25");
      }
    });
    return () => controller.abort();
  }, [selFp, range]);
  const close = useCallback(() => {
    onClose();
  }, [onClose]);
  useEffect(() => {
    closeButtonRef.current?.focus();
    return () => {
      statusReadRef.current?.abort();
      (portalRef.current ?? fallbackRef.current)?.focus();
    };
  }, [fallbackRef, portalRef]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") close();
      if (e.key !== "Tab") return;
      const focusable = Array.from(
        drillRef.current?.querySelectorAll(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
        ) ?? []
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  const setStatus = async (status) => {
    if (!detail || statusReadRef.current) return;
    const fingerprint = detail.fingerprint;
    const controller = new AbortController();
    statusReadRef.current = controller;
    setSavingStatus(true);
    setDetailError(null);
    try {
      await api(
        `/api/dashboard/observe/global_errors/${fingerprint}/status?value=${status}`,
        { method: "POST", signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      setDetail((d) => d?.fingerprint === fingerprint ? { ...d, status } : d);
    } catch (error) {
      if (!controller.signal.aborted) {
        setDetailError(error instanceof Error ? error.message : "\u9519\u8BEF\u72B6\u6001\u66F4\u65B0\u5931\u8D25");
      }
    } finally {
      if (statusReadRef.current === controller) {
        statusReadRef.current = null;
        if (!controller.signal.aborted) setSavingStatus(false);
      }
    }
  };
  const gotoSession = (key) => {
    window.dispatchEvent(new CustomEvent("akashic:goto-session", { detail: key }));
    close();
  };
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsx("div", { "aria-hidden": "true", className: "fixed inset-0 z-30 bg-black/55", onClick: close }),
    /* @__PURE__ */ jsxs(
      "div",
      {
        ref: drillRef,
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "observe-error-dialog-title",
        className: "fixed z-40 flex flex-col overflow-hidden rounded-md border border-border-strong bg-surface",
        style: {
          width: "min(1180px, 94vw)",
          height: "min(84vh, 760px)",
          left: "50%",
          top: "50%",
          marginLeft: "calc(min(1180px, 94vw) / -2)",
          marginTop: "calc(min(84vh, 760px) / -2)"
        },
        children: [
          /* @__PURE__ */ jsxs("div", { className: "flex flex-shrink-0 items-center gap-4 border-b border-border px-5 py-4", children: [
            /* @__PURE__ */ jsx(
              "button",
              {
                ref: closeButtonRef,
                type: "button",
                onClick: close,
                className: "grid h-10 w-10 place-items-center rounded-md border border-border-strong bg-surface-2 text-[18px] text-muted transition-colors hover:text-fg",
                "aria-label": "\u5173\u95ED\u9519\u8BEF\u5206\u6790",
                title: "\u8FD4\u56DE (Esc)",
                children: "\u2039"
              }
            ),
            /* @__PURE__ */ jsx("span", { className: "font-mono text-[26px] font-semibold tabular-nums text-danger", children: overview?.total ?? "\u2014" }),
            /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
              /* @__PURE__ */ jsxs("div", { id: "observe-error-dialog-title", className: "text-sm font-semibold", children: [
                "\u9519\u8BEF \xB7 ",
                RANGES.find((r) => r.key === range)?.label ?? range
              ] }),
              /* @__PURE__ */ jsxs("div", { className: "mt-0.5 flex items-center gap-3 text-[11px] text-muted", children: [
                /* @__PURE__ */ jsxs("span", { children: [
                  overview?.types ?? 0,
                  " \u4E2A\u7C7B\u578B"
                ] }),
                (overview?.new_types ?? 0) > 0 && /* @__PURE__ */ jsxs("span", { children: [
                  overview?.new_types,
                  " \u4E2A\u65B0\u7C7B\u578B"
                ] }),
                (overview?.spiking_types ?? 0) > 0 && /* @__PURE__ */ jsxs("span", { className: "font-semibold text-danger", children: [
                  overview?.spiking_types,
                  " \u4E2A\u6B63\u5728\u7206\u53D1"
                ] })
              ] })
            ] })
          ] }),
          /* @__PURE__ */ jsxs("div", { className: "flex flex-shrink-0 items-center gap-3 border-b border-border px-4 py-2.5", children: [
            /* @__PURE__ */ jsx("div", { className: "flex gap-1 rounded-md border border-border bg-bg p-0.5", children: [
              { k: "type", l: "\u6309\u7C7B\u578B" },
              { k: "source", l: "\u6309\u6765\u6E90" },
              { k: "channel", l: "\u6309\u901A\u9053" }
            ].map((f) => /* @__PURE__ */ jsx(
              "button",
              {
                type: "button",
                onClick: () => setFacet(f.k),
                className: `rounded-[4px] px-2.5 py-1 text-[11px] transition-colors ${facet === f.k ? "bg-surface-3 text-fg" : "text-muted hover:text-fg"}`,
                children: f.l
              },
              f.k
            )) }),
            /* @__PURE__ */ jsx(
              "input",
              {
                value: q,
                onChange: (e) => setQ(e.target.value),
                "aria-label": "\u641C\u7D22\u9519\u8BEF",
                placeholder: "\u6309\u6D88\u606F / \u6A21\u5757\u8FC7\u6EE4\u2026",
                className: "w-[280px] rounded-md border border-border bg-bg px-3 py-1.5 text-[11.5px] text-fg outline-none focus:border-accent-deep"
              }
            )
          ] }),
          /* @__PURE__ */ jsxs("div", { className: "grid min-h-0 flex-1 grid-cols-[340px_1fr]", children: [
            /* @__PURE__ */ jsxs("div", { className: "overflow-auto border-r border-border p-1.5", children: [
              listError && /* @__PURE__ */ jsx("div", { className: "p-4 text-[12px] text-danger", role: "alert", children: listError }),
              sections.map((section) => /* @__PURE__ */ jsxs("div", { children: [
                section.label && /* @__PURE__ */ jsxs("div", { className: "flex items-center justify-between px-2.5 pb-1 pt-3 text-[11px] font-medium text-muted", children: [
                  /* @__PURE__ */ jsx("span", { children: section.label }),
                  /* @__PURE__ */ jsxs("span", { children: [
                    section.count,
                    " \u6B21"
                  ] })
                ] }),
                section.items.map((g) => /* @__PURE__ */ jsx(ErrorRow, { g, active: g.fingerprint === selFp, onClick: () => setSelFp(g.fingerprint) }, g.fingerprint))
              ] }, section.key)),
              !listError && sections.length === 0 && /* @__PURE__ */ jsx("div", { className: "p-6 text-[12.5px] text-muted", children: "\u6240\u9009\u533A\u95F4\u5185\u6CA1\u6709\u9519\u8BEF\u3002" })
            ] }),
            detailError ? /* @__PURE__ */ jsx("div", { className: "grid place-items-center p-6 text-[13px] text-danger", role: "alert", children: detailError }) : detail ? /* @__PURE__ */ jsx(
              ErrorDetail,
              {
                detail,
                tab,
                setTab,
                variant,
                setVariant,
                savingStatus,
                onStatus: setStatus,
                onGoto: gotoSession
              }
            ) : /* @__PURE__ */ jsx("div", { className: "grid place-items-center text-[13px] text-muted", children: "\u9009\u62E9\u5DE6\u4FA7\u4E00\u4E2A\u9519\u8BEF\u67E5\u770B\u73B0\u573A" })
          ] })
        ]
      }
    )
  ] });
}
function ErrorRow({ g, active, onClick }) {
  const tone = _severity(g.count, g.is_spiking);
  const spark = g.spark ?? [];
  return /* @__PURE__ */ jsxs(
    "button",
    {
      type: "button",
      onClick,
      className: `grid w-full grid-cols-[9px_1fr_auto] items-center gap-2.5 border-b border-border px-3 py-2.5 text-left transition-colors duration-150 ${active ? "bg-accent-soft" : "hover:bg-surface-2"}`,
      children: [
        /* @__PURE__ */ jsx("span", { className: "relative flex h-2 w-2", "aria-hidden": "true", children: /* @__PURE__ */ jsx("span", { className: `relative inline-flex h-2 w-2 rounded-full ${TONE_BG[tone]}` }) }),
        /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
          /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-1.5 font-mono text-[12.5px]", children: [
            /* @__PURE__ */ jsx("span", { className: "truncate", children: g.error_type }),
            g.is_new && /* @__PURE__ */ jsx("span", { className: "text-[9px] font-semibold text-accent", children: "\u65B0" }),
            g.is_spiking && /* @__PURE__ */ jsx("span", { className: "text-[9px] font-semibold text-danger", children: "\u7206\u53D1" })
          ] }),
          /* @__PURE__ */ jsx("div", { className: "mt-0.5 truncate font-mono text-[10px] text-subtle", children: g.logger_name }),
          /* @__PURE__ */ jsxs("div", { className: "mt-1 flex gap-2.5 text-[10px] text-muted", children: [
            /* @__PURE__ */ jsxs("span", { children: [
              /* @__PURE__ */ jsx("b", { className: "font-semibold text-fg", children: g.count }),
              " \u6B21"
            ] }),
            /* @__PURE__ */ jsxs("span", { children: [
              /* @__PURE__ */ jsx("b", { className: "font-semibold text-fg", children: g.sessions }),
              " session"
            ] })
          ] })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "flex flex-col items-end gap-1.5", children: [
          /* @__PURE__ */ jsx("div", { className: "h-[22px] w-[62px]", children: spark.length > 1 && /* @__PURE__ */ jsx(Sparkline, { data: spark, tone, height: 22 }) }),
          /* @__PURE__ */ jsx("span", { className: "font-mono text-[10px] text-subtle", children: _shortTs(g.last_ts) })
        ] })
      ]
    }
  );
}
function ErrorDetail({
  detail,
  tab,
  setTab,
  variant,
  setVariant,
  savingStatus,
  onStatus,
  onGoto
}) {
  const status = STATUS_META[detail.status] ?? STATUS_META.active;
  const tone = _severity(detail.count, false);
  const activeVariant = detail.variants[variant] ?? detail.variants[0];
  return /* @__PURE__ */ jsxs("div", { className: "flex min-h-0 flex-col", children: [
    /* @__PURE__ */ jsxs("div", { className: "border-b border-border px-5 py-4", children: [
      /* @__PURE__ */ jsx("div", { className: "font-mono text-[19px] font-semibold", children: detail.error_type }),
      /* @__PURE__ */ jsx("div", { className: "mt-1.5 font-mono text-[12px] leading-relaxed text-danger", children: detail.message }),
      /* @__PURE__ */ jsxs("div", { className: "mt-3 flex flex-wrap gap-1.5", children: [
        /* @__PURE__ */ jsx(Chip, { children: detail.logger_name }),
        /* @__PURE__ */ jsxs(Chip, { children: [
          "\u6765\u6E90 \xB7 ",
          SOURCE_LABEL[detail.source] ?? detail.source
        ] }),
        /* @__PURE__ */ jsx(Chip, { children: detail.channel }),
        /* @__PURE__ */ jsx(Chip, { tone: "danger", children: detail.level }),
        /* @__PURE__ */ jsx(Chip, { tone: status.tone, children: status.label })
      ] })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "grid grid-cols-4 gap-px border-b border-border bg-border", children: [
      /* @__PURE__ */ jsx(Blast, { label: "\u7D2F\u8BA1\u6B21\u6570", value: String(detail.count) }),
      /* @__PURE__ */ jsx(Blast, { label: "\u72EC\u7ACB session", value: String(detail.sessions) }),
      /* @__PURE__ */ jsx(Blast, { label: "\u9996\u6B21", value: _shortTs(detail.first_ts), small: true }),
      /* @__PURE__ */ jsx(Blast, { label: "\u6700\u8FD1", value: _shortTs(detail.last_ts), small: true })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "flex gap-1 border-b border-border px-5 pt-3", children: [
      /* @__PURE__ */ jsx(TabBtn, { active: tab === "trend", onClick: () => setTab("trend"), children: "\u8D8B\u52BF" }),
      /* @__PURE__ */ jsxs(TabBtn, { active: tab === "trace", onClick: () => setTab("trace"), children: [
        "Traceback",
        detail.variants.length > 1 ? ` \xB7 ${detail.variants.length} \u53D8\u4F53` : ""
      ] }),
      /* @__PURE__ */ jsxs(TabBtn, { active: tab === "occ", onClick: () => setTab("occ"), children: [
        "\u73B0\u573A \xB7 ",
        detail.occurrences.length
      ] })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "min-h-0 flex-1 overflow-auto px-5 py-4", children: [
      tab === "trend" && /* @__PURE__ */ jsx(
        TrendChart,
        {
          data: detail.trend.map((p) => ({ label: _bucketLabel(p.bucket), value: p.count })),
          kind: "bar",
          tone,
          valueFmt: (n) => String(n),
          empty: "\u533A\u95F4\u5185\u65E0\u53D1\u4F5C"
        }
      ),
      tab === "trace" && /* @__PURE__ */ jsxs("div", { children: [
        detail.variants.length > 1 && /* @__PURE__ */ jsx("div", { className: "mb-3 flex gap-2", children: detail.variants.map((v, i) => /* @__PURE__ */ jsxs(
          "button",
          {
            type: "button",
            onClick: () => setVariant(i),
            className: `rounded border px-2.5 py-1.5 text-left text-[10.5px] ${i === variant ? "border-accent-deep bg-accent-soft text-fg" : "border-border bg-bg text-muted"}`,
            children: [
              /* @__PURE__ */ jsx("b", { className: "text-fg", children: v.count }),
              " \u6B21 \xB7 \u53D8\u4F53 ",
              i + 1
            ]
          },
          v.fingerprint
        )) }),
        /* @__PURE__ */ jsx("pre", { className: "m-0 max-h-[280px] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-muted", children: activeVariant?.traceback_text || detail.traceback_text })
      ] }),
      tab === "occ" && /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2", children: [
        detail.occurrences.length === 0 && /* @__PURE__ */ jsx("div", { className: "text-[12px] text-muted", children: "\u65E0\u53EF\u5173\u8054\u7684 session \u73B0\u573A\u3002" }),
        detail.occurrences.map((o) => /* @__PURE__ */ jsxs("div", { className: "grid grid-cols-[auto_1fr_auto] items-center gap-3.5 border-b border-border bg-bg px-3.5 py-2.5", children: [
          /* @__PURE__ */ jsx("span", { className: "font-mono text-[11px] text-accent", children: _shortTs(o.ts) }),
          /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
            /* @__PURE__ */ jsx("div", { className: "truncate text-[12px]", children: o.user_preview || "\uFF08\u65E0\u7528\u6237\u6D88\u606F\uFF09" }),
            /* @__PURE__ */ jsxs("div", { className: "mt-0.5 font-mono text-[10px] text-subtle", children: [
              "session ",
              o.session_key
            ] })
          ] }),
          /* @__PURE__ */ jsx(
            "button",
            {
              type: "button",
              onClick: () => onGoto(o.session_key),
              className: "whitespace-nowrap rounded border border-accent-deep bg-accent-soft px-2.5 py-1.5 text-[10.5px] text-accent-ink",
              children: "\u67E5\u770B\u5BF9\u8BDD"
            }
          )
        ] }, o.session_key))
      ] })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "flex flex-shrink-0 gap-2 border-t border-border px-5 py-3", children: [
      /* @__PURE__ */ jsx(
        "button",
        {
          type: "button",
          onClick: () => detail.occurrences[0] && onGoto(detail.occurrences[0].session_key),
          disabled: detail.occurrences.length === 0,
          className: "rounded border border-accent-deep bg-accent-soft px-3 py-2 text-[11px] text-accent-ink transition-colors disabled:opacity-40",
          children: "\u67E5\u770B\u6700\u8FD1\u5BF9\u8BDD"
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          type: "button",
          onClick: () => void navigator.clipboard?.writeText(detail.traceback_text),
          className: "rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:text-fg",
          children: "\u590D\u5236 Traceback"
        }
      ),
      /* @__PURE__ */ jsx("div", { className: "flex-1" }),
      /* @__PURE__ */ jsx("button", { type: "button", disabled: savingStatus, onClick: () => onStatus("acknowledged"), className: "rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:text-fg disabled:opacity-40", children: "\u6807\u8BB0\u5DF2\u786E\u8BA4" }),
      /* @__PURE__ */ jsx("button", { type: "button", disabled: savingStatus, onClick: () => onStatus("ignored"), className: "rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger disabled:opacity-40", children: "\u5FFD\u7565\u6B64\u7C7B\u578B" })
    ] })
  ] });
}
function Blast({ label, value, small }) {
  return /* @__PURE__ */ jsxs("div", { className: "bg-surface px-4 py-3", children: [
    /* @__PURE__ */ jsx("div", { className: "text-[10px] text-subtle", children: label }),
    /* @__PURE__ */ jsx("div", { className: `mt-1.5 font-mono font-semibold tabular-nums ${small ? "text-[12.5px]" : "text-[18px]"}`, children: value })
  ] });
}
function TabBtn({ active, onClick, children }) {
  return /* @__PURE__ */ jsx(
    "button",
    {
      type: "button",
      onClick,
      className: `-mb-px border-b-2 px-3 py-2 text-[11.5px] transition-colors ${active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg"}`,
      children
    }
  );
}
function SkelBlock({ className }) {
  return /* @__PURE__ */ jsx("div", { className: `relative overflow-hidden rounded border border-border bg-surface-2 ${className}` });
}
function ObserveSkeleton() {
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-4 p-6", children: [
    /* @__PURE__ */ jsxs("div", { className: "flex items-end justify-between", children: [
      /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2", children: [
        /* @__PURE__ */ jsx(SkelBlock, { className: "h-7 w-48" }),
        /* @__PURE__ */ jsx(SkelBlock, { className: "h-3 w-64 rounded" })
      ] }),
      /* @__PURE__ */ jsx(SkelBlock, { className: "h-9 w-56" })
    ] }),
    /* @__PURE__ */ jsx("div", { className: "grid grid-cols-4 gap-4", children: [0, 1, 2, 3].map((i) => /* @__PURE__ */ jsx(SkelBlock, { className: "h-[132px]" }, i)) }),
    /* @__PURE__ */ jsx("div", { className: "grid grid-cols-2 gap-4", children: [0, 1, 2, 3].map((i) => /* @__PURE__ */ jsx(SkelBlock, { className: "h-[218px]" }, i)) })
  ] });
}
function ObserveMain(_props) {
  const [range, setRange] = useState("24h");
  const [overview, setOverview] = useState(null);
  const [points, setPoints] = useState([]);
  const [gErr, setGErr] = useState(null);
  const [drillOpen, setDrillOpen] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(0);
  const [nowTs, setNowTs] = useState(() => Date.now());
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const portalRef = useRef(null);
  const refreshRef = useRef(null);
  const overviewRef = useRef(null);
  const mainReadRef = useRef(null);
  const load = useCallback(async () => {
    mainReadRef.current?.abort();
    const controller = new AbortController();
    mainReadRef.current = controller;
    setRefreshing(true);
    setError(null);
    try {
      const [ov, series, ge] = await Promise.all([
        api(`/api/dashboard/observe/overview?range=${range}`, { signal: controller.signal }),
        api(`/api/dashboard/observe/timeseries?range=${range}`, { signal: controller.signal }),
        api(`/api/dashboard/observe/global_errors/overview?range=${range}`, { signal: controller.signal })
      ]);
      if (controller.signal.aborted) return;
      setOverview(ov);
      setPoints(series.points ?? []);
      setGErr(ge);
      setUpdatedAt(Date.now());
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason.message : "\u76D1\u6D4B\u6570\u636E\u8BFB\u53D6\u5931\u8D25");
      }
    } finally {
      if (mainReadRef.current === controller) {
        mainReadRef.current = null;
        if (!controller.signal.aborted) setRefreshing(false);
      }
    }
  }, [range]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 15e3);
    return () => {
      window.clearInterval(id);
      mainReadRef.current?.abort();
    };
  }, [load]);
  useEffect(() => {
    if (!refreshing || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const animation = refreshRef.current?.animate(
      [{ transform: "rotate(0deg)" }, { transform: "rotate(360deg)" }],
      { duration: 1e3, iterations: Infinity }
    );
    return () => animation?.cancel();
  }, [refreshing]);
  useEffect(() => {
    const id = window.setInterval(() => setNowTs(Date.now()), 1e3);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    if (overviewRef.current) overviewRef.current.inert = drillOpen;
  }, [drillOpen]);
  if (!overview) {
    return error ? /* @__PURE__ */ jsx("div", { className: "grid min-h-full place-items-center p-6 text-danger", role: "alert", children: error }) : /* @__PURE__ */ jsx(ObserveSkeleton, {});
  }
  const turnSeries = points.map((p) => p.turns);
  const errorSeries = points.map((p) => p.errors);
  const tokenSeries = points.map((p) => p.input_tokens);
  const passiveHitSeries = points.map((p) => (p.passive_cache_hit_rate ?? 0) * 100);
  const proactiveHitSeries = points.map((p) => (p.proactive_cache_hit_rate ?? 0) * 100);
  const iterSeries = points.map((p) => p.avg_iteration ?? 0);
  const labelled = (vals) => points.map((p, i) => ({ label: _bucketLabel(p.bucket), value: vals[i] }));
  const gErrTotal = gErr?.total ?? overview.errors;
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsxs(
      "div",
      {
        ref: overviewRef,
        "aria-hidden": drillOpen || void 0,
        className: "flex flex-col gap-5 p-6 transition-opacity duration-150",
        style: drillOpen ? { opacity: 0.35, pointerEvents: "none" } : void 0,
        children: [
          /* @__PURE__ */ jsxs("div", { className: "flex items-end justify-between", children: [
            /* @__PURE__ */ jsxs("div", { children: [
              /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-2.5", children: [
                /* @__PURE__ */ jsx("span", { className: "detail-title", children: "Observe \xB7 \u76D1\u6D4B" }),
                /* @__PURE__ */ jsx("span", { className: "text-[11px] font-medium text-success", children: "\u5B9E\u65F6\u66F4\u65B0" })
              ] }),
              /* @__PURE__ */ jsxs("div", { className: "detail-subtext", children: [
                "Agent \u4E3B\u5FAA\u73AF\u9065\u6D4B \xB7 Token / \u8FED\u4EE3 / \u9519\u8BEF",
                /* @__PURE__ */ jsxs("span", { className: "ml-2 font-mono text-[11px] text-subtle", children: [
                  "\u66F4\u65B0\u4E8E ",
                  _ago(nowTs - updatedAt)
                ] })
              ] })
            ] }),
            /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-2", children: [
              /* @__PURE__ */ jsx(
                "button",
                {
                  ref: refreshRef,
                  type: "button",
                  onClick: () => void load(),
                  className: "grid h-10 w-10 place-items-center rounded-md border border-border bg-surface-2 text-muted transition-colors hover:border-border-strong hover:text-fg",
                  "aria-label": "\u5237\u65B0\u76D1\u6D4B\u6570\u636E",
                  title: "\u5237\u65B0",
                  children: "\u21BB"
                }
              ),
              /* @__PURE__ */ jsx("div", { className: "flex gap-1 rounded-md border border-border bg-surface-2 p-1", children: RANGES.map((r) => /* @__PURE__ */ jsx(
                "button",
                {
                  type: "button",
                  onClick: () => setRange(r.key),
                  className: `min-h-10 rounded-[4px] px-2.5 py-1 text-[11px] transition-colors ${range === r.key ? "bg-accent text-accent-ink" : "text-muted hover:bg-surface-3 hover:text-fg"}`,
                  "aria-pressed": range === r.key,
                  children: r.label
                },
                r.key
              )) })
            ] })
          ] }),
          error && /* @__PURE__ */ jsx("div", { className: "border border-danger/40 bg-danger/10 px-4 py-3 text-[12px] text-danger", role: "alert", children: error }),
          /* @__PURE__ */ jsxs(
            "section",
            {
              className: `flex min-h-16 items-center justify-between gap-4 border px-4 py-3 ${gErrTotal > 0 ? "border-danger/40 bg-danger/10" : "border-success/35 bg-success/10"}`,
              "aria-label": "\u8FD0\u884C\u72B6\u6001",
              children: [
                /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
                  /* @__PURE__ */ jsx("div", { className: `text-[13px] font-semibold ${gErrTotal > 0 ? "text-danger" : "text-success"}`, children: gErrTotal > 0 ? `${gErrTotal} \u6761\u9519\u8BEF\u9700\u8981\u67E5\u770B` : "\u5F53\u524D\u533A\u95F4\u6CA1\u6709\u91C7\u96C6\u5230\u9519\u8BEF" }),
                  /* @__PURE__ */ jsx("p", { className: "mt-1 text-[11.5px] text-muted", children: gErrTotal > 0 ? `${gErr?.types ?? 0} \u4E2A\u9519\u8BEF\u7C7B\u578B\uFF0C\u5148\u67E5\u770B\u7206\u53D1\u548C\u65B0\u51FA\u73B0\u7684\u7C7B\u578B\u3002` : "\u4E3B\u5FAA\u73AF\u9065\u6D4B\u6301\u7EED\u66F4\u65B0\uFF0C\u7F13\u5B58\u4E0E\u8FED\u4EE3\u6307\u6807\u89C1\u4E0B\u65B9\u3002" })
                ] }),
                gErrTotal > 0 && /* @__PURE__ */ jsx(
                  "button",
                  {
                    type: "button",
                    ref: portalRef,
                    onClick: () => setDrillOpen(true),
                    className: "min-h-10 flex-shrink-0 rounded-md border border-danger/40 bg-surface px-3 text-[12px] font-semibold text-danger hover:bg-danger/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                    children: "\u67E5\u770B\u9519\u8BEF\u5206\u6790"
                  }
                )
              ]
            }
          ),
          /* @__PURE__ */ jsxs(Grid, { columns: 3, children: [
            /* @__PURE__ */ jsx("div", { children: /* @__PURE__ */ jsx(MetricTile, { label: "\u5BF9\u8BDD\u8F6E\u6570", value: _compact(overview.turns), delta: _delta(turnSeries), sub: overview.last_ts ? `\u6700\u8FD1 ${_shortTs(overview.last_ts)}` : "\u65E0\u8BB0\u5F55", tone: "accent", spark: turnSeries }) }),
            /* @__PURE__ */ jsx("div", { children: /* @__PURE__ */ jsx(MetricTile, { label: "\u88AB\u52A8 KV \u547D\u4E2D\u7387", value: _pct(overview.passive_cache_hit_rate), sub: `\u4E3B\u52A8 ${_pct(overview.proactive_cache_hit_rate)}`, tone: "success", spark: passiveHitSeries }) }),
            /* @__PURE__ */ jsx("div", { children: /* @__PURE__ */ jsx(MetricTile, { label: "\u5E73\u5747\u8FED\u4EE3", value: overview.avg_iteration != null ? overview.avg_iteration.toFixed(1) : "\u2014", unit: `\u5CF0 ${overview.max_iteration}`, sub: "\u6BCF\u8F6E LLM \u8C03\u7528\u6B21\u6570", tone: "warning", spark: iterSeries }) })
          ] }),
          /* @__PURE__ */ jsxs(Grid, { columns: 2, children: [
            /* @__PURE__ */ jsx(Card, { title: "\u8F93\u5165 Token \u8D8B\u52BF", children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(tokenSeries), kind: "area", tone: "accent", valueFmt: _compact }) }),
            /* @__PURE__ */ jsx(Card, { title: "\u5E73\u5747\u8FED\u4EE3\u8D8B\u52BF", children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(iterSeries), kind: "area", tone: "warning", valueFmt: (n) => n.toFixed(1) }) })
          ] }),
          /* @__PURE__ */ jsxs("details", { className: "border-t border-border pt-1", children: [
            /* @__PURE__ */ jsx("summary", { className: "min-h-11 cursor-pointer py-3 text-[12px] font-semibold text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent", children: "\u67E5\u770B\u7F13\u5B58\u547D\u4E2D\u4E0E\u9519\u8BEF\u8D8B\u52BF" }),
            /* @__PURE__ */ jsxs(Grid, { columns: 2, children: [
              /* @__PURE__ */ jsx(Card, { title: "\u5168\u5C40\u88AB\u52A8\u94FE\u8DEF\u547D\u4E2D\u7387\u8D8B\u52BF", children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(passiveHitSeries), kind: "area", tone: "success", valueFmt: (n) => `${n.toFixed(0)}%` }) }),
              /* @__PURE__ */ jsx(Card, { title: "\u5168\u5C40\u4E3B\u52A8\u94FE\u8DEF\u547D\u4E2D\u7387\u8D8B\u52BF", children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(proactiveHitSeries), kind: "area", tone: "accent", valueFmt: (n) => `${n.toFixed(0)}%` }) }),
              /* @__PURE__ */ jsx(Card, { title: "\u9519\u8BEF\u8D8B\u52BF", children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(errorSeries), kind: "bar", tone: "danger", valueFmt: (n) => String(n), empty: "\u6240\u9009\u533A\u95F4\u5185\u6CA1\u6709\u9519\u8BEF" }) })
            ] })
          ] })
        ]
      }
    ),
    drillOpen && /* @__PURE__ */ jsx(ErrorDrill, { portalRef, fallbackRef: refreshRef, range, onClose: () => setDrillOpen(false) })
  ] });
}
var panel = {
  id: "observe",
  label: "\u8FD0\u884C\u76D1\u6D4B",
  viewLabel: "\u8FD0\u884C\u76D1\u6D4B",
  order: 60,
  layout: "workbench",
  pageSize: 30,
  rowKey: "id",
  countTitle(total) {
    return `${total} \u8F6E\u9065\u6D4B`;
  },
  columns: [
    { key: "session_key", label: "\u4F1A\u8BDD", width: 120, cellClass: "mono cell-session", rawTitle: true },
    { key: "ts", label: "\u65F6\u95F4", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "error", label: "\u9519\u8BEF", flex: true, cellClass: "content-preview" }
  ],
  async getCount({ signal }) {
    try {
      const ov = await api("/api/dashboard/observe/overview?range=all", { signal });
      return ov.turns || 0;
    } catch (error) {
      if (signal.aborted) throw error;
      return null;
    }
  },
  async fetchPage({ page, pageSize, signal }) {
    const data = await api(
      `/api/dashboard/observe/errors?range=all&page=${page}&page_size=${pageSize}`,
      { signal }
    );
    return { items: data.items || [], total: data.total || 0 };
  },
  Main: ObserveMain
};
function activate(ctx) {
  dashboardRequest = ctx.http.request;
  const release = ctx.ui.inject("workbench.panels.v2", (mount) => mount.register(panel));
  return () => {
    release();
    dashboardRequest = null;
  };
}
export {
  activate
};
