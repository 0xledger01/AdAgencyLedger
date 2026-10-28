import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, hasPerm, money, qs, type Session } from "../api";
import { AuthedThumb } from "../components/AuthedFile";
import { badgeClass, fmtTime } from "../utils";

interface Item {
  id: number;
  biz_no: string;
  client_id: number;
  client_name: string | null;
  client_code: string | null;
  channel: string | null;
  source: string;
  source_label: string;
  applicant: string | null;
  platform: string;
  platform_label: string;
  bm_id: string | null;
  subject: string | null;
  apply_count: number;
  completed_count: number;
  open_method: string;
  open_method_label: string;
  timezone: string | null;
  currency: string;
  daily_limit: string;
  status: string;
  status_label: string;
  approver: string | null;
  handler: string | null;
  approved_at: string | null;
  processed_at: string | null;
  reject_reason: string | null;
  remark: string | null;
  agent_receipt: string | null;
  // 客户申请单材料（P8）：审批五项核对要看的原始提交内容
  license_url: string | null;
  credit_code: string | null;
  registered_place: string | null;
  industry: string | null;
  target_country: string | null;
  bc_ids: string[];
  product_link: string | null;
  account_ids: string[];
  created_at: string;
}

interface ListResp {
  total: number;
  items: Item[];
  summary: Record<string, number>;
  pending_total: number;
}

interface ClientProfile {
  id: number;
  company_name: string;
  credit_code: string | null;
  registered_place: string | null;
  founded_at: string | null;
  industry: string | null;
  platforms: string | null;
  website: string | null;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  status: string;
  status_label: string;
  license_url: string | null;
  idcard_url: string | null;
  qualification_url: string | null;
}

interface Detail {
  item: Item;
  client: ClientProfile | null;
  existing_accounts: {
    id: number;
    account_id: string;
    platform: string;
    status: string;
    currency: string;
  }[];
}

type CheckKey = "subject" | "industry" | "bm" | "duplicate" | "channel";

const CHECK_ITEMS: [CheckKey, string][] = [
  ["subject", "主体一致性（营业执照 / 开户主体一致）"],
  ["industry", "行业可投（不属于禁投行业）"],
  ["bm", "BM 归属（BM 未绑定其它代理）"],
  ["duplicate", "是否重复开户（同客户同平台无重复账户）"],
  ["channel", "渠道匹配（上游渠道与客户渠道一致）"],
];

const EMPTY_CHECKS: Record<CheckKey, boolean> = {
  subject: false,
  industry: false,
  bm: false,
  duplicate: false,
  channel: false,
};

interface Filters {
  dateFrom: string;
  dateTo: string;
  channel: string;
  platform: string;
  bizNo: string;
  clientQ: string;
  subject: string;
  handler: string;
  accountId: string;
}

const EMPTY_FILTERS: Filters = {
  dateFrom: "",
  dateTo: "",
  channel: "",
  platform: "",
  bizNo: "",
  clientQ: "",
  subject: "",
  handler: "",
  accountId: "",
};

export default function DeskOpen({ session }: { session: Session }) {
  const [tab, setTab] = useState<"all" | "pending">("all");
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [pendingTotal, setPendingTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const [statusOptions, setStatusOptions] = useState<[string, string][]>([]);
  const [platformOptions, setPlatformOptions] = useState<[string, string][]>([]);
  const [channelOptions, setChannelOptions] = useState<string[]>([]);
  const [countryOptions, setCountryOptions] = useState<[string, string][]>([]);
  const [industryOptions, setIndustryOptions] = useState<[string, string][]>([]);
  const [placeOptions, setPlaceOptions] = useState<[string, string][]>([]);
  const [clients, setClients] = useState<{ id: number; company_name: string }[]>([]);

  const [detailId, setDetailId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState("");
  const [checks, setChecks] = useState<Record<CheckKey, boolean>>(EMPTY_CHECKS);
  const [approveRemark, setApproveRemark] = useState("");
  const [manualIds, setManualIds] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [receiptResult, setReceiptResult] = useState<"OPENED" | "AGENT_REJECTED">("OPENED");
  const [receiptIds, setReceiptIds] = useState("");
  const [receiptReason, setReceiptReason] = useState("");

  const [createOpen, setCreateOpen] = useState(false);
  const [fClient, setFClient] = useState("");
  const [fPlatform, setFPlatform] = useState("TIKTOK");
  const [fSubject, setFSubject] = useState("");
  const [fApplyCount, setFApplyCount] = useState("1");
  const [fBmId, setFBmId] = useState("");
  const [fTimezone, setFTimezone] = useState("Asia/Shanghai");
  const [fCurrency, setFCurrency] = useState("USD");
  const [fDailyLimit, setFDailyLimit] = useState("1000");
  const [fRemark, setFRemark] = useState("");

  const canApprove = hasPerm(session, "open_request.approve");
  const canSubmit = hasPerm(session, "open_request.submit");
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "desk",
        "/api/desk/open-requests" +
          qs({
            tab,
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            channel: applied.channel || undefined,
            platform: applied.platform || undefined,
            biz_no: applied.bizNo || undefined,
            client_q: applied.clientQ || undefined,
            subject: applied.subject || undefined,
            handler: applied.handler || undefined,
            account_id: applied.accountId || undefined,
            limit: 200,
          })
      );
      if (stale()) return;
      setItems(d.items);
      setTotal(d.total);
      setPendingTotal(d.pending_total ?? 0);
    } catch (e) {
      if (!stale()) setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [tab, applied]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api
      .get<{
        open_request_status: { code: string; label: string }[];
        platform: { code: string; label: string }[];
        agent_channel: { code: string; label: string }[];
        open_country?: { code: string; label: string }[];
        open_industry?: { code: string; label: string }[];
        registered_place?: { code: string; label: string }[];
      }>("desk", "/api/desk/enums")
      .then((d) => {
        setStatusOptions(d.open_request_status.map((x) => [x.code, x.label]));
        setPlatformOptions(d.platform.map((x) => [x.code, x.label]));
        setChannelOptions(d.agent_channel.map((x) => x.code));
        setCountryOptions((d.open_country ?? []).map((x) => [x.code, x.label]));
        setIndustryOptions((d.open_industry ?? []).map((x) => [x.code, x.label]));
        setPlaceOptions((d.registered_place ?? []).map((x) => [x.code, x.label]));
      })
      .catch(() => {});
    api
      .get<{ items: { id: number; company_name: string }[] }>("desk", "/api/desk/clients?limit=200")
      .then((d) => setClients(d.items ?? []))
      .catch(() => {});
  }, []);

  const loadDetail = useCallback(async (id: number) => {
    setDetailLoading(true);
    setDetailErr("");
    try {
      const d = await api.get<Detail>("desk", `/api/desk/open-requests/${id}`);
      setDetail(d);
    } catch (e) {
      setDetailErr(e instanceof Error ? e.message : "详情加载失败");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  function openDetail(id: number) {
    setDetailId(id);
    setDetail(null);
    setChecks(EMPTY_CHECKS);
    setApproveRemark("");
    setManualIds("");
    setRejecting(false);
    setRejectReason("");
    setReceiptResult("OPENED");
    setReceiptIds("");
    setReceiptReason("");
    setMsg("");
    setErr("");
    loadDetail(id);
  }

  function closeDetail() {
    setDetailId(null);
    setDetail(null);
  }

  const allChecked = useMemo(() => CHECK_ITEMS.every(([k]) => checks[k]), [checks]);

  /** 申请单里的国家/行业/注册地存的是代码，展示时翻成中文。 */
  const optLabel = (opts: [string, string][], code: string | null) =>
    (code ? opts.find(([c]) => c === code)?.[1] : "") || code || "—";

  async function doApprove(autoComplete: boolean) {
    if (!detail) return;
    const ids = autoComplete
      ? manualIds.split(/[\n,，;；]/).map((x) => x.trim()).filter(Boolean)
      : [];
    if (ids.length && ids.length !== detail.item.apply_count) {
      setErr(`账户 ID 数量（${ids.length}）与申请开户数（${detail.item.apply_count}）不一致`);
      return;
    }
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      await api.post("desk", `/api/desk/open-requests/${detail.item.id}/approve`, {
        checks,
        remark: approveRemark || null,
        auto_complete: autoComplete,
        account_ids: ids,
      });
      setMsg(autoComplete ? "审批通过，已自动开户" : "审批通过，等待提交一代");
      await load();
      await loadDetail(detail.item.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "审批失败");
    } finally {
      setBusy(false);
    }
  }

  async function doReject() {
    if (!detail || !rejectReason.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await api.post("desk", `/api/desk/open-requests/${detail.item.id}/reject`, {
        reason: rejectReason.trim(),
      });
      setMsg("已驳回");
      await load();
      await loadDetail(detail.item.id);
      setRejecting(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "驳回失败");
    } finally {
      setBusy(false);
    }
  }

  async function doSubmit(item: Item | null) {
    if (!item) return;
    setBusy(true);
    setErr("");
    try {
      await api.post("desk", `/api/desk/open-requests/${item.id}/submit`);
      setMsg("已提交到一代，等待回执");
      await load();
      if (detailId === item.id) await loadDetail(item.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }

  async function doReceipt() {
    if (!detail) return;
    setBusy(true);
    setErr("");
    try {
      const ids = receiptIds.split(/[\n,，;；]/).map((x) => x.trim()).filter(Boolean);
      await api.post("desk", `/api/desk/open-requests/${detail.item.id}/receipt`, {
        result: receiptResult,
        account_ids: ids,
        reason: receiptResult === "AGENT_REJECTED" ? receiptReason.trim() : null,
      });
      setMsg("回执已录入");
      await load();
      await loadDetail(detail.item.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "回执失败");
    } finally {
      setBusy(false);
    }
  }

  async function doCreate() {
    setBusy(true);
    setErr("");
    try {
      await api.post("desk", "/api/desk/open-requests", {
        client_id: Number(fClient),
        platform: fPlatform,
        subject: fSubject.trim() || null,
        apply_count: Number(fApplyCount),
        bm_id: fBmId.trim() || null,
        timezone: fTimezone.trim() || null,
        currency: fCurrency,
        daily_limit: fDailyLimit,
        remark: fRemark.trim() || null,
      });
      setCreateOpen(false);
      setFBmId("");
      setFRemark("");
      setMsg("开户申请已创建，等待审批");
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    const header = [
      "ID", "申请单号", "客户名称", "代理商", "开户媒体", "开户主体",
      "申请开户数", "完成开户数", "开户方式", "状态", "处理人", "创建时间",
    ];
    const lines = items.map((r) => [
      r.id, r.biz_no, r.client_name ?? "", r.channel ?? "", r.platform_label,
      r.subject ?? "", r.apply_count, r.completed_count, r.open_method_label,
      r.status_label, r.handler ?? "", r.created_at,
    ]);
    const csv = [header, ...lines]
      .map((cols) => cols.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
      .join("\r\n");
    const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `open-requests-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function setFilter<K extends keyof Filters>(key: K, value: Filters[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <>
      <div className="card">
        <div className="card-head" style={{ paddingBottom: 0, borderBottom: 0 }}>
          <div className="subtabs" style={{ marginBottom: 0, flex: 1 }}>
            <button className={tab === "all" ? "on" : ""} onClick={() => setTab("all")}>
              全部
            </button>
            <button className={tab === "pending" ? "on" : ""} onClick={() => setTab("pending")}>
              待处理{pendingTotal > 0 ? `（${pendingTotal}）` : ""}
            </button>
          </div>
          <div className="toolbar">
            {canSubmit && (
              <button className="btn sm" onClick={() => setCreateOpen(true)} disabled={busy}>
                代客户提交
              </button>
            )}
            <button className="btn sm" onClick={exportCsv} disabled={!items.length}>
              导出
            </button>
          </div>
        </div>

        <div className="card-body" style={{ paddingBottom: 10 }}>
          <div className="filterbar">
            <div className="filter-field span2">
              <label>创建时间</label>
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <input
                  type="date"
                  value={draft.dateFrom}
                  onChange={(e) => setFilter("dateFrom", e.target.value)}
                />
                <span className="faint">至</span>
                <input
                  type="date"
                  value={draft.dateTo}
                  onChange={(e) => setFilter("dateTo", e.target.value)}
                />
              </div>
            </div>
            <div className="filter-field">
              <label>代理商</label>
              <select value={draft.channel} onChange={(e) => setFilter("channel", e.target.value)}>
                <option value="">请选择代理商</option>
                {channelOptions.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            <div className="filter-field">
              <label>开户媒体</label>
              <select value={draft.platform} onChange={(e) => setFilter("platform", e.target.value)}>
                <option value="">全部媒体</option>
                {platformOptions.map(([code, label]) => (
                  <option key={code} value={code}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="filter-field">
              <label>申请单号</label>
              <input
                value={draft.bizNo}
                onChange={(e) => setFilter("bizNo", e.target.value)}
                placeholder="申请单号，请输入"
              />
            </div>
            <div className="filter-field">
              <label>客户名称 / 编号</label>
              <input
                value={draft.clientQ}
                onChange={(e) => setFilter("clientQ", e.target.value)}
                placeholder="客户名称/编号，请输入"
              />
            </div>
            <div className="filter-field">
              <label>开户主体</label>
              <input
                value={draft.subject}
                onChange={(e) => setFilter("subject", e.target.value)}
                placeholder="开户主体，请输入"
              />
            </div>
            <div className="filter-field">
              <label>处理人</label>
              <input
                value={draft.handler}
                onChange={(e) => setFilter("handler", e.target.value)}
                placeholder="处理人，请输入"
              />
            </div>
            <div className="filter-field">
              <label>账户 ID</label>
              <input
                value={draft.accountId}
                onChange={(e) => setFilter("accountId", e.target.value)}
                placeholder="账户 ID，请输入"
              />
            </div>
            <div className="filter-actions">
              <button
                className="btn primary"
                onClick={() => setApplied({ ...draft })}
                disabled={loading}
              >
                查询
              </button>
              <button
                className="btn"
                onClick={() => {
                  setDraft(EMPTY_FILTERS);
                  setApplied(EMPTY_FILTERS);
                }}
              >
                重置
              </button>
            </div>
          </div>
        </div>

        {msg && (
          <div className="card-body" style={{ paddingBottom: 0, paddingTop: 0 }}>
            <div className="warnbox">{msg}</div>
          </div>
        )}
        {err && (
          <div className="card-body" style={{ paddingBottom: 0, paddingTop: 0 }}>
            <div className="err">{err}</div>
          </div>
        )}

        <div style={{ overflowX: "auto" }}>
          <table className="open-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>申请单号</th>
                <th>客户名称</th>
                <th>代理商</th>
                <th>开户媒体</th>
                <th>开户主体</th>
                <th className="num">申请开户数</th>
                <th className="num">完成开户数</th>
                <th>开户方式</th>
                <th>状态</th>
                <th>处理人</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id}>
                  <td className="mono faint">{r.id}</td>
                  <td>
                    <div className="mono">{r.biz_no}</div>
                    <div className="faint" style={{ fontSize: 11 }}>
                      {r.source_label}
                      {r.applicant ? ` · ${r.applicant}` : ""}
                    </div>
                  </td>
                  <td className="wrap">
                    <div>{r.client_name || "—"}</div>
                    {r.client_code && (
                      <div className="faint mono" style={{ fontSize: 11 }}>
                        {r.client_code}
                      </div>
                    )}
                  </td>
                  <td className="muted">{r.channel || "—"}</td>
                  <td>{r.platform_label}</td>
                  <td className="wrap">{r.subject || "—"}</td>
                  <td className="num mono">{r.apply_count}</td>
                  <td className="num mono">{r.completed_count}</td>
                  <td className="muted">{r.open_method_label}</td>
                  <td>
                    <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                    {r.reject_reason && (
                      <div className="faint" style={{ fontSize: 11, maxWidth: 220 }}>
                        {r.reject_reason}
                      </div>
                    )}
                  </td>
                  <td className="muted">
                    {r.handler || "—"}
                    {r.processed_at && (
                      <div className="faint" style={{ fontSize: 11 }}>
                        {fmtTime(r.processed_at)}
                      </div>
                    )}
                  </td>
                  <td className="mono faint">{fmtTime(r.created_at)}</td>
                  <td>
                    <button className="btn sm ghost" onClick={() => openDetail(r.id)}>
                      查看
                    </button>
                    {canApprove && r.status === "PENDING_APPROVAL" && (
                      <button className="btn sm primary" onClick={() => openDetail(r.id)}>
                        审批
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {items.length === 0 && !loading && (
                <tr>
                  <td colSpan={13}>
                    <div className="empty">暂无数据</div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
          共 {total} 条 · 待处理 {pendingTotal} 条。审批通过后可直接自动开户；未通过审批的单据服务端会拦截。
          Mock 回执仅用于联调，不产生真实媒体账户。
        </div>
      </div>

      {createOpen && (
        <div className="modal-mask" onClick={() => setCreateOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>代客户提交开户申请</h2>
              <button className="btn ghost sm" onClick={() => setCreateOpen(false)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">创建后进入「待审批」，由管理端审批通过后开户。</div>
              <div className="form-grid">
                <div className="full">
                  <label>客户</label>
                  <select value={fClient} onChange={(e) => setFClient(e.target.value)}>
                    <option value="">请选择客户</option>
                    {clients.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.company_name}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>开户媒体</label>
                  <select value={fPlatform} onChange={(e) => setFPlatform(e.target.value)}>
                    {platformOptions.map(([code, label]) => (
                      <option key={code} value={code}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>开户主体</label>
                  <input
                    value={fSubject}
                    onChange={(e) => setFSubject(e.target.value)}
                    placeholder="默认使用客户公司名"
                  />
                </div>
                <div>
                  <label>申请开户数</label>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    value={fApplyCount}
                    onChange={(e) => setFApplyCount(e.target.value)}
                  />
                </div>
                <div>
                  <label>BM ID</label>
                  <input value={fBmId} onChange={(e) => setFBmId(e.target.value)} placeholder="BM-123" />
                </div>
                <div>
                  <label>时区</label>
                  <input value={fTimezone} onChange={(e) => setFTimezone(e.target.value)} />
                </div>
                <div>
                  <label>币种</label>
                  <select value={fCurrency} onChange={(e) => setFCurrency(e.target.value)}>
                    {["USD", "HKD"].map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>单账户日限额</label>
                  <input
                    value={fDailyLimit}
                    onChange={(e) => setFDailyLimit(e.target.value)}
                    inputMode="decimal"
                  />
                </div>
                <div className="full">
                  <label>备注</label>
                  <input value={fRemark} onChange={(e) => setFRemark(e.target.value)} />
                </div>
              </div>
              {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setCreateOpen(false)} disabled={busy}>
                取消
              </button>
              <button className="btn primary" onClick={doCreate} disabled={busy || !fClient}>
                创建
              </button>
            </div>
          </div>
        </div>
      )}

      {detailId !== null && (
        <div className="drawer-mask" onClick={closeDetail}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>开户申请详情</h2>
                <p className="mono">{detail?.item.biz_no || `#${detailId}`}</p>
              </div>
              <button className="btn ghost sm" onClick={closeDetail}>
                关闭
              </button>
            </div>
            <div className="drawer-body">
              {detailLoading && <div className="empty">加载中…</div>}
              {detailErr && <div className="err">{detailErr}</div>}
              {detail && (
                <>
                  {msg && <div className="warnbox">{msg}</div>}
                  {err && <div className="err">{err}</div>}

                  <div className="kv" style={{ marginBottom: 14 }}>
                    <span className="k">申请单号</span>
                    <span className="v mono">{detail.item.biz_no}</span>
                    <span className="k">来源</span>
                    <span className="v">
                      {detail.item.source_label}
                      {detail.item.applicant ? ` · ${detail.item.applicant}` : ""}
                    </span>
                    <span className="k">客户名称</span>
                    <span className="v">
                      {detail.item.client_name || "—"}
                      {detail.item.client_code ? `（${detail.item.client_code}）` : ""}
                    </span>
                    <span className="k">代理商</span>
                    <span className="v">{detail.item.channel || "—"}</span>
                    <span className="k">开户媒体</span>
                    <span className="v">{detail.item.platform_label}</span>
                    <span className="k">开户主体</span>
                    <span className="v">{detail.item.subject || "—"}</span>
                    <span className="k">申请 / 完成</span>
                    <span className="v mono">
                      {detail.item.apply_count} / {detail.item.completed_count}
                    </span>
                    <span className="k">开户方式</span>
                    <span className="v">{detail.item.open_method_label}</span>
                    <span className="k">状态</span>
                    <span className="v">
                      <span className={`badge ${badgeClass(detail.item.status)}`}>
                        {detail.item.status_label}
                      </span>
                    </span>
                    <span className="k">BM / 时区</span>
                    <span className="v">
                      {detail.item.bm_id || "—"} · {detail.item.timezone || "—"}
                    </span>
                    <span className="k">币种 / 日限额</span>
                    <span className="v mono">
                      {detail.item.currency} · {money(detail.item.daily_limit)}
                    </span>
                    <span className="k">处理人</span>
                    <span className="v">{detail.item.handler || "—"}</span>
                    <span className="k">创建时间</span>
                    <span className="v mono">{fmtTime(detail.item.created_at)}</span>
                    <span className="k">处理时间</span>
                    <span className="v mono">{fmtTime(detail.item.processed_at)}</span>
                  </div>

                  {detail.item.remark && (
                    <div className="warnbox">客户备注：{detail.item.remark}</div>
                  )}
                  {detail.item.reject_reason && (
                    <div className="err">驳回原因：{detail.item.reject_reason}</div>
                  )}

                  {(detail.item.license_url ||
                    detail.item.credit_code ||
                    detail.item.target_country ||
                    detail.item.industry ||
                    detail.item.bc_ids.length > 0 ||
                    detail.item.product_link) && (
                    <div className="qual-block" style={{ marginBottom: 14 }}>
                      <div className="qual-head">
                        申请材料
                        <span className="faint" style={{ fontWeight: 400, fontSize: 11 }}>
                          客户在门户提交
                        </span>
                      </div>
                      <div className="kv" style={{ marginBottom: 10 }}>
                        <span className="k">开户主体</span>
                        <span className="v">{detail.item.subject || "—"}</span>
                        <span className="k">信用代码</span>
                        <span className="v mono">{detail.item.credit_code || "—"}</span>
                        <span className="k">注册地</span>
                        <span className="v">
                          {optLabel(placeOptions, detail.item.registered_place)}
                        </span>
                        <span className="k">投放国家</span>
                        <span className="v">
                          {optLabel(countryOptions, detail.item.target_country)}
                        </span>
                        <span className="k">账户行业</span>
                        <span className="v">{optLabel(industryOptions, detail.item.industry)}</span>
                        <span className="k">绑定 BCID</span>
                        <span className="v mono">
                          {detail.item.bc_ids.length ? detail.item.bc_ids.join("、") : "—"}
                        </span>
                        <span className="k">产品链接</span>
                        <span className="v">
                          {detail.item.product_link ? (
                            <a
                              href={detail.item.product_link}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {detail.item.product_link}
                            </a>
                          ) : (
                            "—"
                          )}
                        </span>
                      </div>
                      <div className="qual-files">
                        <div className="qual-file">
                          <div className="lbl">申请用营业执照</div>
                          {detail.item.license_url ? (
                            <AuthedThumb
                              kind="desk"
                              path={detail.item.license_url}
                              alt="申请用营业执照"
                              className="qual-thumb"
                              onClick={(blob) => window.open(blob, "_blank", "noopener")}
                            />
                          ) : (
                            <div className="qual-none">未提供</div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {detail.client && (
                    <div className="qual-block" style={{ marginBottom: 14 }}>
                      <div className="qual-head">
                        客户资质
                        <span className={`badge ${badgeClass(detail.client.status)}`}>
                          {detail.client.status_label}
                        </span>
                      </div>
                      <div className="kv" style={{ marginBottom: 10 }}>
                        <span className="k">公司名称</span>
                        <span className="v">{detail.client.company_name}</span>
                        <span className="k">信用代码</span>
                        <span className="v mono">{detail.client.credit_code || "—"}</span>
                        <span className="k">行业</span>
                        <span className="v">{detail.client.industry || "—"}</span>
                        <span className="k">注册地</span>
                        <span className="v">{detail.client.registered_place || "—"}</span>
                      </div>
                      <div className="qual-files">
                        {([
                          ["营业执照", detail.client.license_url],
                          ["身份证", detail.client.idcard_url],
                          ["其他资质", detail.client.qualification_url],
                        ] as [string, string | null][]).map(([label, url]) => (
                          <div className="qual-file" key={label}>
                            <div className="lbl">{label}</div>
                            {url ? (
                              <AuthedThumb
                                kind="desk"
                                path={url}
                                alt={label}
                                className="qual-thumb"
                                onClick={(blob) => window.open(blob, "_blank", "noopener")}
                              />
                            ) : (
                              <div className="qual-none">未提供</div>
                            )}
                          </div>
                        ))}
                      </div>
                      {detail.existing_accounts.length > 0 && (
                        <div className="warnbox" style={{ marginTop: 10 }}>
                          该客户已有 {detail.existing_accounts.length} 个广告账户：
                          {detail.existing_accounts
                            .map((a) => `${a.account_id}（${a.platform}）`)
                            .join("、")}
                        </div>
                      )}
                    </div>
                  )}

                  {detail.item.account_ids.length > 0 && (
                    <div className="kv" style={{ marginBottom: 14 }}>
                      <span className="k">已开账户</span>
                      <span className="v mono">{detail.item.account_ids.join("、")}</span>
                    </div>
                  )}

                  {detail.item.status === "PENDING_APPROVAL" && canApprove && (
                    <>
                      <div className="warnbox">
                        五项核对清单须全部勾选。通过并自动开户会立即生成广告账户（Mock RPA，
                        账户 ID 形如 AUTO-单号-1）；也可只审批，后续再提交一代。
                      </div>
                      <div className="check-list">
                        {CHECK_ITEMS.map(([key, label]) => (
                          <label key={key}>
                            <input
                              type="checkbox"
                              checked={checks[key]}
                              onChange={(e) =>
                                setChecks((prev) => ({ ...prev, [key]: e.target.checked }))
                              }
                            />
                            <span>{label}</span>
                          </label>
                        ))}
                      </div>
                      <div className="field" style={{ marginTop: 12 }}>
                        <label>审批备注（可选）</label>
                        <input
                          value={approveRemark}
                          onChange={(e) => setApproveRemark(e.target.value)}
                          placeholder="核对通过"
                        />
                      </div>
                      <div className="field">
                        <label>账户 ID（可选，每行一个；留空则自动开户）</label>
                        <textarea
                          rows={2}
                          value={manualIds}
                          onChange={(e) => setManualIds(e.target.value)}
                          placeholder={"留空由系统生成\n或填写与申请数一致的媒体账户 ID"}
                        />
                      </div>
                      {rejecting && (
                        <div className="field">
                          <label>驳回原因（必填）</label>
                          <textarea
                            rows={2}
                            value={rejectReason}
                            onChange={(e) => setRejectReason(e.target.value)}
                            placeholder="开户主体与营业执照不一致"
                          />
                        </div>
                      )}
                    </>
                  )}

                  {detail.item.status === "PENDING_SUBMIT" && canSubmit && (
                    <div className="warnbox">
                      已审批通过，尚未提交一代。提交后会进入「一代审核中」。
                    </div>
                  )}

                  {detail.item.status === "AGENT_REVIEWING" && (canApprove || canSubmit) && (
                    <>
                      <div className="warnbox">
                        一代审核中。可录入 Mock 回执联调；客户申请留空账户 ID 时由系统自动补齐。
                      </div>
                      <div className="field">
                        <label>回执结果</label>
                        <select
                          value={receiptResult}
                          onChange={(e) =>
                            setReceiptResult(e.target.value as "OPENED" | "AGENT_REJECTED")
                          }
                        >
                          <option value="OPENED">已开户（OPENED）</option>
                          <option value="AGENT_REJECTED">一代拒绝（AGENT_REJECTED）</option>
                        </select>
                      </div>
                      {receiptResult === "OPENED" ? (
                        <div className="field">
                          <label>账户 ID（每行一个，留空自动生成）</label>
                          <textarea
                            rows={2}
                            value={receiptIds}
                            onChange={(e) => setReceiptIds(e.target.value)}
                          />
                        </div>
                      ) : (
                        <div className="field">
                          <label>拒绝原因（必填）</label>
                          <textarea
                            rows={2}
                            value={receiptReason}
                            onChange={(e) => setReceiptReason(e.target.value)}
                          />
                        </div>
                      )}
                    </>
                  )}

                  {detail.item.agent_receipt && (
                    <div className="field" style={{ marginTop: 12 }}>
                      <label>回执原文</label>
                      <pre className="ta" style={{ minHeight: 90 }}>
                        {detail.item.agent_receipt}
                      </pre>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="drawer-foot">
              {detail?.item.status === "PENDING_APPROVAL" && canApprove && !rejecting && (
                <>
                  <button className="btn danger" onClick={() => setRejecting(true)} disabled={busy}>
                    驳回
                  </button>
                  <button
                    className="btn"
                    onClick={() => doApprove(false)}
                    disabled={busy || !allChecked}
                    title={allChecked ? "" : "五项核对清单须全部勾选"}
                  >
                    仅审批通过
                  </button>
                  <button
                    className="btn primary"
                    onClick={() => doApprove(true)}
                    disabled={busy || !allChecked}
                    title={allChecked ? "" : "五项核对清单须全部勾选"}
                  >
                    通过并开户
                  </button>
                </>
              )}
              {detail?.item.status === "PENDING_APPROVAL" && canApprove && rejecting && (
                <>
                  <button className="btn" onClick={() => setRejecting(false)} disabled={busy}>
                    取消
                  </button>
                  <button
                    className="btn danger"
                    onClick={doReject}
                    disabled={busy || !rejectReason.trim()}
                  >
                    确认驳回
                  </button>
                </>
              )}
              {detail?.item.status === "PENDING_SUBMIT" && canSubmit && (
                <button className="btn primary" onClick={() => doSubmit(detail.item)} disabled={busy}>
                  提交到一代
                </button>
              )}
              {detail?.item.status === "AGENT_REVIEWING" && (canApprove || canSubmit) && (
                <button
                  className="btn primary"
                  onClick={doReceipt}
                  disabled={
                    busy ||
                    (receiptResult === "AGENT_REJECTED" && !receiptReason.trim())
                  }
                >
                  录入 Mock 回执
                </button>
              )}
              <button className="btn" onClick={closeDetail}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
