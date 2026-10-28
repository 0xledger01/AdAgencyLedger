import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, hasPerm, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";
import DeskRecharge from "./DeskRecharge";

interface AccountRow {
  id: number;
  account_id: string;
  platform: string;
  platform_label: string;
  bm_id: string | null;
  timezone: string | null;
  currency: string;
  balance: string;
  daily_limit: string;
  status: string;
  status_label: string;
  authorized: boolean;
  authorized_label: string;
  channel: string;
  client_id: number;
  client_name: string;
  client_biz_no: string | null;
  subject: string | null;
  open_request_biz_no: string | null;
  open_request_status: string | null;
  open_request_status_label: string | null;
  remark: string | null;
  opened_at: string | null;
  created_at: string;
}

interface ListResp {
  total: number;
  items: AccountRow[];
  summary: {
    by_status?: Record<string, number>;
    by_currency?: { currency: string; balance: string; count: number }[];
  };
}

interface RechargeRow {
  id: number;
  biz_no: string;
  amount: string;
  currency: string;
  status: string;
  status_label: string;
  serial_no: string | null;
  remark: string | null;
  created_at: string;
}

interface DetailResp {
  account: AccountRow;
  client: {
    id: number;
    biz_no: string | null;
    company_name: string;
    status: string;
    status_label: string;
    contact_name: string | null;
    phone: string | null;
    email: string | null;
    industry: string | null;
    registered_place: string | null;
    sales_owner: string | null;
    ae_owner: string | null;
    channel: string | null;
    settle_currency: string;
  };
  wallets: { id: number; currency: string; balance: string; frozen: string; available: string }[];
  open_request: {
    id: number;
    biz_no: string | null;
    status_label: string;
    subject: string | null;
    timezone: string | null;
    bm_id: string | null;
    applicant: string | null;
    apply_count: number;
    completed_count: number;
    remark: string | null;
    approved_at: string | null;
    processed_at: string | null;
  } | null;
  recharges: { total: number; items: RechargeRow[] };
  recharge_summary: {
    total?: number;
    by_status?: Record<string, number>;
    success_by_currency?: { currency: string; amount: string; count: number }[];
    last_success_at?: string | null;
  };
}

interface CreateResp extends RechargeRow {
  deduped?: boolean;
  account_id: string;
  platform: string;
}

interface ExecResp {
  id: number;
  status: string;
  status_label: string;
  serial_no?: string | null;
  account_balance?: string;
}

// 当前只做 TikTok：筛选只给 TikTok。历史 FB / Google 账户仍按原数据展示。
const PLATFORMS: [string, string][] = [["TIKTOK", "TikTok"]];

const STATUSES: [string, string][] = [
  ["NORMAL", "正常"],
  ["LIMITED", "受限"],
  ["BANNED", "封停"],
];

function newRequestNo(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") return `rc-${c.randomUUID()}`;
  return `rc-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 列表里的时间只留「月-日 时:分」，完整值放 title，避免撑爆列宽 */
function fmtShortTime(v?: string | null): string {
  if (!v) return "—";
  const s = v.replace("T", " ");
  return s.length >= 16 ? s.slice(5, 16) : s;
}

type RechargeModal = { account: AccountRow } | null;
type Detail = { loading: boolean; error?: string; data?: DetailResp };

export default function DeskAccounts({ session }: { session: Session }) {
  const [tab, setTab] = useState<"accounts" | "recharges">("accounts");

  const [rows, setRows] = useState<AccountRow[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<ListResp["summary"]>({});
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const [keyword, setKeyword] = useState("");
  const [clientKeyword, setClientKeyword] = useState("");
  const [agent, setAgent] = useState("");
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState("");

  // 提交中的筛选条件：输入框改动后点「查询」才生效，避免每个字符都打接口
  const [query, setQuery] = useState({ keyword: "", clientKeyword: "", agent: "", platform: "", status: "" });

  const [detail, setDetail] = useState<Detail | null>(null);
  const [modal, setModal] = useState<RechargeModal>(null);
  const [amount, setAmount] = useState("");
  const [remark, setRemark] = useState("");
  const [executeNow, setExecuteNow] = useState(true);
  const [requestNo, setRequestNo] = useState(newRequestNo);
  const [busy, setBusy] = useState(false);
  const [modalMsg, setModalMsg] = useState("");
  const [modalErr, setModalErr] = useState("");

  const canRecharge = hasPerm(session, "recharge.view");
  const canExec = hasPerm(session, "recharge.exec");
  const canExport = hasPerm(session, "ad_account.view");

  const seqRef = useRef(0);

  const load = useCallback(async (q: typeof query = query) => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "desk",
        "/api/desk/ad-accounts" +
          qs({
            keyword: q.keyword || undefined,
            client_keyword: q.clientKeyword || undefined,
            agent: q.agent || undefined,
            platform: q.platform || undefined,
            status: q.status || undefined,
            limit: 200,
          })
      );
      if (stale()) return;
      setRows(d.items);
      setTotal(d.total);
      setSummary(d.summary ?? {});
    } catch (e) {
      if (stale()) return;
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    load(query);
  }, [load, query]);

  function submitFilters(e?: React.FormEvent) {
    e?.preventDefault();
    setQuery({ keyword, clientKeyword, agent, platform, status });
  }

  function resetFilters() {
    setKeyword("");
    setClientKeyword("");
    setAgent("");
    setPlatform("");
    setStatus("");
    setQuery({ keyword: "", clientKeyword: "", agent: "", platform: "", status: "" });
  }

  async function exportCsv() {
    setErr("");
    try {
      await download(
        "desk",
        "/api/desk/ad-accounts/export" +
          qs({
            keyword: query.keyword || undefined,
            client_keyword: query.clientKeyword || undefined,
            agent: query.agent || undefined,
            platform: query.platform || undefined,
            status: query.status || undefined,
          }),
        `广告账户-${Date.now()}.csv`
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "导出失败");
    }
  }

  async function openDetail(account: AccountRow) {
    setDetail({ loading: true });
    try {
      const d = await api.get<DetailResp>("desk", `/api/desk/ad-accounts/${account.id}`);
      setDetail({ loading: false, data: d });
    } catch (e) {
      setDetail({ loading: false, error: e instanceof Error ? e.message : "详情加载失败" });
    }
  }

  function openRecharge(account: AccountRow) {
    setModal({ account });
    setAmount("");
    setRemark("");
    setExecuteNow(canExec);
    setRequestNo(newRequestNo());
    setModalMsg("");
    setModalErr("");
  }

  async function doRecharge() {
    if (!modal) return;
    setBusy(true);
    setModalErr("");
    setModalMsg("");
    try {
      const created = await api.post<CreateResp>("desk", "/api/desk/recharges", {
        ad_account_id: modal.account.id,
        amount,
        remark: remark || null,
        request_no: requestNo,
      });
      let note = created.deduped
        ? "该请求已提交过，返回原单（没有重复冻结）"
        : `已发起充值 ${money(created.amount)} ${created.currency}，金额已冻结占位`;
      if (executeNow && canExec && created.status === "PENDING_EXEC") {
        const r = await api.post<ExecResp>("desk", `/api/desk/recharges/${created.id}/exec`, {
          outcome: "SUCCESS",
        });
        note = `充值完成：${r.status_label}${
          r.account_balance ? ` · 账户余额 ${money(r.account_balance)}` : ""
        }${r.serial_no ? ` · 流水号 ${r.serial_no}` : ""}`;
      } else if (!executeNow) {
        note += "；到「充值记录」里执行或核查";
      }
      setModalMsg(note);
      setRequestNo(newRequestNo());
      setAmount("");
      setRemark("");
      await load(query);
      if (detail?.data?.account.id === modal.account.id) {
        const d = await api.get<DetailResp>("desk", `/api/desk/ad-accounts/${modal.account.id}`);
        setDetail({ loading: false, data: d });
      }
    } catch (e) {
      // 失败后换新幂等键：否则改了金额重试会被后端当成同一笔去重
      setRequestNo(newRequestNo());
      setModalErr(e instanceof Error ? e.message : "充值失败");
    } finally {
      setBusy(false);
    }
  }

  const balanceLines = summary.by_currency ?? [];
  const normalCount = summary.by_status?.NORMAL ?? 0;
  const riskyCount = (summary.by_status?.LIMITED ?? 0) + (summary.by_status?.BANNED ?? 0);

  return (
    <>
      <div className="subtabs">
        <button className={tab === "accounts" ? "on" : ""} onClick={() => setTab("accounts")}>
          广告账户
        </button>
        <button className={tab === "recharges" ? "on" : ""} onClick={() => setTab("recharges")}>
          充值记录
        </button>
      </div>

      {tab === "recharges" ? (
        <DeskRecharge session={session} />
      ) : (
        <>
          <div className="kpis compact" style={{ marginBottom: 14 }}>
            <div className="kpi">
              <div className="k">广告账户</div>
              <div className="v">{total}</div>
              <div className="s">受当前筛选影响</div>
            </div>
            <div className="kpi">
              <div className="k">正常 / 异常</div>
              <div className="v">
                {normalCount} <span className="faint" style={{ fontSize: 13 }}>/ {riskyCount}</span>
              </div>
              <div className="s">受限与封停需要处理</div>
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
          </div>

          <div className="card">
            <div className="card-head">
              <h2>账户列表</h2>
              <div className="toolbar">
                <span className="faint">共 {total} 个账户</span>
                <button className="btn sm" onClick={() => load(query)} disabled={loading}>
                  刷新
                </button>
                {canExport && (
                  <button className="btn sm" onClick={exportCsv}>
                    导出
                  </button>
                )}
              </div>
            </div>

            <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
              <form className="toolbar acc-filter" onSubmit={submitFilters}>
                <input
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                  placeholder="广告账户ID / BC ID"
                  style={{ width: 170 }}
                />
                <input
                  value={clientKeyword}
                  onChange={(e) => setClientKeyword(e.target.value)}
                  placeholder="客户名称/编号"
                  style={{ width: 170 }}
                />
                <input
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                  placeholder="代理商"
                  style={{ width: 130 }}
                />
                <select value={platform} onChange={(e) => setPlatform(e.target.value)} style={{ width: 130 }}>
                  <option value="">全部平台</option>
                  {PLATFORMS.map(([code, label]) => (
                    <option key={code} value={code}>
                      {label}
                    </option>
                  ))}
                </select>
                <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 120 }}>
                  <option value="">全部状态</option>
                  {STATUSES.map(([code, label]) => (
                    <option key={code} value={code}>
                      {label}
                    </option>
                  ))}
                </select>
                <button className="btn primary" disabled={loading}>
                  查询
                </button>
                <button type="button" className="btn" onClick={resetFilters} disabled={loading}>
                  重置
                </button>
              </form>
              {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
            </div>

            <div className="table-wrap accounts-wrap">
              <table className="accounts">
                <colgroup>
                  <col style={{ width: "3%" }} />
                  <col style={{ width: "16%" }} />
                  <col style={{ width: "9.5%" }} />
                  <col style={{ width: "16%" }} />
                  <col style={{ width: "6.5%" }} />
                  <col style={{ width: "9%" }} />
                  <col style={{ width: "9%" }} />
                  <col style={{ width: "6%" }} />
                  <col style={{ width: "6.5%" }} />
                  <col style={{ width: "8.5%" }} />
                  <col style={{ width: "10%" }} />
                </colgroup>
                <thead>
                  <tr>
                    <th className="acc-id">ID</th>
                    <th className="acc-account">广告账户</th>
                    <th>申请单号</th>
                    <th className="acc-client">客户名称</th>
                    <th>代理商</th>
                    <th>BC ID</th>
                    <th className="num acc-balance">账户余额</th>
                    <th>状态</th>
                    <th>授权状态</th>
                    <th>开户时间</th>
                    <th className="acc-actions"></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono faint acc-id">{r.id}</td>
                      <td className="acc-account">
                        <div className="mono">{r.account_id}</div>
                        <div className="faint" style={{ fontSize: 11 }}>
                          {r.platform_label} · {r.timezone || "时区未登记"}
                        </div>
                      </td>
                      <td className="mono faint acc-no" title={r.open_request_biz_no || ""}>
                        {r.open_request_biz_no || "—"}
                      </td>
                      <td className="acc-client">
                        <div>{r.client_name}</div>
                        <div className="faint mono" style={{ fontSize: 11 }}>
                          {r.client_biz_no || ""}
                        </div>
                      </td>
                      <td className="faint">{r.channel}</td>
                      <td className="mono faint" title={r.bm_id || ""}>
                        {r.bm_id || "—"}
                      </td>
                      <td className="num mono acc-balance">
                        <div>{money(r.balance)}</div>
                        <div className="faint" style={{ fontSize: 11 }}>
                          {r.currency}
                        </div>
                      </td>
                      <td>
                        <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                      </td>
                      <td>
                        <span className={`badge ${r.authorized ? "green" : "red"}`}>{r.authorized_label}</span>
                      </td>
                      <td className="mono faint" title={fmtTime(r.opened_at || r.created_at)}>
                        {fmtShortTime(r.opened_at || r.created_at)}
                      </td>
                      <td className="right acc-actions">
                        <div className="acc-btns">
                          <button className="btn sm" onClick={() => openDetail(r)}>
                            详情
                          </button>
                          {canRecharge && r.status !== "BANNED" && (
                            <button
                              className="btn sm primary"
                              onClick={() => openRecharge(r)}
                              title="从客户钱包冻结并充值到该广告账户"
                            >
                              充值
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 && !loading && (
                    <tr>
                      <td colSpan={11}>
                        <div className="empty">没有符合条件的广告账户</div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {detail && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>{detail.data?.account.account_id || "账户详情"}</h2>
                {detail.data && (
                  <p>
                    {detail.data.account.platform_label} ·{" "}
                    <span className={`badge ${badgeClass(detail.data.account.status)}`}>
                      {detail.data.account.status_label}
                    </span>
                    <span className="faint"> · {detail.data.account.timezone || "时区未登记"}</span>
                  </p>
                )}
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>

            <div className="drawer-body">
              {detail.loading && <div className="empty">正在加载账户详情…</div>}
              {detail.error && <div className="err">{detail.error}</div>}
              {detail.data && (
                <>
                  <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>账户信息</h3>
                  <div className="kv" style={{ marginBottom: 18 }}>
                    <span className="k">账户 ID</span>
                    <span className="v mono">{detail.data.account.account_id}</span>
                    <span className="k">账户余额</span>
                    <span className="v mono">
                      {money(detail.data.account.balance)} {detail.data.account.currency}
                    </span>
                    <span className="k">日限额</span>
                    <span className="v mono">{money(detail.data.account.daily_limit)}</span>
                    <span className="k">BC ID</span>
                    <span className="v mono">{detail.data.account.bm_id || "—"}</span>
                    <span className="k">代理商</span>
                    <span className="v">{detail.data.account.channel}</span>
                    <span className="k">时区</span>
                    <span className="v mono">{detail.data.account.timezone || "—"}</span>
                    <span className="k">授权状态</span>
                    <span className="v">
                      <span className={`badge ${detail.data.account.authorized ? "green" : "red"}`}>
                        {detail.data.account.authorized_label}
                      </span>
                    </span>
                    <span className="k">开户时间</span>
                    <span className="v mono">{fmtTime(detail.data.account.opened_at)}</span>
                    <span className="k">备注</span>
                    <span className="v">{detail.data.account.remark || "—"}</span>
                  </div>

                  <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>客户</h3>
                  <div className="kv" style={{ marginBottom: 18 }}>
                    <span className="k">客户名称</span>
                    <span className="v">
                      {detail.data.client.company_name}{" "}
                      <span className={`badge ${badgeClass(detail.data.client.status)}`}>
                        {detail.data.client.status_label}
                      </span>
                    </span>
                    <span className="k">客户编号</span>
                    <span className="v mono">{detail.data.client.biz_no || "—"}</span>
                    <span className="k">联系人</span>
                    <span className="v">
                      {detail.data.client.contact_name || "—"} {detail.data.client.phone || ""}
                    </span>
                    <span className="k">行业 / 主体</span>
                    <span className="v">
                      {detail.data.client.industry || "—"} / {detail.data.client.registered_place || "—"}
                    </span>
                    <span className="k">销售 / AE</span>
                    <span className="v">
                      {detail.data.client.sales_owner || "—"} / {detail.data.client.ae_owner || "—"}
                    </span>
                  </div>

                  <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>客户钱包</h3>
                  <div className="table-wrap" style={{ marginBottom: 18 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>币种</th>
                          <th className="num">余额</th>
                          <th className="num">冻结</th>
                          <th className="num">可用</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.data.wallets.map((w) => (
                          <tr key={w.id}>
                            <td className="mono">{w.currency}</td>
                            <td className="num mono">{money(w.balance)}</td>
                            <td className="num mono">{money(w.frozen)}</td>
                            <td className="num mono">{money(w.available)}</td>
                          </tr>
                        ))}
                        {detail.data.wallets.length === 0 && (
                          <tr>
                            <td colSpan={4}>
                              <div className="empty">该客户还没有钱包（先走充值管理入账）</div>
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>

                  <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>开户单</h3>
                  <div className="kv" style={{ marginBottom: 18 }}>
                    {detail.data.open_request ? (
                      <>
                        <span className="k">申请单号</span>
                        <span className="v mono">{detail.data.open_request.biz_no || "—"}</span>
                        <span className="k">状态</span>
                        <span className="v">{detail.data.open_request.status_label}</span>
                        <span className="k">开户主体</span>
                        <span className="v">{detail.data.open_request.subject || "—"}</span>
                        <span className="k">下户进度</span>
                        <span className="v mono">
                          {detail.data.open_request.completed_count} / {detail.data.open_request.apply_count}
                        </span>
                        <span className="k">申请人</span>
                        <span className="v">{detail.data.open_request.applicant || "—"}</span>
                        <span className="k">备注</span>
                        <span className="v">{detail.data.open_request.remark || "—"}</span>
                      </>
                    ) : (
                      <>
                        <span className="k">关联开户单</span>
                        <span className="v faint">无（历史导入账户）</span>
                      </>
                    )}
                  </div>

                  {canRecharge && (
                    <>
                      <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>最近充值记录</h3>
                      {(detail.data.recharge_summary.success_by_currency ?? []).length > 0 && (
                        <div className="faint" style={{ marginBottom: 8 }}>
                          累计成功：
                          {(detail.data.recharge_summary.success_by_currency ?? [])
                            .map((x) => `${money(x.amount)} ${x.currency}（${x.count} 笔）`)
                            .join(" · ")}
                          {detail.data.recharge_summary.last_success_at
                            ? ` · 最近 ${fmtTime(detail.data.recharge_summary.last_success_at)}`
                            : ""}
                        </div>
                      )}
                      <div className="table-wrap">
                        <table>
                          <thead>
                            <tr>
                              <th>单号</th>
                              <th className="num">金额</th>
                              <th>状态</th>
                              <th>流水号</th>
                              <th>时间</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(detail.data.recharges.items ?? []).map((r) => (
                              <tr key={r.id}>
                                <td className="mono faint">{r.biz_no}</td>
                                <td className="num mono">
                                  {money(r.amount)} <span className="faint">{r.currency}</span>
                                </td>
                                <td>
                                  <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                                </td>
                                <td className="mono faint">{r.serial_no || "—"}</td>
                                <td className="mono faint">{fmtTime(r.created_at)}</td>
                              </tr>
                            ))}
                            {(detail.data.recharges.items ?? []).length === 0 && (
                              <tr>
                                <td colSpan={5}>
                                  <div className="empty">这个账户还没有充值记录</div>
                                </td>
                              </tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}
                </>
              )}
            </div>

            {detail.data && canRecharge && detail.data.account.status !== "BANNED" && (
              <div className="drawer-foot">
                <button className="btn" onClick={() => setDetail(null)}>
                  关闭
                </button>
                <button className="btn primary" onClick={() => openRecharge(detail.data!.account)}>
                  充值
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {modal && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>账户充值 · {modal.account.account_id}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="kv" style={{ marginBottom: 12 }}>
                <span className="k">客户</span>
                <span className="v">{modal.account.client_name}</span>
                <span className="k">当前余额</span>
                <span className="v mono">
                  {money(modal.account.balance)} {modal.account.currency}
                </span>
              </div>
              <div className="field">
                <label>充值金额（{modal.account.currency}）</label>
                <input
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="0.00"
                  inputMode="decimal"
                  autoFocus
                />
              </div>
              <div className="field">
                <label>备注（可选）</label>
                <input value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="客户追加预算" />
              </div>
              {canExec && (
                <label className="row" style={{ gap: 6, marginBottom: 8 }}>
                  <input
                    type="checkbox"
                    checked={executeNow}
                    onChange={(e) => setExecuteNow(e.target.checked)}
                  />
                  <span>确认后立即执行（模拟媒体到账，真正从客户钱包出账）</span>
                </label>
              )}
              <div className="warnbox">
                第一步先从客户钱包冻结金额；执行成功才解冻并出账、加到媒体侧余额。
                {!canExec && " 你没有执行权限，发起后需由运营/管理员在「充值记录」里执行。"}
              </div>
              {modalMsg && <div className="warnbox" style={{ marginTop: 10 }}>{modalMsg}</div>}
              {modalErr && <div className="err" style={{ marginTop: 10 }}>{modalErr}</div>}
              <div className="faint mono" style={{ marginTop: 8, fontSize: 11 }}>
                本次幂等键 request_no：{requestNo}
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                关闭
              </button>
              <button
                className="btn primary"
                onClick={doRecharge}
                disabled={busy || !amount.trim()}
              >
                {executeNow && canExec ? "确认充值" : "发起充值"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
