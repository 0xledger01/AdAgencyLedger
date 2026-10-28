import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, download, money, qs, type Session } from "../api";
import { Sparkline, TrendChart, platformColor } from "../components/Charts";
import { fmtDate } from "../utils";

/* ------------------------------------------------------------------ 类型 */

type GroupBy = "client" | "account" | "sales" | "agent";
type View = GroupBy | "detail";
type Mode = "summary" | "daily";
type TopMetric = "total" | "gmv" | "bc";

interface SerializedBucket {
  gmv_bc: { total: string; net: string };
  gmv_max: { total: string; net: string };
  bc: {
    total: string;
    net: string;
    cash: string;
    grant: string;
    impressions: number;
    cpm: string | null;
    clicks: number;
    ctr: string | null;
    cpc: string | null;
    conversions: number;
    cvr: string | null;
  };
  currency: string;
}

interface Overview extends SerializedBucket {
  mom_pct: number | null;
  prev_total: string;
  gmv_bc_total: string;
  gmv_bc_net: string;
  gmv_total: string;
  gmv_net: string;
  bc_total: string;
  bc_net: string;
  bc_cash: string;
  bc_grant: string;
  impressions: number;
  clicks: number;
  conversions: number;
  ctr: string | null;
  cpc: string | null;
  cpm: string | null;
}

interface ReportRow extends SerializedBucket {
  key: string;
  date: string;
  label: string;
  sub_label: string;
  platform: string;
  platform_label: string;
  client_id: number;
  client_name: string;
  client_code: string;
  sales_owner: string;
  agent: string;
  account_id: string;
  ad_account_id: number;
  spark: string[];
  mom_pct: number | null;
}

interface Totals extends SerializedBucket {
  label: string;
  sub_label: string;
  platform: string;
  platform_label: string;
  spark: string[];
  mom_pct: number | null;
}

interface TopClient {
  client_id: number;
  client_name: string;
  client_code: string;
  sales_owner: string;
  amount: string;
  share_pct: number;
  mom_pct: number | null;
}

interface ReportResp {
  range: {
    date_from: string;
    date_to: string;
    days: number;
    prev_from: string;
    prev_to: string;
    currency: string;
    currencies: string[];
  };
  group_by: GroupBy;
  mode: Mode;
  top_metric: TopMetric;
  overview: Overview;
  top_clients: TopClient[];
  trend: { days: string[]; series: { platform: string; label: string; points: string[] }[] };
  rows: ReportRow[];
  totals: Totals;
}

interface DetailRow {
  id: number;
  date: string;
  account_id: string;
  client_name: string;
  platform: string;
  spend: string;
  currency: string;
  source: string;
  anomaly: string | null;
  gmv_max_spend: string;
  bc_spend: string;
  bc_cash_spend: string;
  bc_grant_spend: string;
  impressions: number;
  clicks: number;
  conversions: number;
}

interface DetailResp {
  items: DetailRow[];
  total: number;
  totals: { spend: { currency: string; amount: string }[] };
}

interface ImportIssue {
  row: number;
  account_id: string;
  date: string | null;
  reason: string;
  spend?: string;
}

interface ImportResp {
  dry_run: boolean;
  total: number;
  success: number;
  skipped: ImportIssue[];
  failed: ImportIssue[];
  anomalies: ImportIssue[];
}

/* ------------------------------------------------------------------ 工具 */

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function ymd(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function shiftDate(day: string, delta: number): string {
  const d = new Date(`${day}T00:00:00`);
  d.setDate(d.getDate() + delta);
  return ymd(d);
}

function initialRange(): { from: string; to: string } {
  const to = new Date();
  const from = new Date();
  from.setDate(to.getDate() - 6);
  return { from: ymd(from), to: ymd(to) };
}

export function ccySymbol(currency: string | undefined): string {
  if (currency === "USD") return "$";
  if (currency === "HKD") return "HK$";
  return "";
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function momClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "muted";
  if (v > 0) return "mom-up";
  if (v < 0) return "mom-down";
  return "muted";
}

function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("zh-CN");
}

function pctText(v: string | null | undefined): string {
  return v === null || v === undefined || v === "" ? "—" : `${v}%`;
}

/* ------------------------------------------------------------------ 导入解析 */

const FIELD_ALIASES: Record<string, string[]> = {
  account_id: ["account_id", "account id", "账户ID", "广告账户ID", "账户", "广告账户", "account"],
  date: ["date", "日期", "统计日期"],
  spend: ["spend", "消耗", "总消耗", "消耗金额", "总花费", "GMV Max + BC总消耗", "GMV Max+BC总消耗", "GMV Max BC总消耗"],
  gmv_max_spend: ["gmv_max_spend", "GMV Max总消耗", "GMV Max 消耗"],
  gmv_max_net_spend: ["gmv_max_net_spend", "GMV Max净消耗"],
  bc_spend: ["bc_spend", "BC总消耗", "BC 消耗"],
  bc_net_spend: ["bc_net_spend", "BC净消耗"],
  bc_cash_spend: ["bc_cash_spend", "BC现金消耗", "现金消耗"],
  bc_grant_spend: ["bc_grant_spend", "BC赠款消耗", "赠款消耗"],
  impressions: ["impressions", "曝光数", "曝光", "展示数", "展示"],
  clicks: ["clicks", "点击数", "点击"],
  conversions: ["conversions", "转化数", "转化"],
  currency: ["currency", "币种"],
  exchange_rate: ["exchange_rate", "汇率"],
};

const NUMERIC_FIELDS = new Set([
  "spend", "gmv_max_spend", "gmv_max_net_spend", "bc_spend", "bc_net_spend",
  "bc_cash_spend", "bc_grant_spend", "impressions", "clicks", "conversions",
  "exchange_rate",
]);

const SAMPLE = [
  "账户ID,日期,GMV Max总消耗,GMV Max净消耗,BC总消耗,BC净消耗,BC现金消耗,BC赠款消耗,曝光数,点击数,转化数",
  "act_1001,2026-10-20,840,800,360,350,300,60,45000,810,49",
  "act_2001,2026-10-20,1260,1200,540,520,450,90,67500,1215,73",
].join("\n");

function normKey(s: string): string {
  return String(s ?? "")
    .replace(/^\ufeff/, "")
    .toLowerCase()
    .replace(/[\s_\-+（）()：:]/g, "")
    .trim();
}

const ALIAS_LOOKUP = new Map<string, string>();
for (const [field, names] of Object.entries(FIELD_ALIASES)) {
  for (const n of names) ALIAS_LOOKUP.set(normKey(n), field);
}

/** 支持引号、逗号、制表符、CRLF 的轻量 CSV 解析（不引第三方库）。 */
function parseCsvTable(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        cell += ch;
      }
      continue;
    }
    if (ch === '"') {
      quoted = true;
    } else if (ch === "," || ch === "\t") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (ch !== "\r") {
      cell += ch;
    }
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

function cleanRow(raw: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(raw)) {
    const text = String(val ?? "").trim();
    if (NUMERIC_FIELDS.has(k)) {
      let num = text.replace(/[,\s$¥]/g, "");
      if (num === "-" || num === "—") num = "";
      out[k] = num;
    } else {
      out[k] = text;
    }
  }
  return out;
}

function parseInput(text: string): { rows: Record<string, string>[]; error: string } {
  const raw = text.trim();
  if (!raw) return { rows: [], error: "请先粘贴 CSV / JSON，或选择一个 CSV 文件" };

  if (raw.startsWith("[") || raw.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(raw);
      const arr = Array.isArray(parsed)
        ? parsed
        : (parsed as { rows?: unknown })?.rows;
      if (!Array.isArray(arr)) return { rows: [], error: "JSON 需要是数组，或 {\"rows\": [...]}" };
      const rows = arr.map((item) => {
        const src = (item ?? {}) as Record<string, unknown>;
        const mapped: Record<string, string> = {};
        for (const [k, v] of Object.entries(src)) {
          const field = ALIAS_LOOKUP.get(normKey(k)) || normKey(k);
          mapped[field] = v === null || v === undefined ? "" : String(v);
        }
        return cleanRow(mapped);
      });
      if (!rows.length) return { rows: [], error: "JSON 里没有数据行" };
      return { rows, error: "" };
    } catch (e) {
      return { rows: [], error: `JSON 解析失败：${e instanceof Error ? e.message : String(e)}` };
    }
  }

  const table = parseCsvTable(raw);
  if (!table.length) return { rows: [], error: "没有解析出任何数据行" };

  const first = table[0];
  const headerMap = first.map((c) => ALIAS_LOOKUP.get(normKey(c)) || null);
  const recognized = headerMap.filter(Boolean).length;
  const hasHeader = recognized >= 2 && headerMap.includes("account_id") && headerMap.includes("date");

  const rows: Record<string, string>[] = [];
  if (hasHeader) {
    for (const cells of table.slice(1)) {
      const obj: Record<string, string> = {};
      headerMap.forEach((field, i) => {
        if (field) obj[field] = cells[i] ?? "";
      });
      if (Object.values(obj).some((v) => v.trim() !== "")) rows.push(cleanRow(obj));
    }
  } else {
    // 老格式无表头：account_id,date,spend[,currency,exchange_rate]
    const keys = ["account_id", "date", "spend", "currency", "exchange_rate"];
    for (const cells of table) {
      const obj: Record<string, string> = {};
      keys.forEach((k, i) => {
        if (cells[i] !== undefined) obj[k] = cells[i];
      });
      rows.push(cleanRow(obj));
    }
  }

  if (!rows.length) {
    return { rows: [], error: "没有数据行。表头需含「账户ID / 日期」，并至少填一种消耗金额" };
  }
  return { rows, error: "" };
}

/* ------------------------------------------------------------------ 页面 */

const TABS: { key: View; label: string }[] = [
  { key: "client", label: "客户报表" },
  { key: "account", label: "账户报表" },
  { key: "sales", label: "销售报表" },
  { key: "agent", label: "代理商报表" },
  { key: "detail", label: "消耗明细" },
];

// 当前只做 TikTok：报表媒体筛选只给 TikTok。
const PLATFORMS = [
  { value: "", label: "全部媒体" },
  { value: "TIKTOK", label: "TikTok" },
];

export default function DeskSpend({ session }: { session: Session }) {
  const [range] = useState(initialRange);
  const [view, setView] = useState<View>("client");
  const [mode, setMode] = useState<Mode>("summary");
  const [topMetric, setTopMetric] = useState<TopMetric>("total");
  const [keyword, setKeyword] = useState("");
  const [appliedKeyword, setAppliedKeyword] = useState("");
  const [sales, setSales] = useState("");
  const [platform, setPlatform] = useState("");
  const [currency, setCurrency] = useState("");
  const [from, setFrom] = useState(range.from);
  const [to, setTo] = useState(range.to);

  const [report, setReport] = useState<ReportResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);

  const [detailRows, setDetailRows] = useState<DetailRow[]>([]);
  const [detailTotals, setDetailTotals] = useState<{ currency: string; amount: string }[]>([]);
  const [detailTotal, setDetailTotal] = useState(0);
  const [detailAccount, setDetailAccount] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);

  const [impOpen, setImpOpen] = useState(false);
  const [impText, setImpText] = useState("");
  const [impResult, setImpResult] = useState<ImportResp | null>(null);
  const [impMsg, setImpMsg] = useState("");
  const [impErr, setImpErr] = useState("");
  const [impBusy, setImpBusy] = useState(false);

  const seqRef = useRef(0);
  const detailSeq = useRef(0);

  useEffect(() => {
    const t = setTimeout(() => setAppliedKeyword(keyword.trim()), 320);
    return () => clearTimeout(t);
  }, [keyword]);

  const loadReport = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ReportResp>(
        "desk",
        "/api/desk/spend/report" + qs({
          date_from: from,
          date_to: to,
          group_by: view === "detail" ? "client" : view,
          mode,
          top_metric: topMetric,
          client_keyword: appliedKeyword || undefined,
          sales_owner: sales.trim() || undefined,
          platform: platform || undefined,
          currency: currency || undefined,
        })
      );
      if (seq !== seqRef.current) return;
      setReport(d);
      // 切日期/筛选后，原来选的币种可能已经不在可选清单里：回落到当前最大币种
      const nextCurrency =
        d.range.currencies.length && !d.range.currencies.includes(d.range.currency)
          ? d.range.currencies[0]
          : d.range.currency;
      if (nextCurrency && nextCurrency !== currency) {
        setCurrency(nextCurrency);
      }
    } catch (e) {
      if (seq !== seqRef.current) return;
      setErr(e instanceof Error ? e.message : "报表加载失败");
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [from, to, view, mode, topMetric, appliedKeyword, sales, platform, currency]);

  useEffect(() => {
    if (view === "detail") return;
    loadReport();
  }, [view, loadReport, refreshTick]);

  const loadDetail = useCallback(async () => {
    const seq = ++detailSeq.current;
    setDetailLoading(true);
    setErr("");
    try {
      const d = await api.get<DetailResp>(
        "desk",
        "/api/desk/spend" + qs({
          date_from: from || undefined,
          date_to: to || undefined,
          account_id: detailAccount.trim() || undefined,
          limit: 200,
        })
      );
      if (seq !== detailSeq.current) return;
      setDetailRows(d.items ?? []);
      setDetailTotals(d.totals?.spend ?? []);
      setDetailTotal(d.total ?? 0);
    } catch (e) {
      if (seq !== detailSeq.current) return;
      setErr(e instanceof Error ? e.message : "明细加载失败");
    } finally {
      if (seq === detailSeq.current) setDetailLoading(false);
    }
  }, [from, to, detailAccount]);

  useEffect(() => {
    if (view === "detail") loadDetail();
  }, [view, loadDetail, refreshTick]);

  async function doImport(dryRun: boolean) {
    setImpErr("");
    setImpMsg("");
    const parsed = parseInput(impText);
    if (parsed.error) {
      setImpErr(parsed.error);
      setImpResult(null);
      return;
    }
    setImpBusy(true);
    try {
      const d = await api.post<ImportResp>("desk", "/api/desk/spend/import", {
        dry_run: dryRun,
        rows: parsed.rows,
      });
      setImpResult(d);
      setImpMsg(
        dryRun
          ? `预校验完成：共 ${d.total} 行，可导入 ${d.success} 行（未落库）`
          : `导入完成：共 ${d.total} 行，成功 ${d.success} 行`
      );
      if (!dryRun) {
        setRefreshTick((v) => v + 1);
      }
    } catch (e) {
      setImpErr(e instanceof Error ? e.message : dryRun ? "预校验失败" : "导入失败");
    } finally {
      setImpBusy(false);
    }
  }

  function exportCsv() {
    const path = "/api/desk/spend/report/export" + qs({
      date_from: from,
      date_to: to,
      group_by: view === "detail" ? "client" : view,
      mode,
      top_metric: topMetric,
      client_keyword: appliedKeyword || undefined,
      sales_owner: sales.trim() || undefined,
      platform: platform || undefined,
      currency: currency || undefined,
    });
    download("desk", path, `spend-report-${from}-${to}.csv`);
  }

  const symbol = ccySymbol(report?.range.currency || currency);
  const currencies = report?.range.currencies ?? [];
  const parsedCount = useMemo(
    () => (impText.trim() ? parseInput(impText).rows.length : 0),
    [impText]
  );

  return (
    <>
      {view !== "detail" && (
        <div className="card report-hero">
          <div className="card-head">
            <h2>数据概览</h2>
            <div className="toolbar">
              <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
              <span className="faint">至</span>
              <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
              {currencies.length > 1 && (
                <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
                  {currencies.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              )}
              <button className="btn sm" onClick={() => setRefreshTick((v) => v + 1)} disabled={loading}>
                刷新
              </button>
            </div>
          </div>
          <div className="card-body">
            {!report && loading ? (
              <div className="report-kpis">
                <div className="skeleton" />
                <div className="skeleton" />
                <div className="skeleton" />
              </div>
            ) : (
              <>
                <div className="report-kpis">
                  <div className="report-kpi primary">
                    <div className="k">GMV Max + BC 总消耗</div>
                    <div className="v">
                      <span className="ccy">{symbol}</span>
                      {money(report?.overview.gmv_bc_total)}
                    </div>
                    <div className="s">
                      净消耗 {money(report?.overview.gmv_bc_net)}
                      <span className={`mom ${momClass(report?.overview.mom_pct)}`}>
                        环比 {fmtPct(report?.overview.mom_pct)}
                      </span>
                    </div>
                  </div>
                  <div className="report-kpi">
                    <div className="k">GMV Max 总消耗</div>
                    <div className="v">
                      <span className="ccy">{symbol}</span>
                      {money(report?.overview.gmv_total)}
                    </div>
                    <div className="s">净消耗 {money(report?.overview.gmv_net)}</div>
                  </div>
                  <div className="report-kpi">
                    <div className="k">BC 总消耗</div>
                    <div className="v">
                      <span className="ccy">{symbol}</span>
                      {money(report?.overview.bc_total)}
                    </div>
                    <div className="s">
                      净消耗 {money(report?.overview.bc_net)} · 赠款 {money(report?.overview.bc_grant)}
                    </div>
                  </div>
                </div>
                <div className="report-mini">
                  <span>曝光数 <b>{fmtInt(report?.overview.impressions)}</b></span>
                  <span>点击数 <b>{fmtInt(report?.overview.clicks)}</b></span>
                  <span>点击率 <b>{pctText(report?.overview.ctr)}</b></span>
                  <span>平均千展 <b>{report?.overview.cpm ? `${symbol}${money(report.overview.cpm)}` : "—"}</b></span>
                  <span>平均点击费用 <b>{report?.overview.cpc ? `${symbol}${money(report.overview.cpc)}` : "—"}</b></span>
                  <span>转化数 <b>{fmtInt(report?.overview.conversions)}</b></span>
                  <span className="faint">
                    {from} 至 {to} · 对比 {report?.range.prev_from ?? "—"} ~ {report?.range.prev_to ?? "—"}
                  </span>
                </div>
              </>
            )}
            {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
          </div>
        </div>
      )}

      {view !== "detail" && (
        <div className="report-grid">
          <div className="card">
            <div className="card-head">
              <h2>消耗 TOP10 客户</h2>
              <select value={topMetric} onChange={(e) => setTopMetric(e.target.value as TopMetric)}>
                <option value="total">按 GMV Max + BC</option>
                <option value="gmv">按 GMV Max</option>
                <option value="bc">按 BC</option>
              </select>
            </div>
            <div className="top-list">
              {(report?.top_clients ?? []).map((c, i) => (
                <div className="top-item" key={c.client_id}>
                  <span className={`rank r${i + 1}`}>{i + 1}</span>
                  <div className="top-main">
                    <div className="top-name">{c.client_name}</div>
                    <div className="cell-sub">
                      {c.client_code || "—"} · {c.sales_owner}
                    </div>
                    <div className="top-bar">
                      <i style={{ width: `${Math.max(c.share_pct, 1)}%` }} />
                    </div>
                  </div>
                  <div className="top-right">
                    <div className="top-amount">
                      {symbol}
                      {money(c.amount)}
                    </div>
                    <div className={`mom ${momClass(c.mom_pct)}`}>环比 {fmtPct(c.mom_pct)}</div>
                  </div>
                </div>
              ))}
              {report && !report.top_clients.length && <div className="empty">暂无消耗数据</div>}
              {!report && loading && <div className="empty">加载中…</div>}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2>消耗趋势</h2>
              <span className="faint">{report?.range.currency ?? ""} · 按天</span>
            </div>
            <div className="card-body">
              {report ? (
                <TrendChart
                  days={report.trend.days}
                  series={report.trend.series}
                  symbol={symbol}
                />
              ) : (
                <div className="skeleton" style={{ height: 280 }} />
              )}
            </div>
          </div>
        </div>
      )}

      <div className="card report-card">
        <div className="card-head">
          <div className="subtabs" style={{ marginBottom: 0, flex: 1, borderBottom: 0 }}>
            {TABS.map((t) => (
              <button
                key={t.key}
                className={view === t.key ? "on" : ""}
                onClick={() => setView(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <button className="btn primary sm" onClick={() => setImpOpen(true)}>
            导入消耗
          </button>
        </div>

        <div className="filterbar report-filterbar">
          <div className="filter-field">
            <label>开始日期</label>
            <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
          </div>
          <div className="filter-field">
            <label>结束日期</label>
            <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
          </div>
          {view !== "detail" && (
            <div className="filter-field">
              <label>分组类型</label>
              <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                <option value="summary">汇总</option>
                <option value="daily">按日</option>
              </select>
            </div>
          )}
          {view !== "detail" ? (
            <div className="filter-field">
              <label>客户名称 / 编号</label>
              <input
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                placeholder="公司名 / CRM 编号"
              />
            </div>
          ) : (
            <div className="filter-field">
              <label>广告账户 ID</label>
              <input
                value={detailAccount}
                onChange={(e) => setDetailAccount(e.target.value)}
                placeholder="act_1001"
              />
            </div>
          )}
          {view !== "detail" && (
            <div className="filter-field">
              <label>所属销售</label>
              <input value={sales} onChange={(e) => setSales(e.target.value)} placeholder="请输入" />
            </div>
          )}
          <div className="filter-field">
            <label>媒体</label>
            <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
              {PLATFORMS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </div>
          {view !== "detail" && currencies.length > 1 && (
            <div className="filter-field">
              <label>币种</label>
              <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
                {currencies.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
          )}
          <div className="filter-field filter-actions">
            <button className="btn sm" onClick={() => setRefreshTick((v) => v + 1)} disabled={loading || detailLoading}>
              查询
            </button>
            {view !== "detail" && (
              <button className="btn sm" onClick={exportCsv} disabled={!report || loading}>
                导出
              </button>
            )}
          </div>
        </div>

        {view === "detail" ? (
          <DetailTable
            rows={detailRows}
            totals={detailTotals}
            total={detailTotal}
            loading={detailLoading}
          />
        ) : (
          <ReportTable
            data={report}
            loading={loading}
            view={view as GroupBy}
          />
        )}
      </div>

      {impOpen && (
        <div className="modal-mask" onClick={() => !impBusy && setImpOpen(false)}>
          <div
            className="modal"
            style={{ width: "min(900px, 96vw)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-head">
              <div>
                <h2>导入消耗</h2>
                <p className="faint" style={{ margin: "3px 0 0", fontSize: 11 }}>
                  支持媒体后台导出的 CSV 或 JSON；先预校验，再落库
                </p>
              </div>
              <button className="btn ghost sm" onClick={() => setImpOpen(false)} disabled={impBusy}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="toolbar" style={{ marginBottom: 8 }}>
                <label className="btn sm" style={{ cursor: "pointer" }}>
                  选择 CSV 文件
                  <input
                    type="file"
                    accept=".csv,.txt,text/csv"
                    style={{ display: "none" }}
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      if (f.size > 2 * 1024 * 1024) {
                        setImpErr("文件超过 2MB，请拆分后再导入");
                        return;
                      }
                      try {
                        setImpText(await f.text());
                        setImpResult(null);
                        setImpErr("");
                        setImpMsg(`已读入文件「${f.name}」，先点预校验再导入`);
                      } catch {
                        setImpErr("读取文件失败");
                      } finally {
                        e.target.value = "";
                      }
                    }}
                    disabled={impBusy}
                  />
                </label>
                <button className="btn sm" onClick={() => { setImpText(SAMPLE); setImpResult(null); setImpErr(""); setImpMsg(""); }} disabled={impBusy}>
                  填入示例
                </button>
                <button className="btn sm" onClick={() => { setImpText(""); setImpResult(null); setImpMsg(""); setImpErr(""); }} disabled={impBusy}>
                  清空
                </button>
                <span className="faint">已解析 {parsedCount} 行</span>
              </div>

              <textarea
                className="ta"
                style={{ minHeight: 150 }}
                value={impText}
                onChange={(e) => setImpText(e.target.value)}
                placeholder={
                  "CSV 表头（中英文均可）：账户ID,日期,GMV Max总消耗,GMV Max净消耗,BC总消耗,BC净消耗,BC现金消耗,BC赠款消耗,曝光数,点击数,转化数\n" +
                  "也兼容老格式：account_id,date,spend[,currency,exchange_rate]\n" +
                  "或 JSON：[{\"account_id\":\"act_1001\",\"date\":\"2026-10-27\",\"spend\":\"120.5\"}]"
                }
              />
              <div className="hint" style={{ marginTop: 8 }}>
                账户ID + 日期是唯一键：同一账户同一天重复导入会跳过（幂等）。
                总消耗 = GMV Max + BC；只填总消耗时按 BC 口径落库。
                客户、媒体由账户自动带出，不需要在文件里填。
              </div>
              {impMsg && <div className="warnbox" style={{ marginTop: 10 }}>{impMsg}</div>}
              {impErr && <div className="err" style={{ marginTop: 10 }}>{impErr}</div>}

              {impResult && (
                <>
                  <div className="kpis" style={{ marginTop: 12 }}>
                    <div className="kpi">
                      <div className="k">成功</div>
                      <div className="v in">{impResult.success}</div>
                      <div className="s">{impResult.dry_run ? "可导入" : "已落库"}</div>
                    </div>
                    <div className="kpi">
                      <div className="k">跳过</div>
                      <div className="v muted">{impResult.skipped.length}</div>
                      <div className="s">已存在 / 批次内重复</div>
                    </div>
                    <div className="kpi">
                      <div className="k">失败</div>
                      <div className="v out">{impResult.failed.length}</div>
                      <div className="s">日期 / 账户 / 金额 / 指标</div>
                    </div>
                    <div className="kpi">
                      <div className="k">异常标记</div>
                      <div className="v">{impResult.anomalies.length}</div>
                      <div className="s">不阻断导入</div>
                    </div>
                  </div>
                  {(impResult.failed.length > 0 || impResult.skipped.length > 0 || impResult.anomalies.length > 0) && (
                    <div className="report-table-wrap" style={{ marginTop: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
                      <table className="report compact">
                        <thead>
                          <tr>
                            <th>行号</th>
                            <th>账户 ID</th>
                            <th>日期</th>
                            <th>原因</th>
                          </tr>
                        </thead>
                        <tbody>
                          {impResult.failed.map((f, i) => (
                            <tr key={`f${i}`} className="row-error">
                              <td className="mono">{f.row}</td>
                              <td className="mono">{f.account_id || "—"}</td>
                              <td className="mono">{f.date || "—"}</td>
                              <td>{f.reason}</td>
                            </tr>
                          ))}
                          {impResult.skipped.map((s, i) => (
                            <tr key={`s${i}`}>
                              <td className="mono">{s.row}</td>
                              <td className="mono">{s.account_id || "—"}</td>
                              <td className="mono">{s.date || "—"}</td>
                              <td className="muted">{s.reason}</td>
                            </tr>
                          ))}
                          {impResult.anomalies.map((a, i) => (
                            <tr key={`a${i}`} className="row-anomaly">
                              <td className="mono">{a.row}</td>
                              <td className="mono">{a.account_id || "—"}</td>
                              <td className="mono">{a.date || "—"}</td>
                              <td>
                                {a.reason}
                                {a.spend ? ` · 消耗 ${money(a.spend)}` : ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setImpOpen(false)} disabled={impBusy}>
                关闭
              </button>
              <button className="btn" onClick={() => doImport(true)} disabled={impBusy || !impText.trim()}>
                预校验
              </button>
              <button className="btn primary" onClick={() => doImport(false)} disabled={impBusy || !impText.trim()}>
                导入
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 报表表格 */

function MetricCells({ row }: { row: SerializedBucket }) {
  return (
    <>
      <td className="num strong">{money(row.gmv_bc.total)}</td>
      <td className="num">{money(row.gmv_bc.net)}</td>
      <td className="num strong">{money(row.gmv_max.total)}</td>
      <td className="num">{money(row.gmv_max.net)}</td>
      <td className="num strong">{money(row.bc.total)}</td>
      <td className="num">{money(row.bc.net)}</td>
      <td className="num">{money(row.bc.cash)}</td>
      <td className="num">{money(row.bc.grant)}</td>
      <td className="num">{fmtInt(row.bc.impressions)}</td>
      <td className="num">{row.bc.cpm ? money(row.bc.cpm) : "—"}</td>
      <td className="num">{fmtInt(row.bc.clicks)}</td>
      <td className="num">{pctText(row.bc.ctr)}</td>
      <td className="num">{row.bc.cpc ? money(row.bc.cpc) : "—"}</td>
      <td className="num">{fmtInt(row.bc.conversions)}</td>
    </>
  );
}

function ReportTable({
  data,
  loading,
  view,
}: {
  data: ReportResp | null;
  loading: boolean;
  view: GroupBy;
}) {
  const entityLabel = view === "account" ? "账户" : view === "sales" ? "销售" : view === "agent" ? "代理商" : "客户";
  const prevLabel = data ? `${data.range.prev_from.slice(5)} ~ ${data.range.prev_to.slice(5)}` : "上一周期";

  if (!data && loading) {
    return <div className="card-body"><div className="skeleton" style={{ height: 260 }} /></div>;
  }
  if (!data) {
    return <div className="empty">暂无报表数据</div>;
  }

  return (
    <>
      <div className="report-table-wrap">
        <table className="report">
          <thead>
            <tr>
              <th rowSpan={2}>媒体</th>
              <th rowSpan={2}>日期</th>
              <th rowSpan={2}>{entityLabel}</th>
              <th rowSpan={2}>所属销售</th>
              <th rowSpan={2}>代理商</th>
              <th rowSpan={2}>数据趋势</th>
              <th rowSpan={2} className="num">
                环比趋势
                <div className="th-sub">对比 {prevLabel}</div>
              </th>
              <th colSpan={2} className="grp">GMV Max + BC</th>
              <th colSpan={2} className="grp">GMV Max</th>
              <th colSpan={10} className="grp">BC</th>
            </tr>
            <tr>
              <th className="num grp-start">总消耗</th>
              <th className="num">净消耗</th>
              <th className="num grp-start">总消耗</th>
              <th className="num">净消耗</th>
              <th className="num grp-start">总消耗</th>
              <th className="num">净消耗</th>
              <th className="num">现金消耗</th>
              <th className="num">赠款消耗</th>
              <th className="num">曝光数</th>
              <th className="num">平均千展费用</th>
              <th className="num">点击数</th>
              <th className="num">点击率</th>
              <th className="num">平均点击费用</th>
              <th className="num">转化数</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.key}>
                <td>
                  {r.platform && (
                    <span className="platform-dot" style={{ background: platformColor(r.platform) }} />
                  )}
                  {r.platform_label}
                </td>
                <td className="mono">{data.mode === "daily" ? fmtDate(r.date) : "汇总"}</td>
                <td className="report-entity">
                  <div className={view === "account" ? "mono" : ""}>{r.label}</div>
                  {r.sub_label && <div className="cell-sub">{r.sub_label}</div>}
                </td>
                <td>{r.sales_owner}</td>
                <td>{r.agent}</td>
                <td>
                  <Sparkline
                    points={r.spark.map(Number)}
                    color={r.platform ? platformColor(r.platform) : "var(--accent)"}
                  />
                </td>
                <td className={`num mom-cell ${momClass(r.mom_pct)}`}>
                  {data.mode === "daily" ? "—" : `环比${fmtPct(r.mom_pct)}`}
                </td>
                <MetricCells row={r} />
              </tr>
            ))}
            {!data.rows.length && (
              <tr>
                <td colSpan={21}>
                  <div className="empty">所选条件下没有消耗数据</div>
                </td>
              </tr>
            )}
          </tbody>
          {data.rows.length > 0 && (
            <tfoot>
              <tr className="sum-row">
                <td />
                <td />
                <td>合计</td>
                <td />
                <td />
                <td />
                <td className={`num ${momClass(data.totals.mom_pct)}`}>
                  环比{fmtPct(data.totals.mom_pct)}
                </td>
                <MetricCells row={data.totals} />
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
        共 {data.rows.length} 行 · 币种 {data.range.currency}
        {data.range.currencies.length > 1 && `（可选 ${data.range.currencies.join(" / ")}，不跨币种求和）`}
        {" · "}GMV Max + BC = 总消耗；曝光/点击/转化只挂 BC 侧。
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ 明细表格 */

function DetailTable({
  rows,
  totals,
  total,
  loading,
}: {
  rows: DetailRow[];
  totals: { currency: string; amount: string }[];
  total: number;
  loading: boolean;
}) {
  return (
    <>
      {totals.length > 0 && (
        <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="row">
            <span className="faint">合计消耗：</span>
            {totals.map((t) => (
              <span key={t.currency} className="mono">
                {ccySymbol(t.currency)}{money(t.amount)} {t.currency}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="report-table-wrap">
        <table className="report compact detail">
          <thead>
            <tr>
              <th>日期</th>
              <th>账户 ID</th>
              <th>客户</th>
              <th>媒体</th>
              <th className="num">消耗</th>
              <th className="num">GMV Max</th>
              <th className="num">BC</th>
              <th className="num">现金</th>
              <th className="num">赠款</th>
              <th className="num">曝光数</th>
              <th className="num">点击数</th>
              <th className="num">转化数</th>
              <th>来源</th>
              <th>异常</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={r.anomaly ? "row-anomaly" : ""}>
                <td className="mono">{fmtDate(r.date)}</td>
                <td className="mono">{r.account_id}</td>
                <td>{r.client_name}</td>
                <td>{r.platform}</td>
                <td className="num mono out">
                  {money(r.spend)} <span className="faint">{r.currency}</span>
                </td>
                <td className="num">{money(r.gmv_max_spend)}</td>
                <td className="num">{money(r.bc_spend)}</td>
                <td className="num">{money(r.bc_cash_spend)}</td>
                <td className="num">{money(r.bc_grant_spend)}</td>
                <td className="num">{fmtInt(r.impressions)}</td>
                <td className="num">{fmtInt(r.clicks)}</td>
                <td className="num">{fmtInt(r.conversions)}</td>
                <td className="muted">{r.source}</td>
                <td>{r.anomaly ? <span className="badge amber">{r.anomaly}</span> : <span className="faint">—</span>}</td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={14}>
                  <div className="empty">{loading ? "加载中…" : "暂无消耗记录"}</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
        共 {total} 条，当前显示前 200 条（按日期倒序）。消耗只扣广告账户，不动客户钱包。
      </div>
    </>
  );
}
