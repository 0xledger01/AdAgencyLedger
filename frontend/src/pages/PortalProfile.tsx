import { useCallback, useEffect, useRef, useState } from "react";
import { api, upload, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

/**
 * 门户「入驻进度」。
 * 断头修复（F-07 #1）：后端早就有 POST /portal/onboarding 与 /resubmit，
 * 前端一直没有页面。结果是**被驳回的客户只能看到驳回原因，没有任何重提入口**，
 * 必须打电话让销售改库，等于新客户无法自助跑完入驻。
 */

interface Progress {
  key: string;
  label: string;
  done: boolean;
}

interface Profile {
  biz_no: string | null;
  company_name: string;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  settle_currency: string;
  status: string;
  status_label: string;
  current_step: string | null;
  reject_reason: string | null;
  version: number;
  progress: Progress[];
  timeline: { step: string; action: string; operator: string; reason: string | null; at: string }[];
}

const STEP_LABEL: Record<string, string> = {
  REVIEW: "销售初审",
  RISK: "风控审核",
  LIMIT: "财务定额",
};

export default function PortalProfile({
  session,
  onChanged,
}: {
  session: Session;
  /** 重新提交成功后通知宿主（开户申请页要刷新"能否申请"的判断） */
  onChanged?: () => void;
}) {
  const [prof, setProf] = useState<Profile | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [ocrMsg, setOcrMsg] = useState("");
  /** 公司名还是系统带出的值（没被手改）时，识别结果可以替换 */
  const nameFromProfile = useRef(true);

  const load = useCallback(async () => {
    setErr("");
    try {
      const d = await api.get<Profile>("portal", "/api/portal/profile");
      setProf(d);
    } catch (e: any) {
      setErr(e?.message || "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** 只读态 → 编辑态：把当前档案值灌进表单 */
  function startEdit() {
    if (!prof) return;
    setForm({
      company_name: prof.company_name || "",
      contact_name: prof.contact_name || "",
      phone: prof.phone || "",
      email: prof.email || "",
      license_url: "",
      remark: "",
    });
    setOcrMsg("");
    nameFromProfile.current = true;
    setEditing(true);
    setMsg("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg("");
    setErr("");
    try {
      const payload: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(form)) {
        if (v && v.trim()) payload[k] = v.trim();
      }
      await api.post("portal", "/api/portal/onboarding/resubmit", payload);
      setEditing(false);
      setMsg("已重新提交，等待销售初审");
      await load();
      onChanged?.();
    } catch (e: any) {
      setErr(e?.message || "重新提交失败");
    } finally {
      setBusy(false);
    }
  }

  async function onPickLicense(file: File | null) {
    if (!file) return;
    setBusy(true);
    setErr("");
    setOcrMsg("");
    try {
      const r = await upload("portal", file, "LICENSE");
      setForm((f) => ({ ...f, license_url: r.url }));
      setMsg("营业执照已上传，正在识别证照文字…");
      try {
        const h = await api.post<any>("portal", "/api/portal/open-requests/parse-license", {
          file_key: r.url,
        });
        if (h?.name && nameFromProfile.current) {
          setForm((f) => ({ ...f, company_name: h.name }));
        }
        setOcrMsg(
          h?.parsed
            ? `${h.doc_type_label || "证照"}已识别，公司名称已带出，请核对后提交`
            : h?.note || "未识别到证照文字，请手动核对"
        );
      } catch {
        setOcrMsg("证照识别失败，请手动核对公司名称");
      }
      setMsg("营业执照已上传，提交后生效");
    } catch (e: any) {
      setErr(e?.message || "上传失败");
    } finally {
      setBusy(false);
    }
  }

  if (err && !prof) return <div className="err">{err}</div>;
  if (!prof) return <div className="card"><div className="empty">加载中…</div></div>;

  const rejected = prof.status === "REJECTED";
  const canEdit = rejected;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <h2>{prof.company_name}</h2>
          <span className={`badge ${badgeClass(prof.status)}`}>{prof.status_label}</span>
        </div>
        <div className="card-body">
          <div className="steps">
            {prof.progress.map((p) => (
              <div key={p.key} className={`step ${p.done ? "done" : ""}`}>
                {p.label}
              </div>
            ))}
          </div>

          {prof.reject_reason && <div className="warnbox">驳回原因：{prof.reject_reason}</div>}
          {msg && <div className="warnbox">{msg}</div>}
          {err && <div className="err">{err}</div>}

          <div className="kv">
            <span className="k">客户编号</span>
            <span className="v mono">{prof.biz_no}</span>
            <span className="k">联系人</span>
            <span className="v">{prof.contact_name || "—"}</span>
            <span className="k">电话</span>
            <span className="v">{prof.phone || "—"}</span>
            <span className="k">邮箱</span>
            <span className="v">{prof.email || "—"}</span>
            <span className="k">结算币种</span>
            <span className="v">{prof.settle_currency}</span>
            <span className="k">申请版本</span>
            <span className="v">v{prof.version}</span>
          </div>

          {canEdit && !editing && (
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn primary" onClick={startEdit}>
                修改资料并重新提交
              </button>
              <span className="faint">审核中不可修改；被驳回后可改了重提，版本号 +1</span>
            </div>
          )}
          {!canEdit && (
            <div className="faint" style={{ marginTop: 14 }}>
              {prof.status === "ACTIVE"
                ? "已开通，资料修改请联系客户经理"
                : "审核中，暂不可修改"}
            </div>
          )}
        </div>
      </div>

      {editing && (
        <div className="card">
          <div className="card-head">
            <h2>重新提交入驻资料</h2>
            <button className="btn ghost sm" onClick={() => setEditing(false)} disabled={busy}>
              取消
            </button>
          </div>
          <div className="card-body">
            <form onSubmit={submit} className="form-grid">
              {[
                ["company_name", "公司名称"],
                ["contact_name", "联系人"],
                ["phone", "电话"],
                ["email", "邮箱"],
              ].map(([k, label]) => (
                <div key={k}>
                  <label>{label}</label>
                  <input
                    value={form[k] ?? ""}
                    onChange={(e) => {
                      if (k === "company_name") nameFromProfile.current = false;
                      setForm((f) => ({ ...f, [k]: e.target.value }));
                    }}
                  />
                </div>
              ))}
              <div className="full">
                <label>营业执照（重新上传，可选）</label>
                <input
                  type="file"
                  accept=".jpg,.jpeg,.png,.webp,.pdf"
                  onChange={(e) => onPickLicense(e.target.files?.[0] ?? null)}
                  disabled={busy}
                />
                {form.license_url && <div className="faint mono">{form.license_url}</div>}
                {ocrMsg && <div className="field-hint">{ocrMsg}</div>}
              </div>
              <div className="full">
                <label>修改说明</label>
                <input
                  value={form.remark ?? ""}
                  onChange={(e) => setForm((f) => ({ ...f, remark: e.target.value }))}
                  placeholder="已补充对公账户信息"
                />
              </div>
              <div className="full row">
                <button className="btn primary" disabled={busy}>
                  提交重新审核
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h2>审核记录</h2>
        </div>
        <div className="card-body">
          <div className="timeline">
            {prof.timeline.map((t, i) => (
              <div key={i} className={`item ${t.action === "REJECT" ? "no" : "ok"}`}>
                <div className="t">
                  {STEP_LABEL[t.step] || t.step} ·{" "}
                  {t.action === "APPROVE" ? "通过" : t.action === "REJECT" ? "驳回" : t.action}
                </div>
                <div className="m">
                  {t.operator} · {fmtTime(t.at)}
                </div>
                {t.reason && <div className="m">说明：{t.reason}</div>}
              </div>
            ))}
            {prof.timeline.length === 0 && <div className="empty">还没有审核记录</div>}
          </div>
        </div>
      </div>
    </>
  );
}
