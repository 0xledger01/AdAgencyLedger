import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, hasPerm, money, postForm, qs, type Session } from "../api";
import { actionLabel, badgeClass, fmtTime, stepLabel } from "../utils";

interface Row {
  id: number;
  biz_no: string | null;
  company_name: string;
  crm_name: string | null;
  crm_code: string | null;
  login_email: string | null;
  status: string;
  status_label: string;
  enabled: boolean;
  enabled_label: string;
  active: boolean;
  active_label: string;
  last_active_at: string | null;
  settle_currency: string;
  source_channel: string | null;
  sales_owner: string | null;
  ae_owner: string | null;
  wallet_balance: string;
  wallet_frozen: string;
  wallet_available: string;
  credit_limit: string;
  credit_available: string;
  monthly_spend_expect: string;
  version: number;
  reject_reason: string | null;
  created_by: string | null;
  created_at: string;
}

interface Detail {
  profile: any;
  terms: any | null;
  wallets: { id: number; currency: string; balance: string; frozen: string; available: string }[];
  accounts: any[];
  reviews: any[];
  next_step: string | null;
}

interface Filters {
  name: string;
  codes: string;
  sales_owner: string;
  ae_owner: string;
  enabled: string;
  active: string;
}

const EMPTY_FILTERS: Filters = {
  name: "",
  codes: "",
  sales_owner: "",
  ae_owner: "",
  enabled: "",
  active: "",
};

const STEP_ACTION: Record<string, string> = {
  REVIEW: "初审通过",
  RISK: "风控通过",
  LIMIT: "定额通过",
};

function enabledBadge(r: Row) {
  const cls = r.enabled ? "green" : r.status === "DISABLED" ? "red" : "gray";
  return <span className={`badge ${cls}`}>{r.enabled_label}</span>;
}

export default function DeskClients({ session }: { session: Session }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [pending, setPending] = useState<Record<string, number>>({});
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [pageMsg, setPageMsg] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [tab, setTab] = useState("profile");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  // 编辑 / 添加 / 批量 / 导入 / 重置密码
  const [editor, setEditor] = useState<{ mode: "create" } | { mode: "edit"; row: Row } | null>(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<Row | null>(null);

  // 审核驳回 / 停用启用 都用弹窗，替代 window.prompt（F-16）
  const [reviewAction, setReviewAction] = useState<"REJECT" | null>(null);
  const [reviewReason, setReviewReason] = useState("");
  const [disableTarget, setDisableTarget] = useState<boolean | null>(null);
  const [disableReason, setDisableReason] = useState("");
  // 调账（F-07 #3）
  const [adjustOpen, setAdjustOpen] = useState(false);
  const [adjustDirection, setAdjustDirection] = useState<"IN" | "OUT">("IN");
  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [adjustSecond, setAdjustSecond] = useState("");
  const [adjustRequestNo, setAdjustRequestNo] = useState("");

  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const data = await api.get<{ total: number; items: Row[]; pending: Record<string, number> }>(
        "desk",
        "/api/desk/clients" +
          qs({
            name: applied.name,
            codes: applied.codes,
            sales_owner: applied.sales_owner,
            ae_owner: applied.ae_owner,
            enabled: applied.enabled,
            active: applied.active,
            limit: pageSize,
            offset: page * pageSize,
          })
      );
      if (seq !== seqRef.current) return; // 连续查询时丢弃过期响应
      setRows(data.items);
      setTotal(data.total);
      setPending(data.pending ?? {});
    } catch (e: any) {
      if (seq === seqRef.current) setErr(e?.message || "加载失败");
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [applied, page, pageSize]);

  useEffect(() => {
    load();
  }, [load]);

  function applyFilters() {
    setPage(0);
    setSelected(new Set());
    setApplied({ ...filters });
  }

  function resetFilters() {
    setFilters(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setPage(0);
    setSelected(new Set());
  }

  async function openDetail(id: number) {
    setDetailId(id);
    setTab("profile");
    setMsg("");
    try {
      const d = await api.get<Detail>("desk", `/api/desk/clients/${id}`);
      setDetail(d);
    } catch (e: any) {
      setErr(e?.message || "加载详情失败");
    }
  }

  async function doReview(action: "APPROVE" | "REJECT") {
    if (!detailId) return;
    if (action === "REJECT") {
      setReviewAction("REJECT");
      setReviewReason("");
      return;
    }
    await submitReview("APPROVE", null);
  }

  async function submitReview(action: "APPROVE" | "REJECT", reason: string | null) {
    if (!detailId) return;
    setBusy(true);
    setMsg("");
    try {
      await api.post("desk", `/api/desk/clients/${detailId}/review`, { action, reason });
      await openDetail(detailId);
      await load();
      setReviewAction(null);
      setMsg(action === "APPROVE" ? "已通过，流转到下一步" : "已驳回");
    } catch (e: any) {
      setMsg(e?.message || "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function toggleDisable() {
    if (!detailId || !detail) return;
    const disabled = detail.profile.status !== "DISABLED";
    setDisableReason("");
    setDisableTarget(disabled);
  }

  async function submitDisable() {
    if (!detailId || !disableReason.trim()) return;
    setBusy(true);
    setMsg("");
    try {
      await api.post("desk", `/api/desk/clients/${detailId}/disable`, {
        disabled: disableTarget,
        reason: disableReason.trim(),
      });
      await openDetail(detailId);
      await load();
      setDisableTarget(null);
      setMsg(disableTarget ? "已停用" : "已启用");
    } catch (e: any) {
      setMsg(e?.message || "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveTerms(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!detailId) return;
    const fd = new FormData(e.currentTarget);
    setBusy(true);
    setMsg("");
    try {
      await api.post("desk", `/api/desk/clients/${detailId}/terms`, {
        cost_ratio: fd.get("cost_ratio"),
        rebate_rate: fd.get("rebate_rate"),
        credit_limit: fd.get("credit_limit"),
        payment_terms: Number(fd.get("payment_terms") || 0),
        channel: fd.get("channel"),
      });
      await openDetail(detailId);
      setMsg("商务条款已保存");
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitAdjust() {
    if (!detailId) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.post<any>("desk", `/api/desk/clients/${detailId}/adjust`, {
        amount: adjustAmount,
        direction: adjustDirection,
        reason: adjustReason.trim(),
        request_no: adjustRequestNo,
        second_approver: adjustSecond.trim(),
      });
      await openDetail(detailId);
      setAdjustOpen(false);
      setMsg(`调账成功：流水 #${r.tx_id}，余额 ${money(r.balance)}，可用 ${money(r.available)}`);
    } catch (e: any) {
      setMsg(e?.message || "调账失败");
    } finally {
      setBusy(false);
    }
  }

  async function doExport(fmt: "xlsx" | "csv") {
    setPageMsg("");
    setErr("");
    try {
      await download(
        "desk",
        "/api/desk/clients/export" +
          qs({
            name: applied.name,
            codes: applied.codes,
            sales_owner: applied.sales_owner,
            ae_owner: applied.ae_owner,
            enabled: applied.enabled,
            active: applied.active,
            fmt,
          }),
        `clients.${fmt}`
      );
      setPageMsg(`已导出当前筛选结果（${total} 条）`);
    } catch (e: any) {
      setErr(e?.message || "导出失败");
    }
  }

  const canEdit = hasPerm(session, "client.edit");
  const canAudit = hasPerm(session, "client.audit");
  const canSetTerms = hasPerm(session, "client.limit");
  const canDisable = hasPerm(session, "client.disable");
  const canAdjust = hasPerm(session, "topup.adjust");
  const canReset = hasPerm(session, "client.reset_password");
  const canExport = hasPerm(session, "client.export");
  const canImport = hasPerm(session, "client.import");

  const pages = Math.max(1, Math.ceil(total / pageSize));
  const pageRows = rows.map((r) => r.id);
  const allChecked = pageRows.length > 0 && pageRows.every((id) => selected.has(id));

  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allChecked) pageRows.forEach((id) => next.delete(id));
      else pageRows.forEach((id) => next.add(id));
      return next;
    });
  }

  return (
    <>
      <div className="kpis" style={{ marginBottom: 14 }}>
        <div className="kpi">
          <div className="k">待初审</div>
          <div className="v">{pending?.pending_review ?? 0}</div>
          <div className="s">销售处理</div>
        </div>
        <div className="kpi">
          <div className="k">待风控</div>
          <div className="v">{pending?.pending_risk ?? 0}</div>
          <div className="s">风控处理</div>
        </div>
        <div className="kpi">
          <div className="k">待定额度</div>
          <div className="v">{pending?.pending_limit ?? 0}</div>
          <div className="s">财务处理</div>
        </div>
        <div className="kpi">
          <div className="k">已开通</div>
          <div className="v">{pending?.active ?? 0}</div>
          <div className="s">可提交开户</div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>客户列表</h2>
          <div className="toolbar">
            {canEdit && (
              <>
                <button className="btn sm primary" onClick={() => setEditor({ mode: "create" })}>
                  ＋ 添加客户
                </button>
                <button
                  className="btn sm"
                  onClick={() => {
                    setPageMsg("");
                    if (selected.size === 0) {
                      setPageMsg("请先勾选要批量修改的客户");
                      return;
                    }
                    setBatchOpen(true);
                  }}
                >
                  批量修改
                </button>
              </>
            )}
            {canExport && (
              <button className="btn sm" onClick={() => doExport("xlsx")}>
                导出
              </button>
            )}
            {canImport && (
              <button className="btn sm" onClick={() => setImportOpen(true)}>
                导入
              </button>
            )}
          </div>
        </div>

        <div className="client-filterbar">
          <label className="cf-field">
            <span>客户名称</span>
            <input
              placeholder="请输入客户名称 / CRM 名"
              value={filters.name}
              onChange={(e) => setFilters({ ...filters, name: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && applyFilters()}
            />
          </label>
          <label className="cf-field">
            <span>客户编号</span>
            <input
              placeholder="多个编号用逗号或空格分隔"
              value={filters.codes}
              onChange={(e) => setFilters({ ...filters, codes: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && applyFilters()}
            />
          </label>
          <label className="cf-field">
            <span>负责销售</span>
            <input
              placeholder="请输入"
              value={filters.sales_owner}
              onChange={(e) => setFilters({ ...filters, sales_owner: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && applyFilters()}
            />
          </label>
          <label className="cf-field">
            <span>负责 AE</span>
            <input
              placeholder="请输入"
              value={filters.ae_owner}
              onChange={(e) => setFilters({ ...filters, ae_owner: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && applyFilters()}
            />
          </label>
          <label className="cf-field">
            <span>启用状态</span>
            <select
              value={filters.enabled}
              onChange={(e) => setFilters({ ...filters, enabled: e.target.value })}
            >
              <option value="">请选择</option>
              <option value="true">启用</option>
              <option value="false">停用</option>
              <option value="unopened">未开通</option>
            </select>
          </label>
          <label className="cf-field">
            <span>活跃状态</span>
            <select
              value={filters.active}
              onChange={(e) => setFilters({ ...filters, active: e.target.value })}
            >
              <option value="">请选择</option>
              <option value="true">活跃</option>
              <option value="false">不活跃</option>
            </select>
          </label>
          <div className="cf-actions">
            <button className="btn sm" onClick={load} disabled={loading}>
              刷新
            </button>
            <button className="btn sm primary" onClick={applyFilters}>
              查询
            </button>
            <button className="btn sm ghost" onClick={resetFilters}>
              重置
            </button>
          </div>
        </div>

        {pageMsg && (
          <div className="card-body" style={{ paddingBottom: 0 }}>
            <div className="warnbox">{pageMsg}</div>
          </div>
        )}
        {err && (
          <div className="card-body" style={{ paddingBottom: 0 }}>
            <div className="err">{err}</div>
          </div>
        )}

        <div className="client-table-wrap">
          <table className="wide">
            <thead>
              <tr>
                <th style={{ width: 30 }}>
                  <input type="checkbox" checked={allChecked} onChange={toggleAll} />
                </th>
                <th>ID</th>
                <th>登录邮箱</th>
                <th>客户 / CRM 客户</th>
                <th>负责销售</th>
                <th>负责 AE</th>
                <th className="num">可用金额</th>
                <th className="num">冻结金额</th>
                <th className="num">授信额度</th>
                <th className="num">授信余额</th>
                <th className="num">现金余额</th>
                <th>启用状态</th>
                <th>活跃状态</th>
                <th>创建人</th>
                <th>创建时间</th>
                <th className="right">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(r.id)}
                      onChange={() => toggleOne(r.id)}
                    />
                  </td>
                  <td className="mono faint">{r.id}</td>
                  <td className="mono">{r.login_email || "—"}</td>
                  <td>
                    <div className="cell-main">{r.company_name}</div>
                    <div className="cell-sub">
                      {r.crm_name || r.crm_code ? (
                        <>
                          {r.crm_name || "—"}
                          {r.crm_code && <span className="mono"> · {r.crm_code}</span>}
                        </>
                      ) : (
                        <span className="faint">未关联 CRM</span>
                      )}
                      {r.biz_no && <span className="mono faint"> · {r.biz_no}</span>}
                    </div>
                    {r.status !== "ACTIVE" && r.status !== "DISABLED" && (
                      <span className={`badge ${badgeClass(r.status)}`} style={{ marginTop: 4 }}>
                        {r.status_label}
                      </span>
                    )}
                  </td>
                  <td className="muted">{r.sales_owner || "—"}</td>
                  <td className="muted">{r.ae_owner || "—"}</td>
                  <td className="num mono">{money(r.wallet_available)}</td>
                  <td className="num mono muted">{money(r.wallet_frozen)}</td>
                  <td className="num mono">{money(r.credit_limit)}</td>
                  <td className="num mono">{money(r.credit_available)}</td>
                  <td className="num mono">
                    {money(r.wallet_balance)} <span className="faint">{r.settle_currency}</span>
                  </td>
                  <td>{enabledBadge(r)}</td>
                  <td>
                    <span className={`badge ${r.active ? "green" : "gray"}`}>{r.active_label}</span>
                  </td>
                  <td className="muted">{r.created_by || "—"}</td>
                  <td className="mono faint">{fmtTime(r.created_at)}</td>
                  <td className="right" style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm ghost" onClick={() => openDetail(r.id)}>
                      查看
                    </button>
                    {canEdit && (
                      <button
                        className="btn sm ghost"
                        onClick={() => setEditor({ mode: "edit", row: r })}
                      >
                        编辑
                      </button>
                    )}
                    {canReset && (
                      <button className="btn sm ghost" onClick={() => setResetTarget(r)}>
                        重置密码
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && !loading && (
                <tr>
                  <td colSpan={16}>
                    <div className="empty">没有符合条件的客户</div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="client-pager">
          <span className="faint">
            共 {total} 条 · 第 {page + 1}/{pages} 页
            {selected.size > 0 ? ` · 已选 ${selected.size}` : ""}
          </span>
          <div className="row">
            <select
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(0);
              }}
            >
              <option value={20}>20 条/页</option>
              <option value={50}>50 条/页</option>
              <option value={100}>100 条/页</option>
            </select>
            <button className="btn sm" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0}>
              上一页
            </button>
            <button
              className="btn sm"
              onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
              disabled={page + 1 >= pages}
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      {detail && detailId && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>{detail.profile.company_name}</h2>
                <p>
                  {detail.profile.biz_no} ·{" "}
                  <span className={`badge ${badgeClass(detail.profile.status)}`}>
                    {detail.profile.status_label}
                  </span>
                  {detail.profile.version > 1 && <span className="faint"> · 第 {detail.profile.version} 版</span>}
                </p>
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>

            <div className="drawer-body">
              {msg && <div className="warnbox">{msg}</div>}

              <div className="steps">
                {[
                  ["提交申请", "SUBMIT"],
                  ["销售初审", "REVIEW"],
                  ["风控审核", "RISK"],
                  ["财务定额", "LIMIT"],
                ].map(([label], i) => {
                  const order = ["PENDING_REVIEW", "PENDING_RISK", "PENDING_LIMIT", "ACTIVE"];
                  const idx = order.indexOf(detail.profile.status);
                  const active = detail.profile.status === "ACTIVE" ? 4 : idx;
                  const cls = i === 0 ? "done" : i <= active ? (i === active ? "now" : "done") : "";
                  return (
                    <div key={label} className={`step ${cls}`}>
                      {label}
                    </div>
                  );
                })}
              </div>

              {detail.profile.reject_reason && (
                <div className="warnbox">驳回原因：{detail.profile.reject_reason}</div>
              )}

              <div className="subtabs">
                {[
                  ["profile", "档案"],
                  ["terms", "商务条款"],
                  ["accounts", "广告账户"],
                  ["wallet", "钱包"],
                  ["reviews", "审核记录"],
                ].map(([k, l]) => (
                  <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
                    {l}
                  </button>
                ))}
              </div>

              {tab === "profile" && (
                <div className="kv">
                  <span className="k">登录邮箱</span>
                  <span className="v mono">
                    {detail.profile.login_email || "—"}
                    {detail.profile.login_active === false && (
                      <span className="badge red" style={{ marginLeft: 6 }}>
                        账号停用
                      </span>
                    )}
                  </span>
                  <span className="k">CRM 客户</span>
                  <span className="v">
                    {detail.profile.crm_name || "—"}
                    {detail.profile.crm_code && (
                      <span className="mono faint"> · {detail.profile.crm_code}</span>
                    )}
                  </span>
                  <span className="k">负责销售 / AE</span>
                  <span className="v">
                    {detail.profile.sales_owner || "—"} / {detail.profile.ae_owner || "—"}
                  </span>
                  <span className="k">统一社会信用代码</span>
                  <span className="v">{detail.profile.credit_code || "—"}</span>
                  <span className="k">注册地</span>
                  <span className="v">{detail.profile.registered_place || "—"}</span>
                  <span className="k">成立时间</span>
                  <span className="v">{detail.profile.founded_at || "—"}</span>
                  <span className="k">联系人</span>
                  <span className="v">
                    {detail.profile.contact_name || "—"} {detail.profile.phone ? `· ${detail.profile.phone}` : ""}
                  </span>
                  <span className="k">邮箱</span>
                  <span className="v">{detail.profile.email || "—"}</span>
                  <span className="k">投放平台</span>
                  <span className="v">{detail.profile.platforms || "—"}</span>
                  <span className="k">行业</span>
                  <span className="v">{detail.profile.industry || "—"}</span>
                  <span className="k">目标地区</span>
                  <span className="v">{detail.profile.target_regions || "—"}</span>
                  <span className="k">结算意向</span>
                  <span className="v">
                    {detail.profile.settle_intent === "CREDIT" ? "申请账期" : "预付"}
                  </span>
                  <span className="k">营业执照</span>
                  <span className="v">
                    {detail.profile.license_url ? (
                      <a href={detail.profile.license_url} target="_blank" rel="noreferrer">
                        查看材料
                      </a>
                    ) : (
                      "—"
                    )}
                  </span>
                  <span className="k">创建人 / 时间</span>
                  <span className="v">
                    {detail.profile.created_by || "—"} · {fmtTime(detail.profile.created_at)}
                  </span>
                  <span className="k">最近活跃</span>
                  <span className="v">{fmtTime(detail.profile.last_active_at)}</span>
                </div>
              )}

              {tab === "terms" && (
                <>
                  {detail.terms ? (
                    <form onSubmit={saveTerms}>
                      <div className="warnbox">
                        这些字段客户完全不可见：成本系数、返点率、授信、账期、上游渠道。
                      </div>
                      <div className="kv" style={{ alignItems: "center" }}>
                        <span className="k">成本系数</span>
                        <input name="cost_ratio" defaultValue={detail.terms.cost_ratio} />
                        <span className="k">返点率</span>
                        <input name="rebate_rate" defaultValue={detail.terms.rebate_rate} />
                        <span className="k">授信额度</span>
                        <input name="credit_limit" defaultValue={detail.terms.credit_limit} />
                        <span className="k">账期（天）</span>
                        <input name="payment_terms" defaultValue={detail.terms.payment_terms} />
                        <span className="k">上游渠道</span>
                        <input name="channel" defaultValue={detail.terms.channel || ""} />
                      </div>
                      <div className="row" style={{ marginTop: 14 }}>
                        <button className="btn primary" disabled={busy}>
                          保存条款
                        </button>
                        <span className="faint">成本系数 (0,1]，返点率 [0,1)</span>
                      </div>
                    </form>
                  ) : (
                    <div className="empty">当前角色无权查看商务条款</div>
                  )}
                </>
              )}

              {tab === "accounts" && (
                <table>
                  <thead>
                    <tr>
                      <th>账户 ID</th>
                      <th>平台</th>
                      <th className="num">余额</th>
                      <th className="num">日限额</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.accounts.map((a) => (
                      <tr key={a.id}>
                        <td className="mono">{a.account_id}</td>
                        <td>{a.platform}</td>
                        <td className="num mono">
                          {money(a.balance)} <span className="faint">{a.currency}</span>
                        </td>
                        <td className="num mono muted">{money(a.daily_limit)}</td>
                        <td>
                          <span className={`badge ${badgeClass(a.status)}`}>{a.status}</span>
                        </td>
                      </tr>
                    ))}
                    {detail.accounts.length === 0 && (
                      <tr>
                        <td colSpan={5}>
                          <div className="empty">暂无广告账户</div>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              )}

              {tab === "wallet" && (
                <>
                  {canAdjust && (
                    <div className="row" style={{ marginBottom: 10 }}>
                      <button
                        className="btn sm primary"
                        onClick={() => {
                          setAdjustOpen(true);
                          setAdjustDirection("IN");
                          setAdjustAmount("");
                          setAdjustReason("");
                          setAdjustSecond("");
                          setAdjustRequestNo(
                            typeof crypto !== "undefined" && crypto.randomUUID
                              ? `ADJ-${crypto.randomUUID()}`
                              : `ADJ-${Date.now()}`
                          );
                        }}
                        disabled={busy}
                      >
                        调账
                      </button>
                      <span className="faint">
                        调账需双签 + 幂等单号，新增一条 ADJUST 流水，不改历史
                      </span>
                    </div>
                  )}
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
                      {detail.wallets.map((w) => (
                        <tr key={w.id}>
                          <td>{w.currency}</td>
                          <td className="num mono">{money(w.balance)}</td>
                          <td className="num mono muted">{money(w.frozen)}</td>
                          <td className="num mono">{money(w.available)}</td>
                        </tr>
                      ))}
                      {detail.wallets.length === 0 && (
                        <tr>
                          <td colSpan={4}>
                            <div className="empty">还没有钱包（首次充值后生成）</div>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </>
              )}

              {tab === "reviews" && (
                <div className="timeline">
                  {detail.reviews.map((r) => (
                    <div key={r.id} className={`item ${r.action === "REJECT" ? "no" : "ok"}`}>
                      <div className="t">
                        {stepLabel(r.step)} · {actionLabel(r.action)} · v{r.version}
                      </div>
                      <div className="m">
                        {r.operator}（{r.operator_role}） · {fmtTime(r.created_at)}
                      </div>
                      {r.reason && <div className="m">原因：{r.reason}</div>}
                    </div>
                  ))}
                  {detail.reviews.length === 0 && <div className="empty">暂无审核记录</div>}
                </div>
              )}
            </div>

            {canAudit && detail.next_step && (
              <div className="drawer-foot">
                <button className="btn danger" onClick={() => doReview("REJECT")} disabled={busy}>
                  驳回
                </button>
                <button className="btn primary" onClick={() => doReview("APPROVE")} disabled={busy}>
                  {STEP_ACTION[detail.next_step] || "通过"}
                </button>
              </div>
            )}
            {canAudit && !detail.next_step && (
              <div className="drawer-foot">
                <span className="faint">当前状态不在审核流程中</span>
                {canDisable && detail.profile.status !== "DISABLED" && (
                  <button className="btn danger" onClick={toggleDisable} disabled={busy}>
                    停用客户
                  </button>
                )}
                {canDisable && detail.profile.status === "DISABLED" && (
                  <button className="btn" onClick={toggleDisable} disabled={busy}>
                    启用客户
                  </button>
                )}
              </div>
            )}
            {canDisable && detail.next_step && (
              <div className="drawer-foot">
                <button className="btn ghost" onClick={toggleDisable} disabled={busy}>
                  停用客户
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {editor && (
        <ClientEditor
          session={session}
          row={editor.mode === "edit" ? editor.row : null}
          onClose={() => setEditor(null)}
          onSaved={async () => {
            await load();
          }}
        />
      )}

      {batchOpen && (
        <BatchModal
          session={session}
          ids={[...selected]}
          onClose={() => setBatchOpen(false)}
          onDone={async (text) => {
            setBatchOpen(false);
            setSelected(new Set());
            setPageMsg(text);
            await load();
          }}
        />
      )}

      {importOpen && (
        <ImportModal
          onClose={() => setImportOpen(false)}
          onDone={async (text) => {
            setPageMsg(text);
            await load();
          }}
        />
      )}

      {resetTarget && (
        <ResetPasswordModal row={resetTarget} onClose={() => setResetTarget(null)} />
      )}

      {reviewAction === "REJECT" && (
        <div className="modal-mask" onClick={() => setReviewAction(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>驳回入驻申请</h2>
              <button className="btn ghost sm" onClick={() => setReviewAction(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">驳回原因客户可见，请写清楚缺什么、怎么补。</div>
              <div className="field">
                <label>驳回原因（必填）</label>
                <textarea
                  rows={3}
                  value={reviewReason}
                  onChange={(e) => setReviewReason(e.target.value)}
                  placeholder="营业执照与开户主体不一致，请重新上传"
                />
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setReviewAction(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn danger"
                onClick={() => submitReview("REJECT", reviewReason.trim())}
                disabled={busy || !reviewReason.trim()}
              >
                确认驳回
              </button>
            </div>
          </div>
        </div>
      )}

      {adjustOpen && (
        <div className="modal-mask" onClick={() => setAdjustOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>调账 · {detail?.profile.company_name}</h2>
              <button className="btn ghost sm" onClick={() => setAdjustOpen(false)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                调账会新增一条 ADJUST 流水（不改历史），需双签，幂等键来自本次请求号。
              </div>
              <div className="form-grid">
                <div>
                  <label>方向</label>
                  <select
                    value={adjustDirection}
                    onChange={(e) => setAdjustDirection(e.target.value as "IN" | "OUT")}
                  >
                    <option value="IN">增加（IN）</option>
                    <option value="OUT">扣减（OUT）</option>
                  </select>
                </div>
                <div>
                  <label>金额（正数）</label>
                  <input
                    value={adjustAmount}
                    onChange={(e) => setAdjustAmount(e.target.value)}
                    inputMode="decimal"
                    placeholder="100"
                  />
                </div>
                <div className="full">
                  <label>原因（必填）</label>
                  <input
                    value={adjustReason}
                    onChange={(e) => setAdjustReason(e.target.value)}
                    placeholder="多收退款抵扣"
                  />
                </div>
                <div className="full">
                  <label>第二签名（必填，不能是自己）</label>
                  <input
                    value={adjustSecond}
                    onChange={(e) => setAdjustSecond(e.target.value)}
                    placeholder="另一位审批人姓名"
                  />
                </div>
                <div className="full">
                  <label>幂等单号（自动生成）</label>
                  <input
                    className="mono"
                    value={adjustRequestNo}
                    onChange={(e) => setAdjustRequestNo(e.target.value)}
                  />
                </div>
              </div>
              {adjustSecond.trim() !== "" && adjustSecond.trim() === session.display_name && (
                <div className="warnbox">第二签名不能填自己（{session.display_name}）。</div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setAdjustOpen(false)} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={submitAdjust}
                disabled={
                  busy ||
                  !adjustAmount.trim() ||
                  !adjustReason.trim() ||
                  !adjustSecond.trim() ||
                  !adjustRequestNo.trim() ||
                  adjustSecond.trim() === session.display_name
                }
              >
                确认调账
              </button>
            </div>
          </div>
        </div>
      )}

      {disableTarget !== null && (
        <div className="modal-mask" onClick={() => setDisableTarget(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>{disableTarget ? "停用客户" : "启用客户"}</h2>
              <button className="btn ghost sm" onClick={() => setDisableTarget(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                {disableTarget
                  ? "停用后该客户的资金操作会被服务端拦下（非 ACTIVE 不允许动账），门户只读。"
                  : "启用后恢复为客户状态，客户可继续充值/加款。"}
              </div>
              <div className="field">
                <label>原因（必填，写进审计日志）</label>
                <textarea
                  rows={2}
                  value={disableReason}
                  onChange={(e) => setDisableReason(e.target.value)}
                  placeholder={disableTarget ? "逾期未结清款项" : "款项已结清"}
                />
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setDisableTarget(null)} disabled={busy}>
                取消
              </button>
              <button
                className={`btn ${disableTarget ? "danger" : "primary"}`}
                onClick={submitDisable}
                disabled={busy || !disableReason.trim()}
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 添加 / 编辑 */

function ClientEditor({
  session,
  row,
  onClose,
  onSaved,
}: {
  session: Session;
  row: Row | null;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const isEdit = !!row;
  const canLimit = hasPerm(session, "client.limit");
  const isSales = session.role === "SALES";
  const [form, setForm] = useState<Record<string, string>>({
    company_name: "",
    login_email: "",
    contact_name: "",
    phone: "",
    email: "",
    wechat: "",
    crm_name: "",
    crm_code: "",
    sales_owner: "",
    ae_owner: "",
    source_channel: "",
    settle_currency: "USD",
    monthly_spend_expect: "",
    credit_limit: "",
    password: "",
  });
  const [activate, setActivate] = useState(true);
  const [loading, setLoading] = useState(isEdit);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [created, setCreated] = useState<{ login: string; password: string } | null>(null);

  useEffect(() => {
    if (!row) return;
    let alive = true;
    (async () => {
      try {
        const d = await api.get<Detail>("desk", `/api/desk/clients/${row.id}`);
        if (!alive) return;
        const p = d.profile;
        setForm({
          company_name: p.company_name || "",
          login_email: p.login_email || "",
          contact_name: p.contact_name || "",
          phone: p.phone || "",
          email: p.email || "",
          wechat: p.wechat || "",
          crm_name: p.crm_name || "",
          crm_code: p.crm_code || "",
          sales_owner: p.sales_owner || "",
          ae_owner: p.ae_owner || "",
          source_channel: p.source_channel || "",
          settle_currency: p.settle_currency || "USD",
          monthly_spend_expect: p.monthly_spend_expect || "",
          credit_limit: d.terms?.credit_limit ?? "",
          password: "",
        });
      } catch (e: any) {
        setErr(e?.message || "加载客户资料失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [row]);

  function set(key: string, value: string) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.company_name.trim()) {
      setErr("客户名称必填");
      return;
    }
    if (!isEdit && !form.login_email.trim()) {
      setErr("请填写登录邮箱（客户门户账号）");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      if (isEdit && row) {
        const payload: Record<string, unknown> = {
          company_name: form.company_name.trim(),
          contact_name: form.contact_name,
          phone: form.phone,
          email: form.email,
          wechat: form.wechat,
          crm_name: form.crm_name,
          crm_code: form.crm_code,
          sales_owner: form.sales_owner,
          ae_owner: form.ae_owner,
          source_channel: form.source_channel,
          monthly_spend_expect: form.monthly_spend_expect || null,
        };
        if (form.login_email.trim()) payload.login_email = form.login_email.trim();
        if (canLimit && form.credit_limit !== "") payload.credit_limit = form.credit_limit;
        const r = await api.post<any>("desk", `/api/desk/clients/${row.id}/update`, payload);
        await onSaved();
        if (r.initial_password) {
          setCreated({ login: r.login_email || form.login_email, password: r.initial_password });
        } else {
          onClose();
        }
      } else {
        const payload: Record<string, unknown> = {
          company_name: form.company_name.trim(),
          login_email: form.login_email.trim() || null,
          contact_name: form.contact_name || null,
          phone: form.phone || null,
          email: form.email || null,
          wechat: form.wechat || null,
          crm_name: form.crm_name || null,
          crm_code: form.crm_code || null,
          sales_owner: form.sales_owner || null,
          ae_owner: form.ae_owner || null,
          source_channel: form.source_channel || null,
          settle_currency: form.settle_currency,
          monthly_spend_expect: form.monthly_spend_expect || null,
          activate: isSales ? false : activate,
        };
        if (form.password) payload.password = form.password;
        if (canLimit && form.credit_limit !== "") payload.credit_limit = form.credit_limit;
        const r = await api.post<any>("desk", "/api/desk/clients", payload);
        await onSaved();
        if (r.initial_password) {
          setCreated({ login: r.login_email || form.login_email, password: r.initial_password });
        } else {
          onClose();
        }
      }
    } catch (e: any) {
      setErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={() => !busy && onClose()}>
      <div className="modal" style={{ width: 620 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>{isEdit ? `编辑客户 · ${row?.company_name}` : "添加客户"}</h2>
          <button className="btn ghost sm" onClick={onClose} disabled={busy}>
            关闭
          </button>
        </div>
        <div className="modal-body">
          {created ? (
            <>
              <div className="warnbox">
                客户已创建。把下面这组登录信息交给客户，即可登录<b>客户门户</b>。
              </div>
              <div className="kv" style={{ marginTop: 10 }}>
                <span className="k">登录地址</span>
                <span className="v mono">{`${window.location.origin}/portal`}</span>
                <span className="k">登录邮箱</span>
                <span className="v mono">{created.login}</span>
                <span className="k">初始密码</span>
                <span className="v mono" style={{ fontSize: 15 }}>{created.password}</span>
              </div>
              <div style={{ marginTop: 12 }}>
                <CopyButton
                  text={
                    `客户门户：${window.location.origin}/portal\n` +
                    `登录邮箱：${created.login}\n初始密码：${created.password}`
                  }
                />
              </div>
            </>
          ) : loading ? (
            <div className="empty">正在加载客户资料…</div>
          ) : (
            <form onSubmit={submit} className="form-grid">
              <div className="full">
                <label>客户名称 *</label>
                <input value={form.company_name} onChange={(e) => set("company_name", e.target.value)} />
              </div>
              <div>
                <label>登录邮箱 {isEdit ? "" : "*"}</label>
                <input
                  className="mono"
                  value={form.login_email}
                  onChange={(e) => set("login_email", e.target.value)}
                  placeholder="client@example.com"
                />
              </div>
              {!isEdit && (
                <div>
                  <label>初始密码（留空用默认密码）</label>
                  <input
                    type="password"
                    value={form.password}
                    onChange={(e) => set("password", e.target.value)}
                    placeholder="留空 = 系统默认密码，创建后显示"
                  />
                </div>
              )}
              <div>
                <label>联系人</label>
                <input value={form.contact_name} onChange={(e) => set("contact_name", e.target.value)} />
              </div>
              <div>
                <label>联系电话</label>
                <input value={form.phone} onChange={(e) => set("phone", e.target.value)} />
              </div>
              <div>
                <label>联系邮箱</label>
                <input value={form.email} onChange={(e) => set("email", e.target.value)} />
              </div>
              <div>
                <label>微信</label>
                <input value={form.wechat} onChange={(e) => set("wechat", e.target.value)} />
              </div>
              <div>
                <label>CRM 客户</label>
                <input value={form.crm_name} onChange={(e) => set("crm_name", e.target.value)} />
              </div>
              <div>
                <label>CRM 编号</label>
                <input className="mono" value={form.crm_code} onChange={(e) => set("crm_code", e.target.value)} />
              </div>
              <div>
                <label>负责销售</label>
                <input value={form.sales_owner} onChange={(e) => set("sales_owner", e.target.value)} />
              </div>
              <div>
                <label>负责 AE</label>
                <input value={form.ae_owner} onChange={(e) => set("ae_owner", e.target.value)} />
              </div>
              <div>
                <label>来源渠道</label>
                <input value={form.source_channel} onChange={(e) => set("source_channel", e.target.value)} />
              </div>
              <div>
                <label>结算币种</label>
                <select
                  value={form.settle_currency}
                  onChange={(e) => set("settle_currency", e.target.value)}
                  disabled={isEdit}
                >
                  <option value="USD">USD</option>
                  <option value="HKD">HKD</option>
                </select>
              </div>
              <div>
                <label>月消耗预期</label>
                <input
                  value={form.monthly_spend_expect}
                  onChange={(e) => set("monthly_spend_expect", e.target.value)}
                  inputMode="decimal"
                />
              </div>
              <div>
                <label>
                  授信额度 {canLimit ? "" : "（财务/管理员可改）"}
                </label>
                <input
                  value={form.credit_limit}
                  onChange={(e) => set("credit_limit", e.target.value)}
                  inputMode="decimal"
                  placeholder="0"
                  disabled={!canLimit}
                />
              </div>
              {!isEdit && (
                <div className="full">
                  {isSales ? (
                    <div className="faint" style={{ fontSize: 11 }}>
                      销售添加的客户进入入驻三级审核（销售初审 → 风控 → 财务定额），
                      审核通过后才启用；「立即启用」仅运营/管理员可用。
                    </div>
                  ) : (
                    <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input
                        type="checkbox"
                        checked={activate}
                        onChange={(e) => setActivate(e.target.checked)}
                      />
                      立即启用（运营代客建档）；不勾选则进入入驻三级审核
                    </label>
                  )}
                </div>
              )}
              {isEdit && (
                <div className="full faint" style={{ fontSize: 11 }}>
                  结算币种创建后不可改；授信额度以外的资金字段请走「商务条款」。
                </div>
              )}
              {err && <div className="full err">{err}</div>}
              <div className="full row" style={{ justifyContent: "flex-end", marginTop: 4 }}>
                <button type="button" className="btn" onClick={onClose} disabled={busy}>
                  取消
                </button>
                <button className="btn primary" disabled={busy}>
                  {isEdit ? "保存修改" : "创建客户"}
                </button>
              </div>
            </form>
          )}
          {created && (
            <div className="modal-foot" style={{ paddingLeft: 0, paddingRight: 0 }}>
              <button className="btn primary" onClick={onClose}>
                我已记录，关闭
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 复制登录信息 */

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 非安全上下文（http + 非 localhost）没有 clipboard，退回选中式复制
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setDone(true);
    window.setTimeout(() => setDone(false), 1500);
  }

  return (
    <button type="button" className="btn sm" onClick={copy}>
      {done ? "已复制" : "复制登录信息"}
    </button>
  );
}

/* ------------------------------------------------------------------ 批量修改 */

function BatchModal({
  session,
  ids,
  onClose,
  onDone,
}: {
  session: Session;
  ids: number[];
  onClose: () => void;
  onDone: (text: string) => Promise<void> | void;
}) {
  const canLimit = hasPerm(session, "client.limit");
  const canDisable = hasPerm(session, "client.disable");
  const [sales, setSales] = useState("");
  const [ae, setAe] = useState("");
  const [credit, setCredit] = useState("");
  const [enabled, setEnabled] = useState("keep");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    const payload: Record<string, unknown> = { ids };
    if (sales.trim()) payload.sales_owner = sales.trim();
    if (ae.trim()) payload.ae_owner = ae.trim();
    if (canLimit && credit !== "") payload.credit_limit = credit;
    if (canDisable && enabled !== "keep") payload.enabled = enabled === "true";
    if (Object.keys(payload).length === 1) {
      setErr("至少填一项要修改的内容");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<any>("desk", "/api/desk/clients/batch", payload);
      await onDone(`已批量修改 ${r.updated} 个客户`);
    } catch (e: any) {
      setErr(e?.message || "批量修改失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={() => !busy && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>批量修改 · 已选 {ids.length} 个客户</h2>
          <button className="btn ghost sm" onClick={onClose} disabled={busy}>
            关闭
          </button>
        </div>
        <div className="modal-body">
          <div className="warnbox">留空的项不修改。授信与启用状态受角色权限限制。</div>
          <div className="form-grid" style={{ marginTop: 10 }}>
            <div>
              <label>负责销售</label>
              <input value={sales} onChange={(e) => setSales(e.target.value)} placeholder="留空不改" />
            </div>
            <div>
              <label>负责 AE</label>
              <input value={ae} onChange={(e) => setAe(e.target.value)} placeholder="留空不改" />
            </div>
            <div>
              <label>授信额度 {canLimit ? "" : "（无权限）"}</label>
              <input
                value={credit}
                onChange={(e) => setCredit(e.target.value)}
                placeholder="留空不改"
                inputMode="decimal"
                disabled={!canLimit}
              />
            </div>
            <div>
              <label>启用状态 {canDisable ? "" : "（无权限）"}</label>
              <select value={enabled} onChange={(e) => setEnabled(e.target.value)} disabled={!canDisable}>
                <option value="keep">不修改</option>
                <option value="true">启用</option>
                <option value="false">停用</option>
              </select>
            </div>
          </div>
          {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn primary" onClick={submit} disabled={busy}>
            确认修改
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 导入 */

function ImportModal({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (text: string) => Promise<void> | void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<{ updated: number; errors: { line: number; reason: string }[] } | null>(null);

  async function submit() {
    if (!file) {
      setErr("请选择 CSV 文件");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await postForm<any>("desk", "/api/desk/clients/import", fd);
      setResult(r);
      await onDone(`导入完成：更新 ${r.updated} 行，失败 ${r.error_count} 行`);
    } catch (e: any) {
      setErr(e?.message || "导入失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={() => !busy && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>导入客户资料</h2>
          <button className="btn ghost sm" onClick={onClose} disabled={busy}>
            关闭
          </button>
        </div>
        <div className="modal-body">
          <div className="warnbox">
            CSV 表头可用：客户编号 / 登录邮箱 / 客户名称 / CRM客户 / CRM编号 / 负责销售 / 负责AE / 授信额度。
            只更新已存在的客户；未匹配的行会在结果里列出，请先在页面「添加客户」。
          </div>
          <div className="field" style={{ marginTop: 10 }}>
            <label>CSV 文件（UTF-8）</label>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          {err && <div className="err">{err}</div>}
          {result && (
            <div style={{ marginTop: 10 }}>
              <div className="warnbox">
                更新 {result.updated} 行，失败 {result.errors.length} 行
              </div>
              {result.errors.length > 0 && (
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr>
                      <th>行号</th>
                      <th>原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.errors.map((e, idx) => (
                      <tr key={idx}>
                        <td className="mono">{e.line}</td>
                        <td>{e.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose} disabled={busy}>
            关闭
          </button>
          <button className="btn primary" onClick={submit} disabled={busy || !file}>
            开始导入
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 重置密码 */

function ResetPasswordModal({ row, onClose }: { row: Row; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<{ username: string; password: string; note: string } | null>(null);

  async function submit() {
    if (password && password.length < 6) {
      setErr("密码至少 6 位（留空则重置为默认密码）");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<any>("desk", `/api/desk/clients/${row.id}/reset-password`, {
        password: password || null,
      });
      setResult(r);
    } catch (e: any) {
      setErr(e?.message || "重置失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={() => !busy && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>重置密码 · {row.company_name}</h2>
          <button className="btn ghost sm" onClick={onClose} disabled={busy}>
            关闭
          </button>
        </div>
        <div className="modal-body">
          {result ? (
            <>
              <div className="warnbox">
                密码已重置。客户打开客户门户，用下面这组信息登录。
              </div>
              <div className="kv" style={{ marginTop: 10 }}>
                <span className="k">登录邮箱</span>
                <span className="v mono">{result.username}</span>
                <span className="k">新密码</span>
                <span className="v mono" style={{ fontSize: 15 }}>{result.password}</span>
              </div>
              <div style={{ marginTop: 12 }}>
                <CopyButton
                  text={
                    `客户门户：${window.location.origin}/portal\n` +
                    `登录邮箱：${result.username}\n初始密码：${result.password}`
                  }
                />
              </div>
            </>
          ) : (
            <>
              <div className="warnbox">
                {row.login_email
                  ? `将重置 ${row.login_email} 的门户登录密码。留空则重置为系统默认密码。`
                  : "该客户还没有登录账号，请先在「编辑」里补登录邮箱。"}
              </div>
              <div className="field" style={{ marginTop: 10 }}>
                <label>新密码（可选，至少 6 位）</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="留空 = 系统默认密码"
                />
              </div>
              {err && <div className="err">{err}</div>}
            </>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose} disabled={busy}>
            {result ? "关闭" : "取消"}
          </button>
          {!result && (
            <button className="btn danger" onClick={submit} disabled={busy || !row.login_email}>
              确认重置
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
