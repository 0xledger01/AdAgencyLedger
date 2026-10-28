import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface AccountRow {
  id: number;
  account_id: string;
  platform: string;
  platform_label: string;
  bm_id: string | null;
  bc_ids: string[];
  timezone: string | null;
  currency: string;
  balance: string;
  daily_limit: string;
  status: string;
  status_label: string;
  subject: string | null;
  remark: string | null;
  open_request_biz_no: string | null;
  open_request_status: string | null;
  open_request_status_label: string | null;
  opened_at: string | null;
  created_at: string | null;
  last_recharge_at: string | null;
  pending_recharge_count: number;
}

interface WalletBucket {
  currency: string;
  balance: string;
  frozen: string;
  available: string;
}

interface Option {
  code: string;
  label: string;
}

interface ListResp {
  total: number;
  items: AccountRow[];
  summary: {
    by_status?: Record<string, number>;
    by_currency?: { currency: string; balance: string; count: number }[];
  };
  wallets: WalletBucket[];
  can_recharge: boolean;
  options: { platform: Option[]; status: Option[] };
}

interface RechargeRow {
  biz_no: string | null;
  amount: string;
  currency: string;
  status: string;
  status_label: string;
  created_at: string | null;
  updated_at: string | null;
}

interface DetailResp {
  account: AccountRow;
  wallet: WalletBucket | null;
  spend: { currency: string; total: string; points: { date: string; spend: string }[] };
  recharges: { total: number; items: RechargeRow[] };
  can_recharge: boolean;
}

interface Filters {
  dateFrom: string;
  dateTo: string;
  keyword: string;
  subject: string;
  platform: string;
  status: string;
}

const EMPTY_FILTERS: Filters = {
  dateFrom: "",
  dateTo: "",
  keyword: "",
  subject: "",
  platform: "",
  status: "",
};

function unitOf(ccy: string): string {
  if (ccy === "USD") return "$";
  if (ccy === "CNY") return "¥";
  if (ccy === "HKD") return "HK$";
  return ccy;
}

/** 充值金额只收 2 位小数；返回 null 表示通过。 */
function amountError(raw: string, max?: string): string | null {
  const s = raw.trim();
  if (!s) return "请输入充值金额";
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return "请输入数字，最多 2 位小数";
  const n = Number(s);
  if (!(n > 0)) return "金额必须大于 0";
  if (max !== undefined && n > Number(max)) return `不能超过可用余额 ${money(max)}`;
  return null;
}

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

function newRequestNo(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") return `pa-${c.randomUUID()}`;
  return `pa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * 幂等键按「账户」暂存在 sessionStorage：
 * 提交超时 / 响应丢失后关掉弹层再进来，仍拿同一把键，服务端识别为重试而不是新单。
 * 成功落单后清掉，下一次才是全新一笔。
 */
function requestNoKey(accountId: number): string {
  return `portal.recharge.req.${accountId}`;
}

function takeRequestNo(accountId: number): string {
  try {
    const saved = sessionStorage.getItem(requestNoKey(accountId));
    if (saved) return saved;
    const fresh = newRequestNo();
    sessionStorage.setItem(requestNoKey(accountId), fresh);
    return fresh;
  } catch {
    // 隐私模式下 sessionStorage 可能不可用，退化为页面内幂等
    return newRequestNo();
  }
}

function clearRequestNo(accountId: number) {
  try {
    sessionStorage.removeItem(requestNoKey(accountId));
  } catch {
    /* ignore */
  }
}

export default function PortalAccounts({ session }: { session: Session }) {
  const [rows, setRows] = useState<AccountRow[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<ListResp["summary"]>({});
  const [wallets, setWallets] = useState<WalletBucket[]>([]);
  const [canRecharge, setCanRecharge] = useState(false);
  const [options, setOptions] = useState<ListResp["options"]>({ platform: [], status: [] });

  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [detail, setDetail] = useState<{
    loading: boolean;
    error?: string;
    data?: DetailResp;
  } | null>(null);
  const [modal, setModal] = useState<AccountRow | null>(null);

  // 连续 load 时按序号丢弃过期响应（项目约定：防请求竞态）
  const seqRef = useRef(0);

  const load = useCallback(async (q: Filters, p: number, ps: number) => {
    const seq = ++seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "portal",
        "/api/portal/ad-accounts" +
          qs({
            date_from: q.dateFrom || undefined,
            date_to: q.dateTo || undefined,
            keyword: q.keyword.trim() || undefined,
            subject: q.subject.trim() || undefined,
            platform: q.platform || undefined,
            status: q.status || undefined,
            limit: ps,
            offset: (p - 1) * ps,
          })
      );
      if (seq !== seqRef.current) return;
      setRows(d.items ?? []);
      setTotal(Number(d.total ?? 0));
      setSummary(d.summary ?? {});
      setWallets(d.wallets ?? []);
      setCanRecharge(!!d.can_recharge);
      setOptions(d.options ?? { platform: [], status: [] });
    } catch (e: any) {
      if (seq === seqRef.current) setErr(e?.message || "加载失败");
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(applied, page, pageSize);
  }, [load, applied, page, pageSize]);

  function applyFilters(e?: React.FormEvent) {
    e?.preventDefault();
    setPage(1);
    setApplied({ ...draft });
  }

  function resetFilters() {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setPage(1);
  }

  async function exportCsv() {
    try {
      await download(
        "portal",
        "/api/portal/ad-accounts/export" +
          qs({
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            keyword: applied.keyword.trim() || undefined,
            subject: applied.subject.trim() || undefined,
            platform: applied.platform || undefined,
            status: applied.status || undefined,
          }),
        "我的广告账户.csv"
      );
    } catch (e: any) {
      setMsg(e?.message || "导出失败");
    }
  }

  async function openDetail(account: AccountRow) {
    setDetail({ loading: true });
    try {
      const d = await api.get<DetailResp>("portal", `/api/portal/ad-accounts/${account.id}`);
      setDetail({ loading: false, data: d });
    } catch (e: any) {
      setDetail({ loading: false, error: e?.message || "详情加载失败" });
    }
  }

  function walletOf(ccy: string): WalletBucket | null {
    return wallets.find((w) => w.currency === ccy) ?? null;
  }

  const pages = Math.max(1, Math.ceil(total / pageSize));
  const balanceLines = summary.by_currency ?? [];
  const pendingCount = rows.reduce((n, r) => n + (r.pending_recharge_count || 0), 0);

  return (
    <>
      {err && <div className="err">{err}</div>}
      {msg && <div className="warnbox">{msg}</div>}

      <div className="kpis compact" style={{ marginBottom: 14 }}>
        <div className="kpi">
          <div className="k">广告账户</div>
          <div className="v">{total}</div>
          <div className="s">受当前筛选影响</div>
        </div>
        <div className="kpi">
          <div className="k">账户余额</div>
          <div className="v">
            {balanceLines.length === 0 && "—"}
            {balanceLines.map((b) => (
              <div key={b.currency} style={{ fontSize: 15 }}>
                {money(b.balance)} <span className="faint">{b.currency}</span>
              </div>
            ))}
          </div>
          <div className="s">按币种分开合计，不跨币种相加</div>
        </div>
        <div className="kpi">
          <div className="k">现金可用</div>
          <div className="v">
            {wallets.length === 0 && "—"}
            {wallets.map((w) => (
              <div key={w.currency} style={{ fontSize: 15 }}>
                {money(w.available)} <span className="faint">{w.currency}</span>
              </div>
            ))}
          </div>
          <div className="s">可用于给广告账户充值</div>
        </div>
        <div className="kpi">
          <div className="k">处理中充值</div>
          <div className="v">{pendingCount}</div>
          <div className="s">已冻结，等待运营执行到账</div>
        </div>
      </div>

      <div className="card pw-ledger">
        <div className="card-head pw-filter-head">
          <h2>广告账户</h2>
          <div className="pw-filters">
            <label className="pw-date">
              <span>开户开始</span>
              <input
                type="date"
                value={draft.dateFrom}
                onChange={(e) => setDraft({ ...draft, dateFrom: e.target.value })}
              />
            </label>
            <label className="pw-date">
              <span>开户结束</span>
              <input
                type="date"
                value={draft.dateTo}
                onChange={(e) => setDraft({ ...draft, dateTo: e.target.value })}
              />
            </label>
            <input
              className="pw-filter-input"
              placeholder="账户ID / BC ID：请输入"
              value={draft.keyword}
              onChange={(e) => setDraft({ ...draft, keyword: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyFilters();
              }}
            />
            <input
              className="pw-filter-input"
              placeholder="开户主体：关键字查询"
              value={draft.subject}
              onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyFilters();
              }}
            />
            <select
              value={draft.platform}
              onChange={(e) => setDraft({ ...draft, platform: e.target.value })}
            >
              <option value="">全部媒体</option>
              {options.platform.map((o) => (
                <option key={o.code} value={o.code}>
                  {o.label}
                </option>
              ))}
            </select>
            <select
              value={draft.status}
              onChange={(e) => setDraft({ ...draft, status: e.target.value })}
            >
              <option value="">全部状态</option>
              {options.status.map((o) => (
                <option key={o.code} value={o.code}>
                  {o.label}
                </option>
              ))}
            </select>
            <button className="btn primary sm" onClick={applyFilters} disabled={loading}>
              查询
            </button>
            <button className="btn sm" onClick={resetFilters} disabled={loading}>
              重置
            </button>
            <button className="btn sm" onClick={exportCsv}>
              导出
            </button>
          </div>
        </div>

        <div className="table-scroll">
          <table className="pw-table">
            <thead>
              <tr>
                <th>媒体</th>
                <th>账户</th>
                <th className="num">账户余额</th>
                <th>状态</th>
                <th>开户审核</th>
                <th>时区</th>
                <th>开户主体</th>
                <th>备注</th>
                <th>最近充值</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className="badge gray">{r.platform_label}</span>
                  </td>
                  <td>
                    <div className="mono">{r.account_id}</div>
                    <div className="faint" style={{ fontSize: 11 }}>
                      {r.bc_ids.length
                        ? `BC ${r.bc_ids.join("、")}`
                        : r.bm_id
                          ? `BC ${r.bm_id}`
                          : "—"}
                    </div>
                  </td>
                  <td className="num mono">
                    {money(r.balance)}
                    <div className="faint" style={{ fontSize: 11 }}>
                      {r.currency}
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                  </td>
                  <td>
                    {r.open_request_status ? (
                      <span
                        className={`badge ${badgeClass(r.open_request_status)}`}
                        title={r.open_request_biz_no || ""}
                      >
                        {r.open_request_status_label || r.open_request_status}
                      </span>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="muted">{r.timezone || "—"}</td>
                  <td>
                    <div className="cell-main">{r.subject || "—"}</div>
                    {r.open_request_biz_no && (
                      <div className="cell-sub mono">{r.open_request_biz_no}</div>
                    )}
                  </td>
                  <td className="muted remark-cell" title={r.remark || ""}>
                    {r.remark || "—"}
                  </td>
                  <td className="mono faint pw-time">
                    {r.last_recharge_at ? fmtTime(r.last_recharge_at) : "—"}
                    {r.pending_recharge_count > 0 && (
                      <div>
                        <span className="badge blue">处理中 {r.pending_recharge_count}</span>
                      </div>
                    )}
                  </td>
                  <td className="right">
                    <div className="acc-btns">
                      <button className="btn sm" onClick={() => openDetail(r)}>
                        明细
                      </button>
                      <button
                        className="btn sm primary"
                        disabled={!canRecharge || r.status === "BANNED"}
                        title={
                          !canRecharge
                            ? "客户未开通或已停用，暂不能充值"
                            : r.status === "BANNED"
                              ? "账户已封停，不能充值"
                              : "从现金账户发起充值，运营执行到账"
                        }
                        onClick={() => setModal(r)}
                      >
                        充值
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && !loading && (
                <tr>
                  <td colSpan={10}>
                    <div className="empty">没有符合条件的广告账户</div>
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
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(1);
            }}
          >
            {[10, 20, 50].map((n) => (
              <option key={n} value={n}>
                {n} 条/页
              </option>
            ))}
          </select>
          <div className="pw-pages">
            <button
              className="pw-page"
              disabled={page <= 1}
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
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              ›
            </button>
          </div>
          <span className="faint">共 {pages} 页</span>
        </div>
        <div className="pw-ledger-foot">
          账户余额是媒体侧可用预算，不计入资金总账户；充值从现金账户扣款，提交后金额冻结，运营执行后到账。
        </div>
      </div>

      {detail && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>{detail.data?.account.account_id || "账户明细"}</h2>
                {detail.data && (
                  <p>
                    {detail.data.account.platform_label} ·{" "}
                    <span className={`badge ${badgeClass(detail.data.account.status)}`}>
                      {detail.data.account.status_label}
                    </span>
                    <span className="faint">
                      {" "}
                      · {detail.data.account.timezone || "时区未登记"}
                    </span>
                  </p>
                )}
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>
            <div className="drawer-body">
              {detail.loading && <div className="empty">加载中…</div>}
              {detail.error && <div className="err">{detail.error}</div>}
              {detail.data && (
                <>
                  <div className="kv">
                    <span className="k">账户 ID</span>
                    <span className="v mono">{detail.data.account.account_id}</span>
                    <span className="k">媒体</span>
                    <span className="v">{detail.data.account.platform_label}</span>
                    <span className="k">账户余额</span>
                    <span className="v mono">
                      {money(detail.data.account.balance)} {detail.data.account.currency}
                    </span>
                    <span className="k">日限额</span>
                    <span className="v mono">
                      {money(detail.data.account.daily_limit)} {detail.data.account.currency}
                    </span>
                    <span className="k">时区</span>
                    <span className="v">{detail.data.account.timezone || "—"}</span>
                    <span className="k">开户主体</span>
                    <span className="v">{detail.data.account.subject || "—"}</span>
                    <span className="k">BC ID</span>
                    <span className="v mono">
                      {detail.data.account.bc_ids.length
                        ? detail.data.account.bc_ids.join("、")
                        : detail.data.account.bm_id || "—"}
                    </span>
                    <span className="k">开户单号</span>
                    <span className="v mono">
                      {detail.data.account.open_request_biz_no || "—"}
                    </span>
                    <span className="k">开户审核</span>
                    <span className="v">
                      {detail.data.account.open_request_status ? (
                        <span
                          className={`badge ${badgeClass(
                            detail.data.account.open_request_status
                          )}`}
                        >
                          {detail.data.account.open_request_status_label ||
                            detail.data.account.open_request_status}
                        </span>
                      ) : (
                        "—"
                      )}
                    </span>
                    <span className="k">开户时间</span>
                    <span className="v mono">{fmtTime(detail.data.account.opened_at)}</span>
                    <span className="k">备注</span>
                    <span className="v">{detail.data.account.remark || "—"}</span>
                  </div>

                  <div className="sub-title">资金</div>
                  <div className="kv">
                    <span className="k">现金可用</span>
                    <span className="v mono">
                      {detail.data.wallet
                        ? `${money(detail.data.wallet.available)} ${detail.data.wallet.currency}`
                        : "—"}
                    </span>
                    <span className="k">冻结中</span>
                    <span className="v mono">
                      {detail.data.wallet
                        ? `${money(detail.data.wallet.frozen)} ${detail.data.wallet.currency}`
                        : "—"}
                    </span>
                  </div>

                  <div className="sub-title">近 30 天消耗</div>
                  <div className="kv">
                    <span className="k">合计消耗</span>
                    <span className="v mono out">
                      {money(detail.data.spend.total)} {detail.data.spend.currency}
                    </span>
                  </div>
                  <table style={{ marginTop: 10 }}>
                    <thead>
                      <tr>
                        <th>日期</th>
                        <th className="num">消耗</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...detail.data.spend.points].reverse().map((p) => (
                        <tr key={p.date}>
                          <td className="mono">{p.date}</td>
                          <td className="num mono out">{money(p.spend)}</td>
                        </tr>
                      ))}
                      {detail.data.spend.points.length === 0 && (
                        <tr>
                          <td colSpan={2}>
                            <div className="empty">近 30 天没有消耗记录</div>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>

                  <div className="sub-title">最近充值（共 {detail.data.recharges.total} 笔）</div>
                  <table>
                    <thead>
                      <tr>
                        <th>单号</th>
                        <th className="num">金额</th>
                        <th>状态</th>
                        <th>提交时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.recharges.items.map((r) => (
                        <tr key={r.biz_no || `${r.created_at}-${r.amount}`}>
                          <td className="mono faint">{r.biz_no || "—"}</td>
                          <td className="num mono">
                            {money(r.amount)} <span className="faint">{r.currency}</span>
                          </td>
                          <td>
                            <span className={`badge ${badgeClass(r.status)}`}>
                              {r.status_label}
                            </span>
                          </td>
                          <td className="mono faint">{fmtTime(r.created_at)}</td>
                        </tr>
                      ))}
                      {detail.data.recharges.items.length === 0 && (
                        <tr>
                          <td colSpan={4}>
                            <div className="empty">还没有充值记录</div>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setDetail(null)}>
                关闭
              </button>
              {detail.data && (
                <button
                  className="btn primary"
                  disabled={!detail.data.can_recharge}
                  onClick={() => {
                    const account = detail.data!.account;
                    setDetail(null);
                    setModal(account);
                  }}
                >
                  充值
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {modal && (
        <AccountRechargeModal
          session={session}
          account={modal}
          available={walletOf(modal.currency)?.available ?? "0"}
          onClose={() => setModal(null)}
          onDone={async (text) => {
            setModal(null);
            setMsg(text);
            await load(applied, page, pageSize);
          }}
        />
      )}
    </>
  );
}

function AccountRechargeModal({
  session,
  account,
  available,
  onClose,
  onDone,
}: {
  session: Session;
  account: AccountRow;
  available: string;
  onClose: () => void;
  onDone: (text: string) => Promise<void>;
}) {
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [touched, setTouched] = useState(false);
  // 幂等键在弹层打开时取（同账户重开复用），失败重试沿用同一把（网络中断也不会重复冻结）
  const [requestNo, setRequestNo] = useState(() => takeRequestNo(account.id));

  const unit = unitOf(account.currency);
  const amountErr = amountError(amount, available);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (amountErr) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<any>(
        "portal",
        `/api/portal/ad-accounts/${account.id}/recharge`,
        // 金额按字符串原样提交，避免 JS float 破坏 2 位小数
        { amount: amount.trim(), request_no: requestNo }
      );
      await onDone(
        r.deduped
          ? `该请求已提交过，返回原单 ${r.biz_no}（未重复冻结）`
          : `充值申请 ${r.biz_no} 已提交：${money(r.amount)} ${r.currency} 已冻结，` +
              `待运营执行到账（当前状态：${r.status_label}）`
      );
      // 成功后清掉暂存键，下一次是全新一笔
      clearRequestNo(account.id);
      setRequestNo(newRequestNo());
    } catch (e: any) {
      setErr(e?.message || "提交失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div className="modal pw-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>广告账户充值</h2>
          <button type="button" className="pw-close" onClick={onClose} title="关闭">
            ×
          </button>
        </div>
        <form className="modal-body" onSubmit={submit}>
          <div className="kv" style={{ marginBottom: 16 }}>
            <span className="k">广告账户</span>
            <span className="v mono">{account.account_id}</span>
            <span className="k">媒体 / 时区</span>
            <span className="v">
              {account.platform_label} · {account.timezone || "时区未登记"}
            </span>
            <span className="k">账户余额</span>
            <span className="v mono">
              {money(account.balance)} {account.currency}
            </span>
            <span className="k">现金可用</span>
            <span className="v mono">
              {money(available)} {account.currency}
            </span>
          </div>

          <div className="pw-field">
            <label className="pw-label">
              <span className="pw-req">*</span> 充值金额（{account.currency}）
            </label>
            <div className={`pw-amount-input ${touched && amountErr ? "bad" : ""}`}>
              <span className="pw-amount-unit">{unit}</span>
              <input
                value={amount}
                inputMode="decimal"
                placeholder="请输入(最多2位小数)"
                onChange={(e) => setAmount(e.target.value)}
                onBlur={() => setTouched(true)}
                autoFocus
              />
            </div>
            {touched && amountErr && <div className="pw-field-err">{amountErr}</div>}
            <div className="pw-quote-hint">
              从现金账户扣款；提交后金额立即冻结，运营执行到账前不扣减钱包余额。
            </div>
          </div>

          <div className="pw-assist">
            <div className="pw-assist-title">充值流程</div>
            <div className="pw-assist-sub">
              1. 提交申请（金额冻结） → 2. 运营执行加款 → 3. 广告账户余额增加，
              可在充值记录查看进度。
            </div>
            <div className="pw-form-row">
              <label className="pw-form-label">操作人</label>
              <div className="muted">{session.display_name}</div>
            </div>
          </div>

          {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}

          <div className="modal-foot">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button className="btn primary" disabled={busy}>
              {busy ? "提交中…" : "确认充值"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
