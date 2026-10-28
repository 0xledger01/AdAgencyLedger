import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface Row {
  id: number;
  txn_no: string;
  txn_type: string;
  txn_type_label: string;
  account_id: string | null;
  amount: string;
  currency: string;
  status: string;
  status_label: string;
  feedback: string | null;
  operator: string | null;
  occurred_at: string | null;
}

interface Option {
  code: string;
  label: string;
}

interface ListResp {
  total: number;
  items: Row[];
  txn_type_options: Option[];
  status_options: Option[];
}

interface Filters {
  dateFrom: string;
  dateTo: string;
  txnNo: string;
  txnType: string;
  accountKeyword: string;
  status: string;
}

const EMPTY: Filters = {
  dateFrom: "",
  dateTo: "",
  txnNo: "",
  txnType: "",
  accountKeyword: "",
  status: "",
};

const PAGE_SIZES = [10, 20, 50];

function pageWindow(cur: number, pages: number): (number | "…")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const start = Math.max(2, Math.min(cur - 1, pages - 3));
  const end = Math.min(pages - 1, Math.max(cur + 1, 4));
  if (start > 2) out.push("…");
  for (let i = start; i <= end; i += 1) out.push(i);
  if (end < pages - 1) out.push("…");
  out.push(pages);
  return out;
}

/**
 * 客户门户 · 交易记录（只读）。
 *
 * 只看当前登录客户自己的广告账户充值单；客户身份由后端从登录态取，
 * 前端不传 client_id。这里没有动账按钮，充值仍去「广告账户 → 充值」发起。
 */
export default function PortalTransactions({ session }: { session: Session }) {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [types, setTypes] = useState<Option[]>([]);
  const [statuses, setStatuses] = useState<Option[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "portal",
        "/api/portal/transactions" +
          qs({
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            txn_no: applied.txnNo.trim() || undefined,
            txn_type: applied.txnType || undefined,
            account_keyword: applied.accountKeyword.trim() || undefined,
            status: applied.status || undefined,
            limit: pageSize,
            offset: (page - 1) * pageSize,
          })
      );
      if (stale()) return;
      setRows(d.items ?? []);
      setTotal(d.total ?? 0);
      // 下拉选项由后端下发，前端不维护第二份中文表
      setTypes(d.txn_type_options ?? []);
      setStatuses(d.status_options ?? []);
    } catch (e) {
      if (stale()) return;
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [applied, page, pageSize]);

  useEffect(() => {
    load();
  }, [load]);

  const pages = Math.max(1, Math.ceil(total / pageSize));

  // 筛选后总页数变少时，别让用户停在空页
  useEffect(() => {
    if (page > pages) setPage(pages);
  }, [page, pages]);

  function setField<K extends keyof Filters>(key: K, value: Filters[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function query() {
    setPage(1);
    setApplied({ ...draft });
  }

  function reset() {
    setDraft({ ...EMPTY });
    setApplied({ ...EMPTY });
    setPage(1);
  }

  async function exportCsv() {
    setErr("");
    try {
      await download(
        "portal",
        "/api/portal/transactions/export" +
          qs({
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            txn_no: applied.txnNo.trim() || undefined,
            txn_type: applied.txnType || undefined,
            account_keyword: applied.accountKeyword.trim() || undefined,
            status: applied.status || undefined,
          }),
        `交易记录-${new Date().toISOString().slice(0, 10)}.csv`
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "导出失败");
    }
  }

  return (
    <div className="card pw-ledger">
      <div className="card-head pw-filter-head">
        <h2>交易记录</h2>
        <div className="pw-filters pw-txn-filters">
          <label className="pw-date">
            <span>起始日期</span>
            <input
              type="date"
              value={draft.dateFrom}
              max={draft.dateTo || undefined}
              onChange={(e) => setField("dateFrom", e.target.value)}
            />
          </label>
          <label className="pw-date">
            <span>截止日期</span>
            <input
              type="date"
              value={draft.dateTo}
              min={draft.dateFrom || undefined}
              onChange={(e) => setField("dateTo", e.target.value)}
            />
          </label>
          <input
            className="pw-filter-input"
            placeholder="交易编号：请输入"
            value={draft.txnNo}
            onChange={(e) => setField("txnNo", e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") query();
            }}
          />
          <select
            aria-label="交易类型"
            value={draft.txnType}
            onChange={(e) => setField("txnType", e.target.value)}
          >
            <option value="">交易类型</option>
            {types.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
          <input
            className="pw-filter-input"
            placeholder="广告账户ID：请输入"
            value={draft.accountKeyword}
            onChange={(e) => setField("accountKeyword", e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") query();
            }}
          />
          <select
            aria-label="状态"
            value={draft.status}
            onChange={(e) => setField("status", e.target.value)}
          >
            <option value="">状态</option>
            {statuses.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
          <button className="btn sm" onClick={load} disabled={loading} aria-label="刷新">
            ↻ 刷新
          </button>
          <button className="btn sm" onClick={reset} disabled={loading}>
            重置
          </button>
          <button className="btn primary sm" onClick={query} disabled={loading}>
            {loading ? "查询中…" : "查询"}
          </button>
          <button className="btn sm" onClick={exportCsv}>
            导出
          </button>
        </div>
      </div>

      {err && (
        <div className="card-body" style={{ paddingBottom: 0 }}>
          <div className="err">{err}</div>
        </div>
      )}

      <div className="table-scroll">
        <table className="pw-table">
          <thead>
            <tr>
              <th>交易编号</th>
              <th>交易类型</th>
              <th>账户ID</th>
              <th className="num">交易金额</th>
              <th>状态</th>
              <th>反馈信息</th>
              <th>操作人</th>
              <th>操作时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="mono pw-txn-no">{r.txn_no}</td>
                <td>{r.txn_type_label}</td>
                <td className="mono">{r.account_id || "—"}</td>
                <td className="num mono">
                  {money(r.amount)} <span className="faint">{r.currency}</span>
                </td>
                <td>
                  <span className={`badge ${badgeClass(`TXN_${r.status}`)}`}>
                    {r.status_label}
                  </span>
                </td>
                <td
                  className={r.status === "FAILED" ? "pw-feedback bad" : "pw-feedback"}
                  title={r.feedback || ""}
                >
                  {r.feedback || "—"}
                </td>
                <td>{r.operator || "—"}</td>
                <td className="mono faint pw-time">{fmtTime(r.occurred_at)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={8}>
                  <div className="empty">
                    {loading ? "加载中…" : "没有符合条件的交易记录"}
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pw-pager">
        <span className="faint">共 {total} 条</span>
        <select
          value={pageSize}
          aria-label="每页条数"
          onChange={(e) => {
            setPageSize(Number(e.target.value));
            setPage(1);
          }}
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n} 条/页
            </option>
          ))}
        </select>
        <div className="pw-pages">
          <button
            className="pw-page"
            disabled={page <= 1 || loading}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ‹
          </button>
          {pageWindow(page, pages).map((p, i) =>
            p === "…" ? (
              <span key={`gap-${i}`} className="pw-gap">
                …
              </span>
            ) : (
              <button
                key={p}
                className={`pw-page ${p === page ? "on" : ""}`}
                onClick={() => setPage(p)}
              >
                {p}
              </button>
            )
          )}
          <button
            className="pw-page"
            disabled={page >= pages || loading}
            onClick={() => setPage((p) => Math.min(pages, p + 1))}
          >
            ›
          </button>
        </div>
        <span className="faint">共 {pages} 页</span>
      </div>
      <div className="pw-ledger-foot">
        {session.display_name}，以下仅为当前账号名下广告账户的充值交易；金额以媒体侧到账为准。
      </div>
    </div>
  );
}
