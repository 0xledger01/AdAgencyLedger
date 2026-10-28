import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, hasPerm, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface AccountCard {
  id: number;
  account_id: string;
  client_id: number;
  client_name: string;
  platform: string;
  channel: string;
  currency: string;
  currency_label: string;
  balance: string;
  status: string;
  status_label: string;
  updated_at: string | null;
}

interface ClientRow {
  client_id: number;
  client_name: string;
  currency: string;
  available: string;
}

interface Overview {
  scope: "ALL" | "OWN";
  accounts: AccountCard[];
  client_wallets?: { by_client?: ClientRow[] };
}

interface RechargeRow {
  id: number;
  biz_no: string;
  client_id: number;
  client_name: string | null;
  ad_account_id: number;
  account_id: string | null;
  platform: string | null;
  agent: string | null;
  amount: string;
  currency: string;
  credited_amount: string | null;
  remark: string | null;
  status: string;
  status_label: string;
  receipt: string | null;
  created_at: string;
  sync_at: string | null;
}

interface EnumItem {
  code: string;
  label: string;
}

const PAGE_SIZE = 20;
const CHANNEL_TONES = ["blue", "green", "amber", "red", "gray"];

/**
 * 资金总览。
 *
 * 结构按业务方给的原型：
 *   1. 顶部是一张张「广告账户钱包」卡片：代理商标签 + 客户/账户 ID + 余额 + 更新时间；
 *   2. 底部是交易明细：代理商 / 客户 / 账户 ID / 交易类型 / 状态五维筛选 + 导出。
 *
 * 铁律：**禁止跨币种求和**。卡片逐账户显示原币种余额，不再做 USD+HKD 的总数。
 * 「同步钱包」在本系统没有媒体侧同步接口，卡片上的动作是重新拉取本地账本数据，
 * 所以标成「刷新」而不是假装同步。
 */
export default function DeskOverview({ session }: { session: Session }) {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  // 账本自检面板已按要求下线，相关状态一并注释保留，需要时整块恢复。
  // const [checking, setChecking] = useState(false);
  // const [checkMsg, setCheckMsg] = useState("");
  // const [checkErr, setCheckErr] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<Overview>("desk", "/api/desk/wallets/overview");
      setData(d);
    } catch (e: any) {
      setErr(e?.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 账本自检：全量校验每个钱包的 seq 连续性与 balance_after 首尾相接。
  // 面板已按要求注释下线，函数与状态一并保留，需要时整块恢复。
  // async function runVerify() {
  //   setChecking(true);
  //   setCheckMsg("");
  //   setCheckErr("");
  //   try {
  //     const r = await api.post<any>("desk", "/api/desk/wallets/verify-all", {});
  //     setCheckMsg(
  //       `自检完成：共 ${r.total} 个钱包，通过 ${r.ok} 个${r.broken ? `，断号 ${r.broken} 个` : ""}`
  //     );
  //   } catch (e: any) {
  //     setCheckErr(e?.message || "自检失败");
  //   } finally {
  //     setChecking(false);
  //   }
  // }

  const accounts = data?.accounts ?? [];
  const negativeAvailable = (data?.client_wallets?.by_client ?? []).filter(
    (r) => Number(r.available) < 0
  );

  return (
    <>
      {err && <div className="err">{err}</div>}

      {negativeAvailable.length > 0 && (
        <div className="warnbox">
          ⚠ {negativeAvailable.length} 个客户钱包的冻结额超过余额（可用为负）：
          {negativeAvailable
            .map((r) => ` ${r.client_name} ${r.currency} ${money(r.available)}`)
            .join("，")}
          。通常是加款单冻结后余额被冲正/调账扣减所致，请核对该客户的加款单状态。
        </div>
      )}

      {/* 账户计数、币种说明与顶部刷新按钮均已按要求注释；恢复时把整块注释符去掉即可。 */}
      {/*
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="muted">共 {accounts.length} 个广告账户</span>
        <span className="faint">余额按账户币种展示，不做跨币种合计</span>
        <div className="spacer" />
        <button className="btn sm" onClick={load} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      */}

      {accounts.length === 0 ? (
        <div className="card">
          <div className="empty">暂无广告账户</div>
        </div>
      ) : (
        <div className="wallet-grid">
          {accounts.map((a) => (
            <div className="wallet-card" key={a.id}>
              <div className="wallet-card-top">
                <span className={`badge ${channelTone(a.channel)}`}>{a.channel}</span>
                {a.status !== "NORMAL" ? (
                  <span className={`badge ${badgeClass(a.status)}`}>{a.status_label}</span>
                ) : (
                  <span className="faint" style={{ fontSize: 11 }}>
                    {a.platform}
                  </span>
                )}
              </div>
              <div
                className="wallet-title"
                title={`${a.client_name} - ${a.account_id}`}
              >
                {a.client_name} - {a.account_id}
              </div>
              <div className="wallet-amount">
                {a.status === "NORMAL" ? money(a.balance) : "—"}
              </div>
              <div className="wallet-sub">
                钱包余额（{a.currency_label || a.currency}）
              </div>
              <div className="wallet-foot">
                <span>更新于 {fmtTime(a.updated_at)}</span>
                <button className="link-btn" onClick={load} disabled={loading}>
                  刷新
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {hasPerm(session, "recharge.view") && <RechargeLedger />}

      {/*
      <div className="card">
        <div className="card-head">
          <h2>账本自检</h2>
          <button className="btn sm primary" onClick={runVerify} disabled={checking}>
            {checking ? "自检中…" : "全量自检"}
          </button>
        </div>
        <div className="card-body muted">
          每个钱包的流水链（<span className="mono">seq</span> 连续、
          <span className="mono">balance_after</span> 首尾相接）逐条校验；任何断号都会报出来，不会静默通过。
          {checkMsg && (
            <div className="warnbox" style={{ marginTop: 10 }}>
              {checkMsg}
            </div>
          )}
          {checkErr && (
            <div className="err" style={{ marginTop: 10 }}>
              {checkErr}
            </div>
          )}
        </div>
      </div>
      */}

      {data?.scope === "OWN" || session.role === "SALES" ? (
        <div className="card">
          <div className="card-body faint">
            当前为销售角色：只统计自己名下客户的资金，其他人的池子不展示。
          </div>
        </div>
      ) : null}
    </>
  );
}

/**
 * 交易明细：客户钱包 → 广告账户的每一笔加款。
 * 只列加款单，不把失败/撤销写成"已到账"；到账金额只在 SUCCESS 时展示。
 */
function RechargeLedger() {
  const [agent, setAgent] = useState("");
  const [clientKw, setClientKw] = useState("");
  const [accountKw, setAccountKw] = useState("");
  const [bizType, setBizType] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState(true);

  const [rows, setRows] = useState<RechargeRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const [statusOptions, setStatusOptions] = useState<EnumItem[]>([]);
  const [agentOptions, setAgentOptions] = useState<string[]>([]);

  useEffect(() => {
    api
      .get<any>("desk", "/api/desk/enums")
      .then((d) => {
        setStatusOptions(d.recharge_status ?? []);
        setAgentOptions((d.agent_channel ?? []).map((x: EnumItem) => x.code));
      })
      .catch(() => {});
  }, []);

  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<{ total: number; items: RechargeRow[] }>(
        "desk",
        "/api/desk/recharges" +
          qs({
            agent: agent.trim() || undefined,
            client_keyword: clientKw.trim() || undefined,
            account_keyword: accountKw.trim() || undefined,
            status: status || undefined,
            limit: PAGE_SIZE,
            offset: page * PAGE_SIZE,
          })
      );
      if (stale()) return;
      setRows(d.items ?? []);
      setTotal(d.total ?? 0);
    } catch (e: any) {
      if (stale()) return;
      setErr(e?.message || "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [agent, clientKw, accountKw, status, page]);

  useEffect(() => {
    load();
  }, [load]);

  function reset() {
    setAgent("");
    setClientKw("");
    setAccountKw("");
    setBizType("");
    setStatus("");
    setPage(0);
  }

  async function doExport() {
    setErr("");
    try {
      await download(
        "desk",
        "/api/desk/recharges/export" +
          qs({
            agent: agent.trim() || undefined,
            client_keyword: clientKw.trim() || undefined,
            account_keyword: accountKw.trim() || undefined,
            status: status || undefined,
          }),
        `交易明细-${new Date().toISOString().slice(0, 10)}.csv`
      );
    } catch (e: any) {
      setErr(e?.message || "导出失败");
    }
  }

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = !!(agent || clientKw.trim() || accountKw.trim() || bizType || status);

  return (
    <div className="card">
      <div className="card-head">
        <h2>交易明细</h2>
        <div className="toolbar">
          <span className="faint">共 {total} 条</span>
          <button className="btn sm ghost" onClick={() => setOpen((v) => !v)}>
            {open ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {open && (
        <>
          <div className="card-body filter-bar">
            <div className="toolbar">
              <select
                value={agent}
                onChange={(e) => {
                  setAgent(e.target.value);
                  setPage(0);
                }}
                style={{ minWidth: 140 }}
              >
                <option value="">选择代理商</option>
                {agentOptions.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>

              <input
                value={clientKw}
                onChange={(e) => {
                  setClientKw(e.target.value);
                  setPage(0);
                }}
                placeholder="客户名称/编号"
                style={{ minWidth: 170 }}
              />

              <input
                value={accountKw}
                onChange={(e) => {
                  setAccountKw(e.target.value);
                  setPage(0);
                }}
                placeholder="广告账户ID"
                style={{ minWidth: 150 }}
              />

              <select
                value={bizType}
                onChange={(e) => {
                  setBizType(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">全部交易类型</option>
                <option value="RECHARGE">广告账户充值</option>
              </select>

              <select
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">全部状态</option>
                {statusOptions.map((o) => (
                  <option key={o.code} value={o.code}>
                    {o.label}
                  </option>
                ))}
              </select>

              <button className="btn sm" onClick={reset}>
                全部
              </button>
              <button className="btn sm" onClick={load} disabled={loading}>
                {loading ? "刷新中…" : "刷新"}
              </button>
              <button className="btn sm" onClick={doExport}>
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
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>代理商</th>
                  <th>客户名称</th>
                  <th>广告账户ID</th>
                  <th>交易类型</th>
                  <th className="num">交易金额</th>
                  <th className="num">到账金额</th>
                  <th>备注</th>
                  <th>状态</th>
                  <th>交易时间</th>
                  <th>同步时间</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className={rowClass(r.status)}>
                    <td className="mono">
                      #{r.id}
                      <div className="faint" style={{ fontSize: 11 }}>
                        {r.biz_no}
                      </div>
                    </td>
                    <td>
                      <span className={`badge ${channelTone(r.agent || "未归属")}`}>
                        {r.agent || "未归属"}
                      </span>
                    </td>
                    <td>{r.client_name || "—"}</td>
                    <td className="mono">
                      {r.account_id || "—"}
                      <div className="faint" style={{ fontSize: 11 }}>
                        {r.platform || ""}
                      </div>
                    </td>
                    <td>广告账户充值</td>
                    <td className="num mono">
                      {money(r.amount)} <span className="faint">{r.currency}</span>
                    </td>
                    <td className="num mono">
                      {r.credited_amount ? money(r.credited_amount) : "—"}
                    </td>
                    <td className="remark-cell" title={r.remark || ""}>
                      {r.remark || "—"}
                      {isMock(r.receipt) && (
                        <span className="badge gray" style={{ marginLeft: 6 }}>
                          Mock
                        </span>
                      )}
                    </td>
                    <td>
                      <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                    </td>
                    <td className="mono faint">{fmtTime(r.created_at)}</td>
                    <td className="mono faint">{fmtTime(r.sync_at)}</td>
                  </tr>
                ))}
                {rows.length === 0 && !loading && (
                  <tr>
                    <td colSpan={11}>
                      <div className="empty">
                        {filtered ? "没有符合筛选条件的交易明细" : "还没有广告账户充值交易"}
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="table-foot">
            <span className="faint">
              共 {total} 条 · 第 {Math.min(page + 1, pages)} / {pages} 页
            </span>
            <div className="row">
              <button
                className="btn sm"
                disabled={page <= 0 || loading}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                上一页
              </button>
              <button
                className="btn sm"
                disabled={page + 1 >= pages || loading}
                onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
              >
                下一页
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function channelTone(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return CHANNEL_TONES[Math.abs(hash) % CHANNEL_TONES.length];
}

function rowClass(status: string): string {
  if (status === "REJECTED") return "row-error";
  if (status === "CANCELLED") return "row-cancel";
  if (status === "TO_VERIFY") return "row-warn";
  return "";
}

function isMock(receipt: string | null): boolean {
  if (!receipt) return false;
  try {
    return JSON.parse(receipt)?.mock === true;
  } catch {
    return false;
  }
}
