import { useCallback, useEffect, useRef, useState } from "react";
import { api, qs, upload, type Session } from "../api";
import { AuthedThumb } from "../components/AuthedFile";
import { badgeClass, fmtTime } from "../utils";
import PortalProfile from "./PortalProfile";

interface Option {
  code: string;
  label: string;
}

interface OpenItem {
  id: number;
  biz_no: string;
  platform: string;
  platform_label: string;
  subject: string;
  apply_count: number;
  completed_count: number;
  open_method: string;
  open_method_label: string;
  status: string;
  status_label: string;
  handler: string | null;
  reject_reason: string | null;
  remark: string | null;
  license_url: string | null;
  credit_code: string | null;
  registered_place: string | null;
  industry: string | null;
  target_country: string | null;
  bc_ids: string[];
  product_link: string | null;
  feedback: string | null;
  account_ids: string[];
  /** 客户侧列表目前不下发申请人，导出时回落到当前登录人 */
  applicant?: string | null;
  created_at: string;
  processed_at: string | null;
}

interface LicenseFile {
  url: string;
  original_name: string;
  created_at: string;
}

interface ListResp {
  total: number;
  items: OpenItem[];
  summary: Record<string, number>;
  company_name: string;
  default_subject: string;
  settle_currency: string;
  can_apply: boolean;
  apply_blocked_reason: string | null;
  licenses: LicenseFile[];
  bc_options: string[];
  default_bc_ids: string[];
  options: {
    platform: Option[];
    country: Option[];
    industry: Option[];
    registered_place: Option[];
    status: Option[];
  };
}

interface Filters {
  dateFrom: string;
  dateTo: string;
  subject: string;
  bizNo: string;
  applicant: string;
  platform: string;
  status: string;
}

const EMPTY_FILTERS: Filters = {
  dateFrom: "",
  dateTo: "",
  subject: "",
  bizNo: "",
  applicant: "",
  platform: "",
  status: "",
};

interface ApplyForm {
  platform: string;
  count: string;
  subject: string;
  creditCode: string;
  regPlace: string;
  country: string;
  industry: string;
  productLink: string;
  bcids: string[];
}

/** /parse-license 的返回：OCR 认出来的字段 + 可展开的识别原文 */
interface LicenseHint {
  parsed: boolean;
  source: string;
  doc_type?: string;
  doc_type_label?: string;
  name?: string;
  name_en?: string;
  credit_code?: string;
  registered_place?: string;
  address?: string;
  legal_person?: string;
  business_nature?: string;
  valid_from?: string;
  valid_to?: string;
  confidence?: number;
  warnings?: string[];
  note?: string;
  ocr_note?: string;
  elapsed_ms?: number;
  lines?: string[];
}

// 当前只做 TikTok：开户媒体固定 TikTok，时区随之固定。
const PLATFORM_TIMEZONE: Record<string, string> = {
  TIKTOK: "Asia/Shanghai",
};

/** 开户单自己的四步，与入驻三级审核分开显示，避免两张单混在一起看 */
const REQ_STEPS = ["提交申请", "管理端审批", "开户处理", "已开户"];

/**
 * 按开户单状态推步骤：done=已走完的步数，now=进行中，bad=卡住的那一步。
 * 后端只给状态不给逐节点时间，这里不伪造时间点。
 */
function stepMarks(status: string): { done: number; now: number; bad: number } {
  switch (status) {
    case "PENDING_APPROVAL":
      return { done: 1, now: 1, bad: -1 };
    case "REJECTED":
      return { done: 1, now: -1, bad: 1 };
    case "PENDING_SUBMIT":
    case "AGENT_REVIEWING":
      return { done: 2, now: 2, bad: -1 };
    case "AGENT_REJECTED":
      return { done: 2, now: -1, bad: 2 };
    case "OPENED":
      return { done: 4, now: -1, bad: -1 };
    default:
      return { done: 1, now: -1, bad: -1 };
  }
}

const EMPTY_FORM: ApplyForm = {
  platform: "TIKTOK",
  count: "1",
  subject: "",
  creditCode: "",
  regPlace: "",
  country: "US",
  industry: "",
  productLink: "",
  bcids: [],
};

export default function PortalOpen({ session }: { session: Session }) {
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [data, setData] = useState<ListResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [detail, setDetail] = useState<OpenItem | null>(null);
  /** 入驻进度按「开户申请记录」逐条查看，不再给一个整页入口 */
  const [progressOf, setProgressOf] = useState<OpenItem | null>(null);
  const [form, setForm] = useState<ApplyForm>(EMPTY_FORM);
  const [licenseUrl, setLicenseUrl] = useState("");
  const [licenseName, setLicenseName] = useState("");
  const [licenseHint, setLicenseHint] = useState("");
  const [uploading, setUploading] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [bcidInput, setBcidInput] = useState("");

  const fileRef = useRef<HTMLInputElement | null>(null);
  const seqRef = useRef(0);
  /** 哪些字段是 OCR 自动填的：换一张证照时允许覆盖，用户手改过就不再动 */
  const autoFill = useRef({ subject: false, creditCode: false, regPlace: false });

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "portal",
        "/api/portal/open-requests" +
          qs({
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            subject: applied.subject || undefined,
            biz_no: applied.bizNo || undefined,
            applicant: applied.applicant || undefined,
            platform: applied.platform || undefined,
            status: applied.status || undefined,
            limit: 200,
          })
      );
      if (stale()) return;
      setData(d);
    } catch (e) {
      if (!stale()) setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [applied]);

  useEffect(() => {
    load();
  }, [load]);

  // 入驻进度抽屉：Esc 关闭，锁背景滚动
  useEffect(() => {
    if (!progressOf) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setProgressOf(null);
    };
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [progressOf]);

  function setFilter<K extends keyof Filters>(key: K, value: Filters[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function openCreate() {
    // 企业信息一律不预填：名称、注册地都由客户按营业执照自己填/选，
    // 避免带着上一家或系统默认的主体直接提交。
    setForm({
      ...EMPTY_FORM,
      bcids: [...(data?.default_bc_ids ?? [])],
    });
    setLicenseUrl("");
    setLicenseName("");
    setLicenseHint("");
    setBcidInput("");
    setErr("");
    setMsg("");
    // 三个字段打开时都是空值，OCR 认出来的结果可以直接填进去
    autoFill.current = { subject: false, creditCode: false, regPlace: false };
    setCreateOpen(true);
  }

  function applyHint(h: LicenseHint) {
    // 只在「字段为空」或「上一次也是 OCR 自动填的」时覆盖，避免冲掉用户手改的内容
    setForm((prev) => {
      const takeName = !!h.name && (!prev.subject.trim() || autoFill.current.subject);
      const takeCode =
        !!h.credit_code && (!prev.creditCode.trim() || autoFill.current.creditCode);
      // 注册地下拉默认空着，只有用户没选过才允许被识别结果覆盖
      const takePlace =
        !!h.registered_place && (autoFill.current.regPlace || !prev.regPlace);
      return {
        ...prev,
        subject: takeName ? h.name! : prev.subject,
        creditCode: takeCode ? h.credit_code! : prev.creditCode,
        regPlace: takePlace ? h.registered_place! : prev.regPlace,
      };
    });
    autoFill.current = {
      subject: !!h.name,
      creditCode: !!h.credit_code,
      regPlace: !!h.registered_place,
    };
    setLicenseHint(h.note || (h.parsed ? "已带出可识别信息，请核对" : "请手动补充企业信息"));
  }

  async function parseLicense(url: string) {
    if (!url) return;
    setParsing(true);
    try {
      const h = await api.post<any>("portal", "/api/portal/open-requests/parse-license", {
        file_key: url,
      });
      applyHint(h);
    } catch (e: any) {
      setLicenseHint(e?.message || "未能自动解析，请手动填写");
    } finally {
      setParsing(false);
    }
  }

  async function pickHistory(url: string) {
    setLicenseUrl(url);
    const hit = data?.licenses.find((x) => x.url === url);
    setLicenseName(hit?.original_name || "");
    setLicenseHint("");
    if (url) await parseLicense(url);
  }

  async function handleLicenseFile(file: File) {
    const allowed = ["image/jpeg", "image/png", "image/webp"];
    if (!allowed.includes(file.type)) {
      setErr("营业执照仅支持 jpg / png 格式的彩色扫描件");
      return;
    }
    setUploading(true);
    setErr("");
    setMsg("");
    setLicenseHint("");
    try {
      const r = await upload("portal", file, "LICENSE");
      setLicenseUrl(r.url);
      setLicenseName(r.original_name);
      setData((prev) =>
        prev
          ? {
              ...prev,
              licenses: [
                { url: r.url, original_name: r.original_name, created_at: "" },
                ...prev.licenses.filter((x) => x.url !== r.url),
              ],
            }
          : prev
      );
      await parseLicense(r.url);
    } catch (e: any) {
      setErr(e?.message || "营业执照上传失败");
    } finally {
      setUploading(false);
    }
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleLicenseFile(file);
  }

  function addBcid(raw: string) {
    const value = raw.trim();
    if (!value) return;
    setForm((prev) =>
      prev.bcids.includes(value) || prev.bcids.length >= 10
        ? prev
        : { ...prev, bcids: [...prev.bcids, value] }
    );
    setBcidInput("");
  }

  function removeBcid(value: string) {
    setForm((prev) => ({ ...prev, bcids: prev.bcids.filter((x) => x !== value) }));
  }

  async function submitApply(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setMsg("");
    if (!licenseUrl) return setErr("请上传或选择营业执照/BR");
    if (!form.subject.trim()) return setErr("请填写企业名称");
    if (!form.creditCode.trim()) return setErr("请填写信用代码/登记证号");
    if (!form.country) return setErr("请选择投放国家");
    if (!form.industry) return setErr("请选择账户行业");
    if (!form.productLink.trim()) return setErr("请填写产品链接");
    if (!/^https?:\/\//i.test(form.productLink.trim())) {
      return setErr("产品链接必须以 http:// 或 https:// 开头");
    }
    setBusy(true);
    try {
      const r = await api.post<{ biz_no: string; message?: string }>(
        "portal",
        "/api/portal/open-requests",
        {
          platform: form.platform,
          subject: form.subject.trim(),
          apply_count: Number(form.count) || 1,
          bm_id: form.bcids[0] || null,
          timezone: PLATFORM_TIMEZONE[form.platform] || "UTC",
          currency: data?.settle_currency || "USD",
          daily_limit: "0",
          license_url: licenseUrl,
          credit_code: form.creditCode.trim(),
          registered_place: form.regPlace || null,
          industry: form.industry,
          target_country: form.country,
          bc_ids: form.bcids,
          product_link: form.productLink.trim(),
        }
      );
      setCreateOpen(false);
      setMsg(`开户申请已提交（${r.biz_no}），等待管理端审批`);
      await load();
    } catch (e: any) {
      setErr(e?.message || "提交失败");
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    const header = [
      "申请单编号", "开户主体", "开户媒体", "申请开户数", "申请状态",
      "驳回原因", "反馈信息", "申请人", "申请时间",
    ];
    const rows = (data?.items ?? []).map((r) => [
      r.biz_no,
      r.subject,
      r.platform_label,
      r.apply_count,
      r.status_label,
      r.reject_reason ?? "",
      r.feedback ?? "",
      r.applicant ?? "",
      fmtTime(r.created_at),
    ]);
    const csv = [header, ...rows]
      .map((cols) => cols.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
      .join("\r\n");
    const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `open-applications-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const options = data?.options;
  const blocked = data ? !data.can_apply : false;

  return (
    <>
      {data?.apply_blocked_reason && <div className="warnbox">{data.apply_blocked_reason}</div>}
      {msg && <div className="warnbox">{msg}</div>}
      {err && !createOpen && <div className="err">{err}</div>}

      {/* 入驻没通过时开户申请是提不了的；把入驻进度整块摊在本页，
          被驳回的客户才有「改资料重提」的入口，不能藏进开户单抽屉里。 */}
      {data && !data.can_apply && <PortalProfile session={session} onChanged={load} />}

      <div className="card">
        <div className="card-body" style={{ paddingBottom: 10 }}>
          <div className="filterbar portal-open-filter">
            <div className="filter-field">
              <label>开始日期</label>
              <input
                type="date"
                value={draft.dateFrom}
                onChange={(e) => setFilter("dateFrom", e.target.value)}
              />
            </div>
            <div className="filter-field">
              <label>结束日期</label>
              <input
                type="date"
                value={draft.dateTo}
                onChange={(e) => setFilter("dateTo", e.target.value)}
              />
            </div>
            <div className="filter-field">
              <label>开户主体</label>
              <input
                value={draft.subject}
                onChange={(e) => setFilter("subject", e.target.value)}
                placeholder="（关键字查询）"
              />
            </div>
            <div className="filter-field">
              <label>申请单编号</label>
              <input
                value={draft.bizNo}
                onChange={(e) => setFilter("bizNo", e.target.value)}
                placeholder="申请单编号"
              />
            </div>
            <div className="filter-field">
              <label>申请人</label>
              <input
                value={draft.applicant}
                onChange={(e) => setFilter("applicant", e.target.value)}
                placeholder="申请人"
              />
            </div>
            <div className="filter-field">
              <label>开户媒体</label>
              <select
                value={draft.platform}
                onChange={(e) => setFilter("platform", e.target.value)}
              >
                <option value="">全部媒体</option>
                {(options?.platform ?? []).map((p) => (
                  <option key={p.code} value={p.code}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="filter-field">
              <label>申请状态</label>
              <select
                value={draft.status}
                onChange={(e) => setFilter("status", e.target.value)}
              >
                <option value="">全部状态</option>
                {(options?.status ?? []).map((s) => (
                  <option key={s.code} value={s.code}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="filter-actions">
              <button
                className="btn"
                title="重置筛选"
                onClick={() => {
                  setDraft(EMPTY_FILTERS);
                  setApplied(EMPTY_FILTERS);
                }}
              >
                ↺
              </button>
              <button
                className="btn primary"
                onClick={() => setApplied({ ...draft })}
                disabled={loading}
              >
                查询
              </button>
              <button
                className="btn primary"
                onClick={openCreate}
                disabled={blocked}
                title={data?.apply_blocked_reason || ""}
              >
                + 开户申请
              </button>
              <button className="btn" onClick={exportCsv} disabled={!data?.items.length}>
                导出
              </button>
            </div>
          </div>
        </div>

        <div style={{ overflowX: "auto" }}>
          <table className="open-table portal-open-table">
            <thead>
              <tr>
                <th>申请单编号</th>
                <th>开户主体</th>
                <th>开户媒体</th>
                <th className="num">申请开户数</th>
                <th>申请状态</th>
                <th>驳回原因</th>
                <th>反馈信息</th>
                <th>申请人</th>
                <th>申请时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.biz_no}</td>
                  <td className="wrap">{r.subject || "—"}</td>
                  <td>{r.platform_label}</td>
                  <td className="num mono">{r.apply_count}</td>
                  <td>
                    <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                  </td>
                  <td className="wrap muted">{r.reject_reason || "—"}</td>
                  <td className="wrap muted">{r.feedback || "—"}</td>
                  <td className="muted">{r.applicant || session.display_name || "—"}</td>
                  <td className="mono faint">{fmtTime(r.created_at)}</td>
                  <td>
                    <div className="row-btns">
                      <button className="link-btn" onClick={() => setDetail(r)}>
                        查看申请单
                      </button>
                      <button className="link-btn" onClick={() => setProgressOf(r)}>
                        入驻进度
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {(data?.items ?? []).length === 0 && !loading && (
                <tr>
                  <td colSpan={10}>
                    <div className="empty">还没有开户申请，点右上角「+ 开户申请」开始</div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
          共 {data?.total ?? 0} 条。同一开户主体在同一媒体下有在办申请时，需等管理端处理完才能再次提交。
        </div>
      </div>

      {createOpen && options && (
        <div className="modal-mask" onClick={() => setCreateOpen(false)}>
          <form
            className="modal open-apply-modal"
            onClick={(e) => e.stopPropagation()}
            onSubmit={submitApply}
          >
            <div className="modal-head">
              <h2>开户申请</h2>
              <button type="button" className="btn ghost sm" onClick={() => setCreateOpen(false)}>
                ✕
              </button>
            </div>
            <div className="modal-body open-apply-body">
              <div className="open-apply-head">
                <div>企业信息</div>
                <div>账户信息</div>
              </div>
              <div className="open-apply-cols">
                <section className="apply-col">
                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>营业执照/BR
                    </label>
                    <select value={licenseUrl} onChange={(e) => pickHistory(e.target.value)}>
                      <option value="">选择历史已上传文件</option>
                      {(data?.licenses ?? []).map((l) => (
                        <option key={l.url} value={l.url}>
                          {l.original_name}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="license-block">
                    <div
                      className={`license-drop ${dragging ? "drag" : ""}`}
                      onClick={() => fileRef.current?.click()}
                      onDragOver={(e) => {
                        e.preventDefault();
                        setDragging(true);
                      }}
                      onDragLeave={() => setDragging(false)}
                      onDrop={onDrop}
                    >
                      <input
                        ref={fileRef}
                        type="file"
                        accept=".jpg,.jpeg,.png"
                        style={{ display: "none" }}
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          e.target.value = "";
                          if (f) handleLicenseFile(f);
                        }}
                      />
                      {licenseUrl ? (
                        <div className="license-preview" onClick={(e) => e.stopPropagation()}>
                          <AuthedThumb
                            kind="portal"
                            path={licenseUrl}
                            alt="营业执照"
                            className="license-thumb"
                            onClick={(blob) => window.open(blob, "_blank", "noopener")}
                          />
                          <div className="license-file-name" title={licenseName}>
                            {licenseName || "已选择营业执照"}
                          </div>
                          <button
                            type="button"
                            className="btn sm ghost"
                            onClick={() => {
                              setLicenseUrl("");
                              setLicenseName("");
                              setLicenseHint("");
                            }}
                          >
                            重新选择
                          </button>
                        </div>
                      ) : (
                        <>
                          <div className="license-icon">☁</div>
                          <div className="license-main">
                            {uploading ? "上传中…" : "点击或拖拽上传"}
                          </div>
                          <div className="license-hint">
                            允许上传 jpg/png 格式的彩色扫描文件
                          </div>
                        </>
                      )}
                    </div>

                    <div className="license-fields">
                      <div className="apply-row">
                        <label>注册地</label>
                        <select
                          value={form.regPlace}
                          onChange={(e) => {
                            autoFill.current.regPlace = false;
                            setForm({ ...form, regPlace: e.target.value });
                          }}
                        >
                          <option value="">请选择注册地</option>
                          {options.registered_place.map((p) => (
                            <option key={p.code} value={p.code}>
                              {p.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="apply-row">
                        <label>
                          <span className="req">*</span>信用代码/登记证号
                        </label>
                        <input
                          value={form.creditCode}
                          onChange={(e) => {
                            autoFill.current.creditCode = false;
                            setForm({ ...form, creditCode: e.target.value });
                          }}
                          placeholder="上传文件后自动带出，也可手动填写"
                        />
                      </div>
                      <div className="apply-row">
                        <label>
                          <span className="req">*</span>名称
                        </label>
                        <input
                          value={form.subject}
                          onChange={(e) => {
                            autoFill.current.subject = false;
                            setForm({ ...form, subject: e.target.value });
                          }}
                          placeholder="营业执照上的企业全称"
                        />
                      </div>
                      {(uploading || parsing) && (
                        <div className="field-hint">
                          {uploading ? "正在上传…" : "正在识别证照文字（首次约 10–20 秒）…"}
                        </div>
                      )}
                      {licenseHint && <div className="field-hint">{licenseHint}</div>}
                    </div>
                  </div>
                </section>

                <section className="apply-col">
                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>媒体
                    </label>
                    <div className="radio-row">
                      {options.platform.map((p) => (
                        <label key={p.code} className="radio-item">
                          <input
                            type="radio"
                            name="platform"
                            value={p.code}
                            checked={form.platform === p.code}
                            onChange={() => setForm({ ...form, platform: p.code })}
                          />
                          <span>{p.label}</span>
                        </label>
                      ))}
                    </div>
                  </div>

                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>申请开户数
                    </label>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={form.count}
                      onChange={(e) => setForm({ ...form, count: e.target.value })}
                    />
                  </div>

                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>投放国家
                    </label>
                    <select
                      value={form.country}
                      onChange={(e) => setForm({ ...form, country: e.target.value })}
                    >
                      <option value="">请选择投放国家</option>
                      {options.country.map((c) => (
                        <option key={c.code} value={c.code}>
                          {c.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>账户行业
                    </label>
                    <select
                      value={form.industry}
                      onChange={(e) => setForm({ ...form, industry: e.target.value })}
                    >
                      <option value="">请选择行业</option>
                      {options.industry.map((i) => (
                        <option key={i.code} value={i.code}>
                          {i.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="apply-row">
                    <label>绑定BCID</label>
                    <div>
                      <div className="bcid-box">
                        {form.bcids.map((b) => (
                          <span className="bcid-chip" key={b}>
                            {b}
                            <button type="button" onClick={() => removeBcid(b)}>
                              ✕
                            </button>
                          </span>
                        ))}
                        <input
                          value={bcidInput}
                          onChange={(e) => setBcidInput(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === ",") {
                              e.preventDefault();
                              addBcid(bcidInput);
                            }
                            if (e.key === "Backspace" && !bcidInput && form.bcids.length) {
                              removeBcid(form.bcids[form.bcids.length - 1]);
                            }
                          }}
                          placeholder="输入或选择 BCID（可多选）"
                        />
                      </div>
                      {(data?.bc_options?.length ?? 0) > 0 && (
                        <div className="bcid-suggest">
                          已有：
                          {data!.bc_options.map((b) => (
                            <button
                              type="button"
                              key={b}
                              className="bcid-suggest-item"
                              onClick={() => addBcid(b)}
                            >
                              {b}
                            </button>
                          ))}
                        </div>
                      )}
                      <div className="field-hint">
                        默认预选 BC 管理中设置的默认项，也可输入或选择其他 BCID
                      </div>
                    </div>
                  </div>

                  <div className="apply-row">
                    <label>
                      <span className="req">*</span>产品链接
                    </label>
                    <div>
                      <input
                        value={form.productLink}
                        onChange={(e) => setForm({ ...form, productLink: e.target.value })}
                        placeholder="请输入链接"
                      />
                      <div className="field-hint">tts 开户可使用此链接</div>
                    </div>
                  </div>
                </section>
              </div>

              {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
            </div>
            <div className="modal-foot">
              <button type="button" className="btn" onClick={() => setCreateOpen(false)} disabled={busy}>
                取消
              </button>
              <button className="btn primary" disabled={busy || uploading || parsing}>
                {busy ? "提交中…" : "提交"}
              </button>
            </div>
          </form>
        </div>
      )}

      {detail && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>开户申请详情</h2>
                <p className="mono">{detail.biz_no}</p>
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>
            <div className="drawer-body">
              <div className="kv" style={{ marginBottom: 14 }}>
                <span className="k">状态</span>
                <span className="v">
                  <span className={`badge ${badgeClass(detail.status)}`}>
                    {detail.status_label}
                  </span>
                </span>
                <span className="k">申请人</span>
                <span className="v">{detail.applicant || session.display_name || "—"}</span>
                <span className="k">申请时间</span>
                <span className="v mono">{fmtTime(detail.created_at)}</span>
                <span className="k">开户媒体</span>
                <span className="v">{detail.platform_label}</span>
                <span className="k">申请 / 完成</span>
                <span className="v mono">
                  {detail.apply_count} / {detail.completed_count}
                </span>
                <span className="k">开户方式</span>
                <span className="v">{detail.open_method_label}</span>
              </div>

              <div className="qual-block" style={{ marginBottom: 14 }}>
                <div className="qual-head">企业信息</div>
                <div className="kv" style={{ marginBottom: 10 }}>
                  <span className="k">名称</span>
                  <span className="v">{detail.subject || "—"}</span>
                  <span className="k">信用代码</span>
                  <span className="v mono">{detail.credit_code || "—"}</span>
                  <span className="k">注册地</span>
                  <span className="v">
                    {options?.registered_place.find((x) => x.code === detail.registered_place)
                      ?.label ||
                      detail.registered_place ||
                      "—"}
                  </span>
                </div>
                {detail.license_url ? (
                  <AuthedThumb
                    kind="portal"
                    path={detail.license_url}
                    alt="营业执照"
                    className="qual-thumb"
                    onClick={(blob) => window.open(blob, "_blank", "noopener")}
                  />
                ) : (
                  <div className="faint">未上传营业执照</div>
                )}
              </div>

              <div className="qual-block" style={{ marginBottom: 14 }}>
                <div className="qual-head">账户信息</div>
                <div className="kv">
                  <span className="k">投放国家</span>
                  <span className="v">
                    {options?.country.find((x) => x.code === detail.target_country)?.label ||
                      detail.target_country ||
                      "—"}
                  </span>
                  <span className="k">账户行业</span>
                  <span className="v">
                    {options?.industry.find((x) => x.code === detail.industry)?.label ||
                      detail.industry ||
                      "—"}
                  </span>
                  <span className="k">绑定 BCID</span>
                  <span className="v mono">{detail.bc_ids.length ? detail.bc_ids.join("、") : "—"}</span>
                  <span className="k">产品链接</span>
                  <span className="v">
                    {detail.product_link ? (
                      <a href={detail.product_link} target="_blank" rel="noreferrer">
                        {detail.product_link}
                      </a>
                    ) : (
                      "—"
                    )}
                  </span>
                  {detail.remark && (
                    <>
                      <span className="k">备注</span>
                      <span className="v">{detail.remark}</span>
                    </>
                  )}
                </div>
              </div>

              {detail.reject_reason && (
                <div className="err" style={{ marginBottom: 12 }}>
                  驳回原因：{detail.reject_reason}
                </div>
              )}
              {detail.feedback && <div className="warnbox">反馈信息：{detail.feedback}</div>}
              {detail.account_ids.length > 0 && (
                <div className="kv" style={{ marginTop: 12 }}>
                  <span className="k">已开账户</span>
                  <span className="v mono">{detail.account_ids.join("、")}</span>
                </div>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {progressOf && (
        <div className="drawer-mask" onClick={() => setProgressOf(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>入驻进度</h2>
                <p className="mono">
                  {progressOf.biz_no} · {progressOf.platform_label}
                </p>
              </div>
              <button className="btn ghost sm" onClick={() => setProgressOf(null)}>
                关闭
              </button>
            </div>
            <div className="drawer-body embed-cards">
              <div className="kv" style={{ marginBottom: 14 }}>
                <span className="k">申请状态</span>
                <span className="v">
                  <span className={`badge ${badgeClass(progressOf.status)}`}>
                    {progressOf.status_label}
                  </span>
                </span>
                <span className="k">开户主体</span>
                <span className="v">{progressOf.subject || "—"}</span>
                <span className="k">开户媒体</span>
                <span className="v">{progressOf.platform_label}</span>
                <span className="k">申请 / 完成</span>
                <span className="v mono">
                  {progressOf.apply_count} / {progressOf.completed_count}
                </span>
                <span className="k">开户方式</span>
                <span className="v">{progressOf.open_method_label}</span>
                <span className="k">申请时间</span>
                <span className="v mono">{fmtTime(progressOf.created_at)}</span>
                <span className="k">处理时间</span>
                <span className="v mono">
                  {progressOf.processed_at ? fmtTime(progressOf.processed_at) : "—"}
                </span>
              </div>

              <div className="steps">
                {(() => {
                  const marks = stepMarks(progressOf.status);
                  return REQ_STEPS.map((label, i) => (
                    <div
                      key={label}
                      className={[
                        "step",
                        i < marks.done ? "done" : "",
                        i === marks.now ? "now" : "",
                        i === marks.bad ? "bad" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      {label}
                    </div>
                  ));
                })()}
              </div>

              {progressOf.reject_reason && (
                <div className="warnbox">驳回原因：{progressOf.reject_reason}</div>
              )}
              {progressOf.feedback && (
                <div className="warnbox">反馈信息：{progressOf.feedback}</div>
              )}

              <div className="sub-title">企业入驻审核</div>
              <PortalProfile session={session} onChanged={load} />
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setProgressOf(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
