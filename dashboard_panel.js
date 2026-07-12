// ../akashic-plugin/observe/dashboard_panel.tsx
import {
  useCallback,
  useEffect,
  useRef,
  useState
} from "react";
import { Grid, MetricTile, TrendChart, Sparkline, Chip, api } from "@akashic/dashboard-ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
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
  active: { label: "\u25CF \u6D3B\u8DC3", tone: "warning" },
  acknowledged: { label: "\u25CC \u5DF2\u786E\u8BA4", tone: "muted" },
  ignored: { label: "\u2713 \u5DF2\u5FFD\u7565", tone: "success" }
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
      className: "flex animate-fade-up flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lift-sm transition-[box-shadow,border-color] duration-200 hover:border-border-strong hover:shadow-lift-md",
      style,
      children: [
        /* @__PURE__ */ jsx("div", { className: "flex items-center justify-between border-b border-border px-4 py-2.5", children: /* @__PURE__ */ jsx("h3", { className: "font-mono text-[10px] uppercase tracking-[0.2em] text-muted", children: title }) }),
        /* @__PURE__ */ jsx("div", { className: bodyClass ?? "p-4", children })
      ]
    }
  );
}
function ErrorDrill({
  portalRef,
  range,
  onClose
}) {
  const drillRef = useRef(null);
  const [overview, setOverview] = useState(null);
  const [facet, setFacet] = useState("type");
  const [q, setQ] = useState("");
  const [sections, setSections] = useState([]);
  const [selFp, setSelFp] = useState(null);
  const [detail, setDetail] = useState(null);
  const [tab, setTab] = useState("trace");
  const [variant, setVariant] = useState(0);
  const loadList = useCallback(async () => {
    const [ov, list] = await Promise.all([
      api(`/api/dashboard/observe/global_errors/overview?range=${range}`),
      api(`/api/dashboard/observe/global_errors?range=${range}&facet=${facet}&q=${encodeURIComponent(q)}`)
    ]);
    setOverview(ov);
    setSections(list.sections ?? []);
    const flat = (list.sections ?? []).flatMap((s) => s.items);
    setSelFp((cur) => cur && flat.some((i) => i.fingerprint === cur) ? cur : flat[0]?.fingerprint ?? null);
  }, [range, facet, q]);
  useEffect(() => {
    void loadList();
  }, [loadList]);
  useEffect(() => {
    if (!selFp) {
      setDetail(null);
      return;
    }
    let alive = true;
    void (async () => {
      const d = await api(`/api/dashboard/observe/global_errors/${selFp}?range=${range}`);
      if (alive) {
        setDetail(d);
        setVariant(0);
        setTab("trace");
      }
    })();
    return () => {
      alive = false;
    };
  }, [selFp, range]);
  useEffect(() => {
    const drill = drillRef.current;
    const portal = portalRef.current;
    if (!drill || !portal) return;
    const tr = portal.getBoundingClientRect();
    const cr = drill.getBoundingClientRect();
    drill.style.transition = "none";
    drill.style.transformOrigin = "top left";
    drill.style.transform = `translate(${tr.left - cr.left}px, ${tr.top - cr.top}px) scale(${tr.width / cr.width}, ${tr.height / cr.height})`;
    drill.style.opacity = "0";
    void drill.getBoundingClientRect();
    requestAnimationFrame(() => {
      drill.style.transition = "transform .44s cubic-bezier(.2,.85,.25,1), opacity .26s ease";
      drill.style.transform = "";
      drill.style.opacity = "";
    });
  }, [portalRef]);
  const close = useCallback(() => {
    const drill = drillRef.current;
    const portal = portalRef.current;
    if (drill && portal) {
      const tr = portal.getBoundingClientRect();
      const cr = drill.getBoundingClientRect();
      drill.style.transition = "transform .4s cubic-bezier(.4,0,.6,1), opacity .3s ease";
      drill.style.transformOrigin = "top left";
      drill.style.transform = `translate(${tr.left - cr.left}px, ${tr.top - cr.top}px) scale(${tr.width / cr.width}, ${tr.height / cr.height})`;
      drill.style.opacity = "0";
      window.setTimeout(onClose, 360);
    } else {
      onClose();
    }
  }, [onClose, portalRef]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  const setStatus = async (status) => {
    if (!detail) return;
    await api(`/api/dashboard/observe/global_errors/${detail.fingerprint}/status?value=${status}`, { method: "POST" });
    await loadList();
    setDetail((d) => d ? { ...d, status } : d);
  };
  const gotoSession = (key) => {
    window.dispatchEvent(new CustomEvent("akashic:goto-session", { detail: key }));
    close();
  };
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsx("div", { className: "fixed inset-0 z-30 bg-black/55 backdrop-blur-[2px]", onClick: close }),
    /* @__PURE__ */ jsxs(
      "div",
      {
        ref: drillRef,
        className: "fixed z-40 flex flex-col overflow-hidden rounded-2xl border border-border-strong bg-surface shadow-lift-md",
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
                type: "button",
                onClick: close,
                className: "grid h-8 w-8 place-items-center rounded-md border border-border-strong bg-surface-2 text-[18px] text-muted transition-colors hover:text-fg",
                title: "\u8FD4\u56DE (Esc)",
                children: "\u2039"
              }
            ),
            /* @__PURE__ */ jsx("span", { className: "font-mono text-[26px] font-semibold tabular-nums text-danger", children: overview?.total ?? "\u2014" }),
            /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
              /* @__PURE__ */ jsxs("div", { className: "text-sm font-semibold", children: [
                "\u9519\u8BEF \xB7 ",
                RANGES.find((r) => r.key === range)?.label ?? range
              ] }),
              /* @__PURE__ */ jsxs("div", { className: "mt-0.5 flex items-center gap-3 font-mono text-[11px] text-muted", children: [
                /* @__PURE__ */ jsxs("span", { children: [
                  overview?.types ?? 0,
                  " \u4E2A\u7C7B\u578B"
                ] }),
                (overview?.new_types ?? 0) > 0 && /* @__PURE__ */ jsxs("span", { className: "rounded border border-accent-deep bg-accent-soft px-1.5 py-0.5 text-accent", children: [
                  "\u{1F195} ",
                  overview?.new_types,
                  " \u65B0\u7C7B\u578B"
                ] }),
                (overview?.spiking_types ?? 0) > 0 && /* @__PURE__ */ jsxs("span", { className: "rounded border border-danger/30 bg-danger/10 px-1.5 py-0.5 text-danger", children: [
                  "\u26A1 ",
                  overview?.spiking_types,
                  " \u7206\u53D1"
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
                className: `rounded-[4px] px-2.5 py-1 font-mono text-[11px] transition-colors ${facet === f.k ? "bg-surface-3 text-fg shadow-[inset_0_0_0_1px_var(--color-border-strong)]" : "text-muted hover:text-fg"}`,
                children: f.l
              },
              f.k
            )) }),
            /* @__PURE__ */ jsx(
              "input",
              {
                value: q,
                onChange: (e) => setQ(e.target.value),
                placeholder: "\u6309\u6D88\u606F / \u6A21\u5757\u8FC7\u6EE4\u2026",
                className: "w-[280px] rounded-md border border-border bg-bg px-3 py-1.5 font-mono text-[11.5px] text-fg outline-none focus:border-accent-deep"
              }
            )
          ] }),
          /* @__PURE__ */ jsxs("div", { className: "grid min-h-0 flex-1 grid-cols-[340px_1fr]", children: [
            /* @__PURE__ */ jsxs("div", { className: "overflow-auto border-r border-border p-1.5", children: [
              sections.map((section) => /* @__PURE__ */ jsxs("div", { children: [
                section.label && /* @__PURE__ */ jsxs("div", { className: "flex items-center justify-between px-2.5 pb-1 pt-3 font-mono text-[9.5px] uppercase tracking-[0.14em] text-subtle", children: [
                  /* @__PURE__ */ jsx("span", { children: section.label }),
                  /* @__PURE__ */ jsxs("span", { children: [
                    section.count,
                    " \u6B21"
                  ] })
                ] }),
                section.items.map((g) => /* @__PURE__ */ jsx(ErrorRow, { g, active: g.fingerprint === selFp, onClick: () => setSelFp(g.fingerprint) }, g.fingerprint))
              ] }, section.key)),
              sections.length === 0 && /* @__PURE__ */ jsx("div", { className: "p-6 text-[12.5px] text-muted", children: "\u533A\u95F4\u5185\u65E0\u9519\u8BEF \u{1F389}" })
            ] }),
            detail ? /* @__PURE__ */ jsx(
              ErrorDetail,
              {
                detail,
                tab,
                setTab,
                variant,
                setVariant,
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
      className: `grid w-full grid-cols-[9px_1fr_auto] items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left transition-all duration-150 ${active ? "border-border-strong bg-accent-soft" : "border-transparent hover:border-border hover:bg-surface-2"}`,
      children: [
        /* @__PURE__ */ jsxs("span", { className: "relative flex h-2 w-2", children: [
          g.is_spiking && /* @__PURE__ */ jsx("span", { className: `absolute inline-flex h-full w-full rounded-full ${TONE_BG[tone]} opacity-60 animate-ping` }),
          /* @__PURE__ */ jsx("span", { className: `relative inline-flex h-2 w-2 rounded-full ${TONE_BG[tone]} ${g.is_spiking ? "animate-pulse-dot" : ""}` })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "min-w-0", children: [
          /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-1.5 font-mono text-[12.5px]", children: [
            /* @__PURE__ */ jsx("span", { className: "truncate", children: g.error_type }),
            g.is_new && /* @__PURE__ */ jsx("span", { className: "rounded-sm bg-accent-soft px-1 py-px text-[8.5px] text-accent", children: "NEW" }),
            g.is_spiking && /* @__PURE__ */ jsx("span", { className: "rounded-sm bg-danger/15 px-1 py-px text-[8.5px] text-danger", children: "\u26A1" })
          ] }),
          /* @__PURE__ */ jsx("div", { className: "mt-0.5 truncate font-mono text-[10px] text-subtle", children: g.logger_name }),
          /* @__PURE__ */ jsxs("div", { className: "mt-1 flex gap-2.5 font-mono text-[10px] text-muted", children: [
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
            className: `rounded-md border px-2.5 py-1.5 text-left font-mono text-[10.5px] ${i === variant ? "border-accent-deep bg-accent-soft text-fg" : "border-border bg-bg text-muted"}`,
            children: [
              /* @__PURE__ */ jsx("b", { className: "text-fg", children: v.count }),
              " \u6B21 \xB7 \u53D8\u4F53 ",
              i + 1
            ]
          },
          v.fingerprint
        )) }),
        /* @__PURE__ */ jsx("pre", { className: "m-0 max-h-[280px] overflow-auto rounded-lg border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-[#c4c4cc]", children: activeVariant?.traceback_text || detail.traceback_text })
      ] }),
      tab === "occ" && /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2", children: [
        detail.occurrences.length === 0 && /* @__PURE__ */ jsx("div", { className: "text-[12px] text-muted", children: "\u65E0\u53EF\u5173\u8054\u7684 session \u73B0\u573A\u3002" }),
        detail.occurrences.map((o) => /* @__PURE__ */ jsxs("div", { className: "grid grid-cols-[auto_1fr_auto] items-center gap-3.5 rounded-lg border border-border bg-bg px-3.5 py-2.5", children: [
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
              className: "whitespace-nowrap rounded-md border border-accent-deep bg-accent-soft px-2.5 py-1.5 font-mono text-[10.5px] text-[#dfe3ff]",
              children: "\u67E5\u770B\u5BF9\u8BDD \u2197"
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
          className: "rounded-md border border-accent-deep bg-accent-soft px-3 py-2 font-mono text-[11px] text-[#dfe3ff] transition-all duration-150 hover:brightness-110 active:brightness-95 disabled:opacity-40",
          children: "\u67E5\u770B\u6700\u8FD1\u5BF9\u8BDD \u2197"
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          type: "button",
          onClick: () => void navigator.clipboard?.writeText(detail.traceback_text),
          className: "rounded-md border border-border-strong bg-surface-2 px-3 py-2 font-mono text-[11px] text-muted transition-colors hover:text-fg",
          children: "\u590D\u5236 Traceback"
        }
      ),
      /* @__PURE__ */ jsx("div", { className: "flex-1" }),
      /* @__PURE__ */ jsx("button", { type: "button", onClick: () => onStatus("acknowledged"), className: "rounded-md border border-border-strong bg-surface-2 px-3 py-2 font-mono text-[11px] text-muted transition-colors hover:text-fg", children: "\u6807\u8BB0\u5DF2\u786E\u8BA4" }),
      /* @__PURE__ */ jsx("button", { type: "button", onClick: () => onStatus("ignored"), className: "rounded-md border border-border-strong bg-surface-2 px-3 py-2 font-mono text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger", children: "\u5FFD\u7565\u6B64\u7C7B\u578B" })
    ] })
  ] });
}
function Blast({ label, value, small }) {
  return /* @__PURE__ */ jsxs("div", { className: "bg-surface px-4 py-3", children: [
    /* @__PURE__ */ jsx("div", { className: "font-mono text-[9px] uppercase tracking-[0.12em] text-subtle", children: label }),
    /* @__PURE__ */ jsx("div", { className: `mt-1.5 font-mono font-semibold tabular-nums ${small ? "text-[12.5px]" : "text-[18px]"}`, children: value })
  ] });
}
function TabBtn({ active, onClick, children }) {
  return /* @__PURE__ */ jsx(
    "button",
    {
      type: "button",
      onClick,
      className: `-mb-px border-b-2 px-3 py-2 font-mono text-[11.5px] transition-colors ${active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg"}`,
      children
    }
  );
}
function SkelBlock({ className }) {
  return /* @__PURE__ */ jsx("div", { className: `relative overflow-hidden rounded-2xl border border-border bg-surface ${className}`, children: /* @__PURE__ */ jsx("div", { className: "absolute inset-0 -translate-x-full animate-scan bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" }) });
}
function ObserveSkeleton() {
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-4 p-6", children: [
    /* @__PURE__ */ jsxs("div", { className: "flex items-end justify-between", children: [
      /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2", children: [
        /* @__PURE__ */ jsx(SkelBlock, { className: "h-7 w-48 rounded-lg" }),
        /* @__PURE__ */ jsx(SkelBlock, { className: "h-3 w-64 rounded" })
      ] }),
      /* @__PURE__ */ jsx(SkelBlock, { className: "h-9 w-56 rounded-md" })
    ] }),
    /* @__PURE__ */ jsx("div", { className: "grid grid-cols-4 gap-4", children: [0, 1, 2, 3].map((i) => /* @__PURE__ */ jsx(SkelBlock, { className: "h-[132px]" }, i)) }),
    /* @__PURE__ */ jsx("div", { className: "grid grid-cols-2 gap-4", children: [0, 1, 2, 3].map((i) => /* @__PURE__ */ jsx(SkelBlock, { className: "h-[218px] rounded-lg" }, i)) })
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
  const portalRef = useRef(null);
  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const [ov, series, ge] = await Promise.all([
        api(`/api/dashboard/observe/overview?range=${range}`),
        api(`/api/dashboard/observe/timeseries?range=${range}`),
        api(`/api/dashboard/observe/global_errors/overview?range=${range}`)
      ]);
      setOverview(ov);
      setPoints(series.points ?? []);
      setGErr(ge);
      setUpdatedAt(Date.now());
    } finally {
      setRefreshing(false);
    }
  }, [range]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 15e3);
    return () => window.clearInterval(id);
  }, [load]);
  useEffect(() => {
    const id = window.setInterval(() => setNowTs(Date.now()), 1e3);
    return () => window.clearInterval(id);
  }, []);
  if (!overview) {
    return /* @__PURE__ */ jsx(ObserveSkeleton, {});
  }
  const turnSeries = points.map((p) => p.turns);
  const errorSeries = points.map((p) => p.errors);
  const tokenSeries = points.map((p) => p.input_tokens);
  const passiveHitSeries = points.map((p) => (p.passive_cache_hit_rate ?? 0) * 100);
  const proactiveHitSeries = points.map((p) => (p.proactive_cache_hit_rate ?? 0) * 100);
  const iterSeries = points.map((p) => p.avg_iteration ?? 0);
  const labelled = (vals) => points.map((p, i) => ({ label: _bucketLabel(p.bucket), value: vals[i] }));
  const gErrTotal = gErr?.total ?? overview.errors;
  const gErrSpark = gErr && gErr.spark.length > 1 ? gErr.spark : errorSeries;
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsxs(
      "div",
      {
        className: "flex flex-col gap-4 p-6 transition-[filter,transform,opacity] duration-[420ms]",
        style: drillOpen ? { filter: "blur(7px)", transform: "scale(0.97)", opacity: 0.5, pointerEvents: "none" } : void 0,
        children: [
          /* @__PURE__ */ jsxs("div", { className: "flex items-end justify-between", children: [
            /* @__PURE__ */ jsxs("div", { children: [
              /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-2.5", children: [
                /* @__PURE__ */ jsx("span", { className: "detail-title", children: "Observe \xB7 \u76D1\u6D4B" }),
                /* @__PURE__ */ jsxs("span", { className: "flex items-center gap-1.5 rounded-full border border-success/25 bg-success/10 px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.14em] text-success", children: [
                  /* @__PURE__ */ jsx("span", { className: "h-1.5 w-1.5 rounded-full bg-success animate-pulse-dot" }),
                  "Live"
                ] })
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
                  onClick: () => void load(),
                  className: `grid h-7 w-7 place-items-center rounded-md border border-border bg-surface-2 text-muted transition-colors hover:border-border-strong hover:text-fg ${refreshing ? "animate-spin" : ""}`,
                  title: "\u5237\u65B0",
                  children: "\u21BB"
                }
              ),
              /* @__PURE__ */ jsx("div", { className: "flex gap-1 rounded-md border border-border bg-surface-2 p-1", children: RANGES.map((r) => /* @__PURE__ */ jsx(
                "button",
                {
                  onClick: () => setRange(r.key),
                  className: `rounded-[4px] px-2.5 py-1 font-mono text-[11px] transition-all duration-150 active:brightness-95 ${range === r.key ? "bg-accent text-accent-ink hover:brightness-110" : "text-muted hover:bg-surface-3 hover:text-fg"}`,
                  children: r.label
                },
                r.key
              )) })
            ] })
          ] }),
          /* @__PURE__ */ jsxs(Grid, { columns: 4, children: [
            /* @__PURE__ */ jsx("div", { className: "animate-fade-up", style: { animationDelay: "0ms" }, children: /* @__PURE__ */ jsx(MetricTile, { label: "\u5BF9\u8BDD\u8F6E\u6570", value: _compact(overview.turns), delta: _delta(turnSeries), sub: overview.last_ts ? `\u6700\u8FD1 ${_shortTs(overview.last_ts)}` : "\u65E0\u8BB0\u5F55", tone: "accent", spark: turnSeries }) }),
            /* @__PURE__ */ jsxs(
              "div",
              {
                ref: portalRef,
                onClick: () => setDrillOpen(true),
                className: "group relative animate-fade-up cursor-pointer rounded-2xl transition-transform duration-200 hover:-translate-y-0.5",
                style: { animationDelay: "60ms" },
                children: [
                  gErrTotal > 0 && /* @__PURE__ */ jsxs("span", { className: "absolute left-[68px] top-[18px] z-10 flex h-2 w-2", children: [
                    (gErr?.spiking_types ?? 0) > 0 && /* @__PURE__ */ jsx("span", { className: "absolute inline-flex h-full w-full rounded-full bg-danger opacity-60 animate-ping" }),
                    /* @__PURE__ */ jsx("span", { className: "relative inline-flex h-2 w-2 rounded-full bg-danger animate-pulse-dot" })
                  ] }),
                  /* @__PURE__ */ jsx("span", { className: "pointer-events-none absolute right-4 top-4 z-10 font-mono text-[10px] text-danger opacity-0 transition-opacity group-hover:opacity-100", children: "\u5C55\u5F00\u5206\u6790 \u2192" }),
                  /* @__PURE__ */ jsx(MetricTile, { label: "\u9519\u8BEF", value: _compact(gErrTotal), sub: `${gErr?.types ?? 0} \u7C7B\u578B \xB7 \u70B9\u51FB\u5C55\u5F00`, tone: "danger", spark: gErrSpark })
                ]
              }
            ),
            /* @__PURE__ */ jsx("div", { className: "animate-fade-up", style: { animationDelay: "120ms" }, children: /* @__PURE__ */ jsx(MetricTile, { label: "\u88AB\u52A8 KV \u547D\u4E2D\u7387", value: _pct(overview.passive_cache_hit_rate), sub: `\u4E3B\u52A8 ${_pct(overview.proactive_cache_hit_rate)}`, tone: "success", spark: passiveHitSeries }) }),
            /* @__PURE__ */ jsx("div", { className: "animate-fade-up", style: { animationDelay: "180ms" }, children: /* @__PURE__ */ jsx(MetricTile, { label: "\u5E73\u5747\u8FED\u4EE3", value: overview.avg_iteration != null ? overview.avg_iteration.toFixed(1) : "\u2014", unit: `\u5CF0 ${overview.max_iteration}`, sub: "\u6BCF\u8F6E LLM \u8C03\u7528\u6B21\u6570", tone: "warning", spark: iterSeries }) })
          ] }),
          /* @__PURE__ */ jsxs(Grid, { columns: 2, children: [
            /* @__PURE__ */ jsx(Card, { title: "\u8F93\u5165 Token \u8D8B\u52BF", style: { animationDelay: "220ms" }, children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(tokenSeries), kind: "area", tone: "accent", valueFmt: _compact }) }),
            /* @__PURE__ */ jsx(Card, { title: "\u5E73\u5747\u8FED\u4EE3\u8D8B\u52BF", style: { animationDelay: "280ms" }, children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(iterSeries), kind: "area", tone: "warning", valueFmt: (n) => n.toFixed(1) }) }),
            /* @__PURE__ */ jsx(Card, { title: "\u5168\u5C40\u88AB\u52A8\u94FE\u8DEF\u547D\u4E2D\u7387\u8D8B\u52BF", style: { animationDelay: "340ms" }, children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(passiveHitSeries), kind: "area", tone: "success", valueFmt: (n) => `${n.toFixed(0)}%` }) }),
            /* @__PURE__ */ jsx(Card, { title: "\u5168\u5C40\u4E3B\u52A8\u94FE\u8DEF\u547D\u4E2D\u7387\u8D8B\u52BF", style: { animationDelay: "400ms" }, children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(proactiveHitSeries), kind: "area", tone: "accent", valueFmt: (n) => `${n.toFixed(0)}%` }) }),
            /* @__PURE__ */ jsx(Card, { title: "\u9519\u8BEF\u8D8B\u52BF", style: { animationDelay: "460ms" }, children: /* @__PURE__ */ jsx(TrendChart, { data: labelled(errorSeries), kind: "bar", tone: "danger", valueFmt: (n) => String(n), empty: "\u533A\u95F4\u5185\u65E0\u9519\u8BEF \u{1F389}" }) })
          ] })
        ]
      }
    ),
    drillOpen && /* @__PURE__ */ jsx(ErrorDrill, { portalRef, range, onClose: () => setDrillOpen(false) })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "observe",
  label: "Observe \u76D1\u6D4B",
  viewLabel: "observe",
  layout: "workbench",
  pageSize: 30,
  rowKey: "id",
  countTitle(total) {
    return `${total} \u8F6E\u9065\u6D4B`;
  },
  columns: [
    { key: "session_key", label: "Session", width: 120, cellClass: "mono cell-session", rawTitle: true },
    { key: "ts", label: "Time", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "error", label: "Error", flex: true, cellClass: "content-preview" }
  ],
  async getCount() {
    try {
      const ov = await api("/api/dashboard/observe/overview?range=all");
      return ov.turns || 0;
    } catch {
      return null;
    }
  },
  async fetchPage({ page, pageSize }) {
    const data = await api(
      `/api/dashboard/observe/errors?range=all&page=${page}&page_size=${pageSize}`
    );
    return { items: data.items || [], total: data.total || 0 };
  },
  Main: ObserveMain
});
