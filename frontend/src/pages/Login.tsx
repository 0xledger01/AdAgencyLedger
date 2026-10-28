import { useEffect, useState } from "react";
import { api, setSession, type Session } from "../api";

interface Props {
  kind: "desk" | "portal";
  onLogin: (s: Session) => void;
}

interface PlatformOption {
  code: string;
  label: string;
}

const EMPTY_APPLICATION = {
  company_name: "",
  contact_name: "",
  phone: "",
  wechat: "",
  email: "",
  platform: "TIKTOK",
  monthly_budget_expect: "",
  remark: "",
};

export default function Login({ kind, onLogin }: Props) {
  const isPortal = kind === "portal";
  // 只有本地开发才预填和展示测试账号：生产构建不能把 admin/admin123 摆在登录页上（F-19）
  const isDev = import.meta.env.DEV;
  const [username, setUsername] = useState(isDev ? (isPortal ? "client1" : "admin") : "");
  const [password, setPassword] = useState(isDev ? (isPortal ? "123456" : "admin123") : "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // 门户登录页默认展示开户申请；登录入口在右上角。
  // `?login=1` 让登录页可以刷新 / 被收藏，而不是刷新后又被弹回申请页。
  const [mode, setMode] = useState<"apply" | "login">(() =>
    isPortal && new URLSearchParams(window.location.search).get("login") ? "login" : isPortal ? "apply" : "login"
  );
  const [form, setForm] = useState(EMPTY_APPLICATION);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([{ code: "TIKTOK", label: "TikTok" }]);
  const [applyErr, setApplyErr] = useState("");
  const [applyBusy, setApplyBusy] = useState(false);
  const [applied, setApplied] = useState(false);
  const [applyMessage, setApplyMessage] = useState("");

  useEffect(() => {
    if (!isPortal) return;
    api
      .get<{ platform: PlatformOption[] }>("portal", "/api/public/client-applications/options")
      .then((d) => {
        if (d?.platform?.length) setPlatforms(d.platform);
      })
      .catch(() => {
        // 选项接口失败时保留 TikTok 兜底，不阻塞申请
      });
  }, [isPortal]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const path = isPortal ? "/api/auth/portal/login" : "/api/auth/login";
      const s = await api.post<Session>(kind, path, { username, password });
      setSession(kind, s);
      onLogin(s);
    } catch (e: any) {
      setErr(e?.message || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  function setField(key: keyof typeof EMPTY_APPLICATION, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function submitApplication(e: React.FormEvent) {
    e.preventDefault();
    setApplyErr("");

    if (!form.company_name.trim() || !form.contact_name.trim()) {
      setApplyErr("请填写公司名称和联系人");
      return;
    }
    if (!form.phone.trim() && !form.wechat.trim() && !form.email.trim()) {
      setApplyErr("请至少填写手机号、微信号或邮箱中的一项，方便销售联系你");
      return;
    }

    const budget = form.monthly_budget_expect.trim();
    if (budget && Number.isNaN(Number(budget))) {
      setApplyErr("预计月消耗请填写数字");
      return;
    }

    setApplyBusy(true);
    try {
      const res = await api.post<{ message: string }>("portal", "/api/public/client-applications", {
        company_name: form.company_name.trim(),
        contact_name: form.contact_name.trim(),
        phone: form.phone.trim() || null,
        wechat: form.wechat.trim() || null,
        email: form.email.trim() || null,
        platform: form.platform,
        monthly_budget_expect: budget ? Number(budget) : 0,
        remark: form.remark.trim() || null,
      });
      setApplyMessage(res?.message || "申请已提交，销售会在 1 个工作日内联系你");
      setApplied(true);
    } catch (e: any) {
      setApplyErr(e?.message || "提交失败，请稍后再试");
    } finally {
      setApplyBusy(false);
    }
  }

  function resetApplication() {
    setForm(EMPTY_APPLICATION);
    setApplyErr("");
    setApplied(false);
    setApplyMessage("");
  }

  function switchMode(next: "apply" | "login") {
    setMode(next);
    if (!isPortal) return;
    const url = new URL(window.location.href);
    if (next === "login") url.searchParams.set("login", "1");
    else url.searchParams.delete("login");
    window.history.replaceState(null, "", url);
  }

  const loginForm = (
    <form className="login-card" onSubmit={submit}>
      <h1 className="login-title">{isPortal ? "海外客户端系统" : "海外广告代理业务系统"}</h1>

      {err && <div className="err">{err}</div>}

      <div className="field">
        <label>{isPortal ? "登录邮箱" : "账号"}</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          placeholder={isPortal ? "client@example.com" : ""}
        />
      </div>
      <div className="field">
        <label>密码</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
      </div>

      <button className="btn primary block" disabled={busy} type="submit">
        {busy ? "登录中…" : "登录"}
      </button>

      {isPortal && (
        <div className="hint">用客户经理提供的登录邮箱和初始密码登录，忘记密码请联系客户经理重置。</div>
      )}

      {!isPortal && isDev && (
        <div className="hint">
          测试账号（密码都在这）：<br />
          <code>admin / admin123</code> 管理员 · <code>sales01</code> 销售 · <code>risk01</code> 风控
          <br />
          <code>finance01</code> 财务 · <code>ops01</code> 运营，密码均 <code>123456</code>
        </div>
      )}
    </form>
  );

  // 后台端保持原来的居中登录卡；门户端改成「开户申请 + 右上角登录」。
  if (!isPortal) {
    return <div className="login-wrap">{loginForm}</div>;
  }

  const brand = (
    <div className="public-brand">
      <span className="public-mark">OC</span>
      <div>
        <b>海外客户端系统</b>
        <span>{mode === "apply" ? "广告账户开户申请" : "客户登录"}</span>
      </div>
    </div>
  );

  if (mode === "login") {
    return (
      <div className="public-page">
        <header className="public-nav">
          {brand}
          <button className="btn public-login-btn" onClick={() => switchMode("apply")}>
            返回开户申请
          </button>
        </header>
        <main className="public-login-main">{loginForm}</main>
      </div>
    );
  }

  return (
    <div className="public-page">
      <header className="public-nav">
        {brand}
        <button className="btn public-login-btn" onClick={() => switchMode("login")}>
          登录
        </button>
      </header>

      <main className="public-main">
        <section className="public-hero">
          <span className="public-kicker">TikTok 广告投放开户</span>
          <h1>开立你的广告账户</h1>
          <p>
            留下基础联系方式即可提交申请。专属销售会在 1 个工作日内联系你，
            协助完成资质确认、开户和充值，不需要现在就填写完整企业资料。
          </p>
          <ul className="public-points">
            <li>
              <b>只留联系方式</b>
              <span>公司、联系人、手机 / 微信 / 邮箱，够销售找到你就行。</span>
            </li>
            <li>
              <b>销售一对一跟进</b>
              <span>提交后进入管理端客户申请列表，由销售认领并主动联系。</span>
            </li>
            <li>
              <b>开户后再登录</b>
              <span>正式建档完成后，客户经理会给你门户登录邮箱和初始密码。</span>
            </li>
          </ul>
        </section>

        <section className="public-card">
          {applied ? (
            <div className="apply-success">
              <div className="apply-success-mark">✓</div>
              <h2>申请已收到</h2>
              <p>{applyMessage}</p>
              <div className="apply-next">
                <span>接下来</span>
                <ol>
                  <li>销售查看你的申请并电话 / 微信联系</li>
                  <li>确认投放需求与开户资料</li>
                  <li>完成开户后，客户经理提供门户账号</li>
                </ol>
              </div>
              <div className="row" style={{ marginTop: 18 }}>
                <button className="btn primary" onClick={() => switchMode("login")}>
                  已有账号，去登录
                </button>
                <button className="btn" onClick={resetApplication}>
                  再提交一份
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={submitApplication}>
              <div className="public-card-head">
                <h2>开户申请</h2>
                <p>带 * 为必填，30 秒填完。</p>
              </div>

              {applyErr && <div className="err">{applyErr}</div>}

              <div className="apply-grid">
                <div className="full">
                  <label>公司名称 *</label>
                  <input
                    value={form.company_name}
                    onChange={(e) => setField("company_name", e.target.value)}
                    maxLength={200}
                    autoComplete="organization"
                  />
                </div>
                <div>
                  <label>联系人 *</label>
                  <input
                    value={form.contact_name}
                    onChange={(e) => setField("contact_name", e.target.value)}
                    maxLength={60}
                    autoComplete="name"
                  />
                </div>
                <div>
                  <label>手机号</label>
                  <input
                    value={form.phone}
                    onChange={(e) => setField("phone", e.target.value)}
                    maxLength={30}
                    autoComplete="tel"
                  />
                </div>
                <div>
                  <label>微信号</label>
                  <input
                    value={form.wechat}
                    onChange={(e) => setField("wechat", e.target.value)}
                    maxLength={60}
                  />
                </div>
                <div>
                  <label>邮箱</label>
                  <input
                    type="email"
                    value={form.email}
                    onChange={(e) => setField("email", e.target.value)}
                    maxLength={120}
                    autoComplete="email"
                  />
                </div>
                <div>
                  <label>意向平台</label>
                  <select
                    value={form.platform}
                    onChange={(e) => setField("platform", e.target.value)}
                  >
                    {platforms.map((p) => (
                      <option key={p.code} value={p.code}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>预计月消耗（美元）</label>
                  <input
                    type="number"
                    min={0}
                    step={100}
                    value={form.monthly_budget_expect}
                    onChange={(e) => setField("monthly_budget_expect", e.target.value)}
                  />
                </div>
                <div className="full">
                  <label>需求备注（可选）</label>
                  <textarea
                    rows={3}
                    value={form.remark}
                    onChange={(e) => setField("remark", e.target.value)}
                    maxLength={1000}
                  />
                </div>
              </div>

              <button className="btn primary block" type="submit" disabled={applyBusy}>
                {applyBusy ? "提交中…" : "提交开户申请"}
              </button>
              <div className="apply-note">
                提交后不会立即创建登录账号；销售联系你并完成正式建档后，才会开通门户账号。
              </div>
            </form>
          )}
        </section>
      </main>

      <footer className="public-foot">
        <span>已有门户账号？</span>
        <button className="link-btn" onClick={() => switchMode("login")}>
          直接登录
        </button>
      </footer>
    </div>
  );
}
