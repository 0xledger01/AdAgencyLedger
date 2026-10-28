import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, download, hasPerm, money, qs, type Session } from "../api";
import { fmtTime } from "../utils";

interface FlowItem {
  id: number;
  biz_no: string | null;
  client_id: number | null;
  client_name: string | null;
  client_no: string | null;
  agent: string | null;
  ad_account_pk: number | null;
  ad_account_id: string | null;
  txn_type: string;
  txn_type_label: string;
  is_reversal: boolean;
  reverse_of: number | null;
  direction: string;
  amount: string;
  currency: string;
  balance_before: string;
  balance_after: string;
  remark: string | null;
  operator: string | null;
  occurred_at: string;
  reversed?: boolean;
}

interface CcyTotal {
  currency: string;
  inflow: string;
  outflow: string;
  net: string;
  count: number;
}

interface ClientSummaryRow {
  currency: string;
  opening: string;
  inflow: string;
  outflow: string;
  closing: string;
  actual_closing?: string;
  balanced?: boolean;
  tx_count: number;
}

interface ClientSummary {
  client_id: number;
  client_name: string;
  client_no: string | null;
  per_currency: ClientSummaryRow[];
  has_conflict?: boolean;
  pending_reconcile?: { count: number; amount: string };
}

interface FlowResp {
  total: number;
  page: number;
  page_size: number;
  items: FlowItem[];
  txn_types: { code: string; label: string }[];
  summary: { per_currency: CcyTotal[]; client: ClientSummary | null };
}

interface SpendRow {
  id: number;
  date: string;
  client_name: string;
  account_id: string;
  platform: string;
  spend: string;
  currency: string;
  base_spend: string;
  source: string;
}

interface Filters {
  agent: string;
  billNo: string;
  clientKw: string;
  txnType: string;
  dateFrom: string;
  dateTo: string;
}

const EMPTY: Filters = {
  agent: "",
  billNo: "",
  clientKw: "",
  txnType: "",
  dateFrom: "",
  dateTo: "",
};

const PAGE_SIZES = [20, 50, 100];

/** 分页按钮：首尾页 + 当前页左右各一页，其余用 … 收掉。 */
function pageList(current: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
  const out: (number | "…")[] = [];
  const push = (v: number | "…") => {
    if (out[out.length - 1] !== v) out.push(v);
  };
  push(1);
  if (current > 3) push("…");
  for (let p = Math.max(2, current - 1); p <= Math.min(totalPages - 1, current + 1); p += 1) {
    push(p);
  }
  if (current < totalPages - 2) push("…");
  push(totalPages);
  return out;
}

/**
 * 资金流水（运营台账口径）。
 *
 * 结构按业务方给的原型：一行一笔钱，顶部四个筛选 + 查询/导出，
 * 表格列固定为 ID / 账单ID / 客户 / 代理商 / 广告账户 / 交易类型 /
 * 交易金额 / 交易前余额 / 交易后余额 / 备注 / 创建时间，底部翻页。
 *
 * 铁律：
 * - 流水只追加，按钮里没有"删除/编辑"，纠错只能冲正（双签）；
 * - 汇总按币种分开，不做跨币种合计；
 * - 核销是独立事实，不写回流水。
 */
export default function DeskLedger({ session }: { session: Session }) {
  const [tab, setTab] = useState<"flow" | "spend">("flow");

  // 筛选项分「草稿」和「已生效」两份：输入框改字不立刻打接口，点查询才生效
  const [draft, setDraft] = useState<Filters>({ ...EMPTY });
  const [applied, setApplied] = useState<Filters>({ ...EMPTY });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [jump, setJump] = useState("1");

  const [data, setData] = useState<FlowResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);

  // 账户消耗 Tab（保留原视图，不动口径）
  const [clients, setClients] = useState<{ id: number; company_name: string }[]>([]);
  const [spendClient, setSpendClient] = useState("");
  const [spendRows, setSpendRows] = useState<SpendRow[]>([]);
  const [spendSummary, setSpendSummary] = useState<{ currency: string; spend: string; days: number }[]>([]);
  const [spendTotal, setSpendTotal] = useState(0);

  // 核销 / 冲正弹窗
  const [reverseTx, setReverseTx] = useState<number | null>(null);
  const [reverseReason, setReverseReason] = useState("");
  const [reverseSecond, setReverseSecond] = useState("");
  const [reconcileOpen, setReconcileOpen] = useState(false);
  const [reconcilePeriod, setReconcilePeriod] = useState(new Date().toISOString().slice(0, 7));
  const [reconcileRemark, setReconcileRemark] = useState("");

  const canExport = hasPerm(session, "ledger.export");
  const canReconcile = hasPerm(session, "ledger.reconcile");
  const canReverse = hasPerm(session, "topup.adjust");

  const [agents, setAgents] = useState<string[]>([]);

  useEffect(() => {
    api
      .get<{ agent_channel?: { code: string; label: string }[] }>("desk", "/api/desk/enums")
      .then((d) => setAgents((d.agent_channel ?? []).map((x) => x.code)))
      .catch(() => {});
    api
      .get<{ items: any[] }>("desk", "/api/desk/clients?limit=200")
      .then((d) => setClients(d.items))
      .catch(() => {});
  }, []);

  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    setMsg("");
    setSelected([]);
    try {
      if (tab === "spend") {
        const d = await api.get<any>(
          "desk",
          "/api/desk/account-ledger" +
            qs({
              client_id: spendClient || undefined,
              date_from: applied.dateFrom || undefined,
              date_to: applied.dateTo || undefined,
              limit: 500,
            })
        );
        if (stale()) return;
        setSpendRows(d.items ?? []);
        setSpendSummary(d.summary?.per_currency ?? []);
        setSpendTotal(d.total ?? 0);
        return;
      }
      const d = await api.get<FlowResp>(
        "desk",
        "/api/desk/fund-flow" +
          qs({
            agent: applied.agent || undefined,
            bill_no: applied.billNo.trim() || undefined,
            client_keyword: applied.clientKw.trim() || undefined,
            txn_type: applied.txnType || undefined,
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            page,
            page_size: pageSize,
          })
      );
      if (stale()) return;
      setData(d);
      setJump(String(d.page));
    } catch (e: any) {
      if (stale()) return;
      setErr(e?.message || "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [tab, applied, page, pageSize, spendClient]);

  useEffect(() => {
    load();
  }, [load]);

  const total = tab === "spend" ? spendTotal : data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (tab === "spend" ? 500 : pageSize)));

  function search() {
    setApplied({ ...draft });
    setPage(1);
  }

  function reset() {
    setDraft({ ...EMPTY });
    setApplied({ ...EMPTY });
    setPage(1);
    setSpendClient("");
  }

  function goPage(p: number) {
    const next = Math.min(Math.max(1, p), totalPages);
    setPage(next);
    setJump(String(next));
  }

  function toggle(id: number) {
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  }

  const pageIds = useMemo(() => (data?.items ?? []).map((r) => r.id), [data]);
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selected.includes(id));

  function toggleAll() {
    setSelected(allChecked ? [] : pageIds);
  }

  async function doExport(fmt: "xlsx" | "csv") {
    try {
      await download(
        "desk",
        "/api/desk/fund-flow/export" +
          qs({
            agent: applied.agent || undefined,
            bill_no: applied.billNo.trim() || undefined,
            client_keyword: applied.clientKw.trim() || undefined,
            txn_type: applied.txnType || undefined,
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            fmt,
          }),
        `资金流水.${fmt}`
      );
    } catch (e: any) {
      setMsg(e?.message || "导出失败");
    }
  }

  function doReconcile() {
    if (!selected.length) return;
    // 期间必须显式指定，不能靠"没选日期就用当月"猜（F-14）
    setReconcilePeriod((applied.dateFrom || new Date().toISOString().slice(0, 7)).slice(0, 7));
    setReconcileRemark("");
    setReconcileOpen(true);
  }

  async function submitReconcile() {
    if (!selected.length) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.post<any>("desk", "/api/desk/client-ledger/reconcile", {
        tx_ids: selected,
        period: reconcilePeriod,
        remark: reconcileRemark.trim() || null,
      });
      setReconcileOpen(false);
      setMsg(`核销完成：新建 ${r.created} 条，已存在跳过 ${r.skipped} 条（期间 ${reconcilePeriod}）`);
      await load();
    } catch (e: any) {
      setMsg(e?.message || "核销失败");
    } finally {
      setBusy(false);
    }
  }

  function doReverse(id: number) {
    // 冲正需两人确认，弹窗收原因 + 第二签名
    setReverseTx(id);
    setReverseReason("");
    setReverseSecond("");
  }

  async function submitReverse() {
    if (reverseTx === null) return;
    if (!reverseReason.trim() || !reverseSecond.trim()) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.post<any>("desk", `/api/desk/client-ledger/${reverseTx}/reverse`, {
        reason: reverseReason.trim(),
        second_approver: reverseSecond.trim(),
      });
      setMsg(`已冲正，生成反向流水 #${r.id}，余额 ${money(r.balance_after)}`);
      setReverseTx(null);
      await load();
    } catch (e: any) {
      setMsg(e?.message || "冲正失败");
    } finally {
      setBusy(false);
    }
  }

  const clientSummary = data?.summary.client ?? null;
  const ccyTotals = data?.summary.per_currency ?? [];
  const txnOptions = data?.txn_types ?? [];
  const colCount = canReconcile ? 13 : 12;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div className="subtabs" style={{ margin: 0, borderBottom: "none" }}>
            <button className={tab === "flow" ? "on" : ""} onClick={() => setTab("flow")}>
              资金流水
            </button>
            <button className={tab === "spend" ? "on" : ""} onClick={() => setTab("spend")}>
              账户消耗
            </button>
          </div>
          <div className="toolbar">
            {loading && <span className="faint">加载中…</span>}
            {tab === "flow" && canExport && (
              <button className="btn sm ghost" onClick={() => doExport("csv")}>
                导出 CSV
              </button>
            )}
            {tab === "flow" && canReconcile && (
              <button
                className="btn sm"
                onClick={doReconcile}
                disabled={!selected.length || busy}
                title="把勾选的流水标记为已核销（不修改流水）"
              >
                核销选中（{selected.length}）
              </button>
            )}
          </div>
        </div>

        {tab === "flow" ? (
          <>
            <div className="flow-filters">
              <select
                value={draft.agent}
                onChange={(e) => setDraft({ ...draft, agent: e.target.value })}
                aria-label="代理商"
              >
                <option value="">请选择代理商</option>
                {agents.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
              <input
                value={draft.billNo}
                onChange={(e) => setDraft({ ...draft, billNo: e.target.value })}
                onKeyDown={(e) => e.key === "Enter" && search()}
                placeholder="账单ID：请输入"
              />
              <input
                value={draft.clientKw}
                onChange={(e) => setDraft({ ...draft, clientKw: e.target.value })}
                onKeyDown={(e) => e.key === "Enter" && search()}
                placeholder="客户名称/编号：请输入"
              />
              <select
                value={draft.txnType}
                onChange={(e) => setDraft({ ...draft, txnType: e.target.value })}
                aria-label="交易类型"
              >
                <option value="">交易类型：请选择</option>
                {txnOptions.map((o) => (
                  <option key={o.code} value={o.code}>
                    {o.label}
                  </option>
                ))}
              </select>
              <input
                type="date"
                value={draft.dateFrom}
                onChange={(e) => setDraft({ ...draft, dateFrom: e.target.value })}
                aria-label="开始日期"
              />
              <input
                type="date"
                value={draft.dateTo}
                onChange={(e) => setDraft({ ...draft, dateTo: e.target.value })}
                aria-label="结束日期"
              />
              <div className="ff-actions">
                <button className="btn primary sm" onClick={search} disabled={loading}>
                  查询
                </button>
                <button className="btn sm" onClick={reset} disabled={loading}>
                  重置
                </button>
                {canExport && (
                  <button className="btn sm" onClick={() => doExport("xlsx")}>
                    导出 Excel
                  </button>
                )}
              </div>
            </div>

            {clientSummary && (
              <div className="flow-client-summary">
                <span className="muted">
                  客户对账 · {clientSummary.client_name}
                  <span className="faint mono"> {clientSummary.client_no}</span>
                </span>
                {clientSummary.per_currency.map((c) => (
                  <span className="ccy" key={c.currency}>
                    <span className="faint">{c.currency} · 期初 → 期末</span>
                    <b>
                      {money(c.opening)} → {money(c.closing)}
                    </b>
                    <span className="faint">
                      （进 {money(c.inflow)} / 出 {money(c.outflow)} · {c.tx_count} 笔）
                    </span>
                  </span>
                ))}
                {clientSummary.pending_reconcile && clientSummary.pending_reconcile.count > 0 && (
                  <span className="faint">
                    未核销进账 {clientSummary.pending_reconcile.count} 笔 · 合计{" "}
                    {money(clientSummary.pending_reconcile.amount)}
                  </span>
                )}
                {clientSummary.has_conflict && (
                  <span className="out">恒等式不平：期初 + 本期进 − 本期出 ≠ 期末</span>
                )}
              </div>
            )}

            {msg && (
              <div className="card-body" style={{ paddingBottom: 0 }}>
                <div className="warnbox">{msg}</div>
              </div>
            )}
            {err && (
              <div className="card-body" style={{ paddingBottom: 0 }}>
                <div className="err">{err}</div>
              </div>
            )}

            <div className="table-scroll">
              <table className="flow-table">
                <thead>
                  <tr>
                    {canReconcile && (
                      <th style={{ width: 34 }}>
                        <input
                          type="checkbox"
                          checked={allChecked}
                          onChange={toggleAll}
                          aria-label="全选本页"
                        />
                      </th>
                    )}
                    <th>ID</th>
                    <th>账单ID</th>
                    <th>客户名称</th>
                    <th>代理商ID</th>
                    <th>广告账户ID</th>
                    <th>交易类型</th>
                    <th className="num">交易金额</th>
                    <th className="num">交易前余额</th>
                    <th className="num">交易后余额</th>
                    <th>备注</th>
                    <th>创建时间</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.items ?? []).map((r) => (
                    <tr key={r.id} className={selected.includes(r.id) ? "sel" : ""}>
                      {canReconcile && (
                        <td>
                          <input
                            type="checkbox"
                            checked={selected.includes(r.id)}
                            onChange={() => toggle(r.id)}
                            aria-label={`选择流水 ${r.id}`}
                          />
                        </td>
                      )}
                      <td className="mono">{r.id}</td>
                      <td className="mono faint">{r.biz_no || "—"}</td>
                      <td>
                        <div>{r.client_name || "—"}</div>
                        <div className="cell-sub mono">{r.client_no || ""}</div>
                      </td>
                      <td>
                        {r.agent ? (
                          <span className="badge blue">{r.agent}</span>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td className="mono">
                        {r.ad_account_id || <span className="faint">—</span>}
                      </td>
                      <td>
                        <span>{r.txn_type_label}</span>
                        {r.is_reversal && (
                          <span className="badge gray" style={{ marginLeft: 4 }} title={`冲正 #${r.reverse_of}`}>
                            冲正
                          </span>
                        )}
                        {r.reversed && (
                          <span className="badge gray" style={{ marginLeft: 4 }} title="该流水已被冲正">
                            已冲正
                          </span>
                        )}
                      </td>
                      <td className={`num mono ${r.direction === "IN" ? "in" : "out"}`}>
                        {money(r.amount)}
                        <span className="faint"> {r.currency}</span>
                      </td>
                      <td className="num mono">{money(r.balance_before)}</td>
                      <td className="num mono">{money(r.balance_after)}</td>
                      <td className="remark-cell" title={r.remark || ""}>
                        {r.remark || "—"}
                      </td>
                      <td className="mono faint">{fmtTime(r.occurred_at)}</td>
                      <td className="right">
                        {canReverse && (
                          <button
                            className="btn sm ghost"
                            onClick={() => doReverse(r.id)}
                            disabled={busy || r.reversed}
                            title={r.reversed ? "该流水已冲正，不得重复冲正" : "新增一条反向流水"}
                          >
                            {r.reversed ? "已冲正" : "冲正"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {(data?.items ?? []).length === 0 && !loading && (
                    <tr>
                      <td colSpan={colCount}>
                        <div className="empty">没有符合条件的流水</div>
                      </td>
                    </tr>
                  )}
                </tbody>
                {ccyTotals.length > 0 && (
                  <tfoot>
                    <tr className="sum-row">
                      <td colSpan={colCount}>
                        <div className="sum-line">
                          <span className="muted">汇总（当前筛选）</span>
                          {ccyTotals.map((c) => (
                            <span key={c.currency}>
                              {c.currency} 进 {money(c.inflow)} / 出 {money(c.outflow)} · 净{" "}
                              <span className={Number(c.net) >= 0 ? "in" : "out"}>
                                {money(c.net)}
                              </span>{" "}
                              · {c.count} 笔
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>

            <div className="client-pager">
              <span className="faint">
                共 {total} 条 · 第 {Math.min(page, totalPages)} / {totalPages} 页
              </span>
              <div className="pager-btns">
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value));
                    setPage(1);
                  }}
                  aria-label="每页条数"
                >
                  {PAGE_SIZES.map((n) => (
                    <option key={n} value={n}>
                      {n}条/页
                    </option>
                  ))}
                </select>
                <button className="btn sm" onClick={() => goPage(page - 1)} disabled={page <= 1 || loading}>
                  上一页
                </button>
                {pageList(page, totalPages).map((p, idx) =>
                  p === "…" ? (
                    <span key={`e${idx}`} className="faint">
                      …
                    </span>
                  ) : (
                    <button
                      key={p}
                      className={`btn sm ${p === page ? "primary" : ""}`}
                      onClick={() => goPage(p)}
                      disabled={loading}
                    >
                      {p}
                    </button>
                  )
                )}
                <button
                  className="btn sm"
                  onClick={() => goPage(page + 1)}
                  disabled={page >= totalPages || loading}
                >
                  下一页
                </button>
                <span className="faint">前往</span>
                <input
                  className="pager-jump"
                  value={jump}
                  onChange={(e) => setJump(e.target.value.replace(/[^\d]/g, ""))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") goPage(Number(jump) || 1);
                  }}
                  onBlur={() => goPage(Number(jump) || page)}
                  aria-label="跳转页码"
                />
                <span className="faint">页</span>
              </div>
            </div>
          </>
        ) : (
          <>
            <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
              <div className="toolbar">
                <select
                  value={spendClient}
                  onChange={(e) => {
                    setSpendClient(e.target.value);
                    setPage(1);
                  }}
                  aria-label="客户"
                >
                  <option value="">全部客户</option>
                  {clients.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.company_name}
                    </option>
                  ))}
                </select>
                <input
                  type="date"
                  value={draft.dateFrom}
                  onChange={(e) => setDraft({ ...draft, dateFrom: e.target.value })}
                  onKeyDown={(e) => e.key === "Enter" && search()}
                  aria-label="开始日期"
                />
                <span className="faint">至</span>
                <input
                  type="date"
                  value={draft.dateTo}
                  onChange={(e) => setDraft({ ...draft, dateTo: e.target.value })}
                  onKeyDown={(e) => e.key === "Enter" && search()}
                  aria-label="结束日期"
                />
                <button className="btn primary sm" onClick={search} disabled={loading}>
                  查询
                </button>
                <button className="btn sm" onClick={reset} disabled={loading}>
                  重置
                </button>
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
                    <th>日期</th>
                    <th>客户</th>
                    <th>账户 ID</th>
                    <th>平台</th>
                    <th className="num">消耗</th>
                    <th className="num">本位币</th>
                    <th>来源</th>
                  </tr>
                </thead>
                <tbody>
                  {spendRows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.date}</td>
                      <td>{r.client_name}</td>
                      <td className="mono">{r.account_id}</td>
                      <td>{r.platform}</td>
                      <td className="num mono out">
                        {money(r.spend)} <span className="faint">{r.currency}</span>
                      </td>
                      <td className="num mono muted">{money(r.base_spend)}</td>
                      <td className="muted">{r.source}</td>
                    </tr>
                  ))}
                  {spendRows.length === 0 && (
                    <tr>
                      <td colSpan={7}>
                        <div className="empty">暂无消耗记录</div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="client-pager">
              <span className="faint">共 {spendTotal} 条</span>
              <div className="pager-btns">
                {spendSummary.map((s) => (
                  <span key={s.currency} className="faint">
                    {s.currency} 合计 {money(s.spend)} · {s.days} 天
                  </span>
                ))}
              </div>
            </div>
          </>
        )}

        <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
          流水只追加、不可修改、不可删除，纠错只能冲正（需两人确认）。代理商 / 广告账户列只对广告账户类流水有值。
        </div>
      </div>

      {reconcileOpen && (
        <div className="modal-mask" onClick={() => setReconcileOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>标记核销 · {selected.length} 笔</h2>
              <button className="btn ghost sm" onClick={() => setReconcileOpen(false)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">核销是"财务事后确认"这一独立事实，不修改流水本身。</div>
              <div className="field">
                <label>核销期间（YYYY-MM，必填）</label>
                <input
                  value={reconcilePeriod}
                  onChange={(e) => setReconcilePeriod(e.target.value)}
                  placeholder="2026-10"
                  pattern="\d{4}-\d{2}"
                />
              </div>
              <div className="field">
                <label>备注（可选）</label>
                <input
                  value={reconcileRemark}
                  onChange={(e) => setReconcileRemark(e.target.value)}
                  placeholder="10 月银行流水核对"
                />
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setReconcileOpen(false)} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={submitReconcile}
                disabled={busy || !/^\d{4}-\d{2}$/.test(reconcilePeriod)}
              >
                确认核销
              </button>
            </div>
          </div>
        </div>
      )}

      {reverseTx !== null && (
        <div className="modal-mask" onClick={() => setReverseTx(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>冲正流水 #{reverseTx}</h2>
              <button className="btn ghost sm" onClick={() => setReverseTx(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                冲正只新增一条反向流水，原流水保留。需两人确认：第二签名须与当前操作人不同
                （当前：{session.display_name}）。
              </div>
              <div className="field">
                <label>冲正原因（必填）</label>
                <textarea
                  rows={2}
                  value={reverseReason}
                  onChange={(e) => setReverseReason(e.target.value)}
                  placeholder="银行退票"
                />
              </div>
              <div className="field">
                <label>第二签名（必填，须与当前操作人不同）</label>
                <input
                  value={reverseSecond}
                  onChange={(e) => setReverseSecond(e.target.value)}
                  placeholder="填写另一位审批人姓名"
                />
              </div>
              {reverseSecond.trim() !== "" && reverseSecond.trim() === session.display_name && (
                <div className="warnbox">第二签名不能填自己（{session.display_name}）。</div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setReverseTx(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn danger"
                onClick={submitReverse}
                disabled={
                  busy ||
                  !reverseReason.trim() ||
                  !reverseSecond.trim() ||
                  reverseSecond.trim() === session.display_name
                }
              >
                确认冲正
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
