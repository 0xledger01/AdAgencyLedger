import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface Row {
  id: number;
  txn_no: string;
  biz_no: string | null;
  txn_type: string;
  txn_type_label: string;
  client_id: number;
  client_name: string | null;
  ad_account_id: number;
  account_id: string | null;
  platform: string | null;
  agent: string | null;
  amount: string;
  currency: string;
  remark: string | null;
  status: string;
  status_label: string;
  status_raw: string;
  feedback: string | null;
  operator: string | null;
  occurred_at: string | null;
}

interface EnumItem {
  code: string;
  label: string;
}

interface Filters {
  date_from: string;
  date_to: string;
  txn_no: string;
  txn_type: string;
  account_keyword: string;
  status: string;
}

const EMPTY: Filters = {
  date_from: "",
  date_to: "",
  txn_no: "",
  txn_type: "",
  account_keyword: "",
  status: "",
};

const PAGE_SIZES = [20, 50, 100];

function trim(f: Filters): Filters {
  return {
    date_from: f.date_from,
    date_to: f.date_to,
    txn_no: f.txn_no.trim(),
    txn_type: f.txn_type,
    account_keyword: f.account_keyword.trim(),
    status: f.status,
  };
}

/**
 * 广告账户交易记录（只读）。
 *
 * 与「充值记录」的分工：
 * - 充值记录 = 操作台，执行 / 核查 / 重试 / 撤销都在那里；
 * - 这里只看交易流水和媒体侧反馈，不提供任何动账按钮。
 *
 * 交易编号（ADTR…）与业务单号（RC…）不是一个东西：客服报号看前者，
 * 财务对单据看后者，所以搜索框两个都认。
 */
export default function DeskTransactions({ session }: { session: Session }) {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [jump, setJump] = useState("");
  const [types, setTypes] = useState<EnumItem[]>([]);
  const [statuses, setStatuses] = useState<EnumItem[]>([]);
  const seqRef = useRef(0);

  // 下拉选项来自后端枚举，不在前端维护第二份中文表
  useEffect(() => {
    api
      .get<{ txn_record_type?: EnumItem[]; txn_record_status?: EnumItem[] }>(
        "desk",
        "/api/desk/enums"
      )
      .then((d) => {
        setTypes(d.txn_record_type ?? []);
        setStatuses(d.txn_record_status ?? []);
      })
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<{ total: number; items: Row[] }>(
        "desk",
        "/api/desk/transactions" +
          qs({
            date_from: applied.date_from || undefined,
            date_to: applied.date_to || undefined,
            txn_no: applied.txn_no || undefined,
            txn_type: applied.txn_type || undefined,
            account_keyword: applied.account_keyword || undefined,
            status: applied.status || undefined,
            limit: pageSize,
            offset: page * pageSize,
          })
      );
      if (stale()) return;
      setRows(d.items ?? []);
      setTotal(d.total ?? 0);
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

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  function setField<K extends keyof Filters>(key: K, value: Filters[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function query() {
    setApplied(trim(draft));
    setPage(0);
  }

  function reset() {
    setDraft({ ...EMPTY });
    setApplied({ ...EMPTY });
    setPage(0);
    setJump("");
  }

  function goPage(next: number) {
    const target = Math.min(Math.max(1, next), totalPages);
    setPage(target - 1);
  }

  async function doExport() {
    setErr("");
    try {
      await download(
        "desk",
        "/api/desk/transactions/export" +
          qs({
            date_from: applied.date_from || undefined,
            date_to: applied.date_to || undefined,
            txn_no: applied.txn_no || undefined,
            txn_type: applied.txn_type || undefined,
            account_keyword: applied.account_keyword || undefined,
            status: applied.status || undefined,
          }),
        `交易记录-${new Date().toISOString().slice(0, 10)}.csv`
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "导出失败");
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>交易记录</h2>
        <div className="toolbar">
          <span className="faint">共 {total} 笔</span>
        </div>
      </div>

      <div className="card-body">
        <div className="filterbar txn-filterbar">
          <div className="filter-field">
            <input
              type="date"
              aria-label="开始日期"
              title="开始日期"
              value={draft.date_from}
              max={draft.date_to || undefined}
              onChange={(e) => setField("date_from", e.target.value)}
            />
          </div>
          <div className="filter-field">
            <input
              type="date"
              aria-label="结束日期"
              title="结束日期"
              value={draft.date_to}
              min={draft.date_from || undefined}
              onChange={(e) => setField("date_to", e.target.value)}
            />
          </div>
          <div className="filter-field">
            <input
              value={draft.txn_no}
              onChange={(e) => setField("txn_no", e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") query();
              }}
              placeholder="交易编号：请输入"
            />
          </div>
          <div className="filter-field">
            <select
              aria-label="交易类型"
              value={draft.txn_type}
              onChange={(e) => setField("txn_type", e.target.value)}
            >
              <option value="">交易类型</option>
              {types.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          <div className="filter-field">
            <input
              value={draft.account_keyword}
              onChange={(e) => setField("account_keyword", e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") query();
              }}
              placeholder="广告账户ID：请输入"
            />
          </div>
          <div className="filter-field">
            <select
              aria-label="状态"
              value={draft.status}
              onChange={(e) => setField("status", e.target.value)}
            >
              <option value="">状态</option>
              {statuses.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
          <div className="filter-field filter-actions">
            <button
              className="btn sm"
              onClick={load}
              disabled={loading}
              title="按当前条件重新加载"
              aria-label="刷新"
            >
              ↻ 刷新
            </button>
            <button className="btn sm" onClick={reset} disabled={loading}>
              重置
            </button>
            <button className="btn sm primary" onClick={query} disabled={loading}>
              {loading ? "查询中…" : "查询"}
            </button>
            <button className="btn sm" onClick={doExport}>
              导出
            </button>
          </div>
        </div>
      </div>

      {err && (
        <div className="card-body" style={{ paddingBottom: 0 }}>
          <div className="err">{err}</div>
        </div>
      )}

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>交易编号</th>
              <th>交易类型</th>
              <th>账户ID</th>
              <th className="num">交易金额</th>
              <th>操作备注</th>
              <th>状态</th>
              <th>反馈信息</th>
              <th>操作人</th>
              <th>操作时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="mono" title={`业务单号 ${r.biz_no || "—"}`}>
                  {r.txn_no}
                </td>
                <td>{r.txn_type_label}</td>
                <td className="mono">
                  {r.account_id || "—"}
                  {r.agent && (
                    <div className="faint" style={{ fontSize: 11 }}>
                      {r.agent}
                    </div>
                  )}
                </td>
                <td className="num mono">
                  {money(r.amount)} <span className="faint">{r.currency}</span>
                </td>
                <td className="muted" title={r.remark || ""}>
                  {r.remark || "—"}
                </td>
                <td>
                  {/* 交易状态是执行口径（PENDING/DONE/…），与充值单状态同名前缀不同含义，
                      加 TXN_ 前缀避免与工单的 PENDING 撞色 */}
                  <span className={`badge ${badgeClass(`TXN_${r.status}`)}`}>
                    {r.status_label}
                  </span>
                </td>
                <td
                  className={r.status === "FAILED" ? "txn-feedback bad" : "txn-feedback"}
                  title={r.feedback || ""}
                >
                  {r.feedback || "—"}
                </td>
                <td>{r.operator || "—"}</td>
                <td className="mono faint">{fmtTime(r.occurred_at)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={9}>
                  <div className="empty">{loading ? "加载中…" : "没有符合条件的交易"}</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="client-pager">
        <span className="faint">
          共 {total} 条 · 第 {Math.min(page + 1, totalPages)} / {totalPages} 页
        </span>
        <div className="pager-btns">
          <select
            value={pageSize}
            aria-label="每页条数"
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(0);
            }}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}条/页
              </option>
            ))}
          </select>
          <button className="btn sm" onClick={() => goPage(page)} disabled={page <= 0 || loading}>
            上一页
          </button>
          <button
            className="btn sm"
            onClick={() => goPage(page + 2)}
            disabled={page + 1 >= totalPages || loading}
          >
            下一页
          </button>
          <span className="faint">前往</span>
          <input
            className="pager-jump"
            aria-label="跳转页码"
            value={jump}
            onChange={(e) => setJump(e.target.value.replace(/[^\d]/g, ""))}
            onKeyDown={(e) => {
              if (e.key === "Enter") goPage(Number(jump) || 1);
            }}
            onBlur={() => goPage(Number(jump) || page + 1)}
          />
          <span className="faint">页</span>
        </div>
      </div>
    </div>
  );
}
