import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api, getSession, hasPerm, loadSession, setSession, type Session } from "./api";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import DeskAccounts from "./pages/DeskAccounts";
import DeskClients from "./pages/DeskClients";
import DeskLedger from "./pages/DeskLedger";
import DeskLeads from "./pages/DeskLeads";
import DeskOpen from "./pages/DeskOpen";
import DeskOverview from "./pages/DeskOverview";
import DeskSpend from "./pages/DeskSpend";
import DeskTickets from "./pages/DeskTickets";
import DeskTopups from "./pages/DeskTopups";
// 「交易记录（管理端）」已按要求注释掉；恢复时把这一行 import 一起放开。
// import DeskTransactions from "./pages/DeskTransactions";
import PortalAccounts from "./pages/PortalAccounts";
import PortalBc from "./pages/PortalBc";
import PortalOpen from "./pages/PortalOpen";
import PortalTransactions from "./pages/PortalTransactions";
import PortalWallet from "./pages/PortalWallet";
import { badgeClass, fmtTime } from "./utils";

/**
 * 菜单项带 perm：由后端下发的权限码决定是否渲染。
 * perm = null 表示全员可见。
 */
const DESK_NAV: { key: string; label: string; perm: string | null }[] = [
  { key: "overview", label: "资金总览", perm: "wallet.view" },
  { key: "clients", label: "客户管理", perm: "client.list" },
  { key: "leads", label: "客户申请", perm: "client.lead.view" },
  { key: "topups", label: "充值管理", perm: "topup.view" },
  { key: "open", label: "开户管理", perm: "open_request.view" },
  { key: "accounts", label: "账户管理", perm: "ad_account.view" },
  // 「BC 管理」已按要求从管理端去掉（BCID 交给客户在门户维护）；
  // 需要恢复时把 bc 这一条加回来，并补回 DeskBc 页面与 activePage 分支。
  // 「交易记录」已按要求从管理端注释掉（客户门户的「交易记录」保留）；
  // 需要恢复时把下面这一条菜单、上面的 import、下面的 activePage 分支一起放开。
  // { key: "transactions", label: "交易记录", perm: "recharge.view" },
  { key: "spend", label: "消耗报表", perm: "spend.import" },
  { key: "ledger", label: "资金流水", perm: "ledger.view" },
  { key: "tickets", label: "问题工单", perm: "ticket.view" },
  { key: "profile", label: "我的档案", perm: null },
];

const PORTAL_NAV: { key: string; label: string; perm: string | null }[] = [
  { key: "wallet", label: "我的钱包", perm: null },
  { key: "adaccounts", label: "广告账户", perm: null },
  { key: "bc", label: "BC 管理", perm: null },
  { key: "openreq", label: "开户申请", perm: null },
  { key: "transactions", label: "交易记录", perm: null },
  { key: "tickets", label: "问题工单", perm: null },
  // 客户端侧栏「账号信息」已按要求隐藏，需要时去掉注释符即可恢复。
  // { key: "profile", label: "账号信息", perm: null },
];

/** URL 决定端：/portal 走门户，其余走后台。 */
function resolveKind(): "desk" | "portal" {
  return window.location.pathname.startsWith("/portal") ? "portal" : "desk";
}

/**
 * `/portal` 根地址是门户的登录入口。
 * 直接进这个地址时必须先看到登录页，不能拿上次的会话静默进站——
 * 共用电脑上，上一位客户的票会直接变成下一位的「已登录」。
 * 登录后地址会变成 /portal/<page>，刷新和深链接仍然保持会话。
 */
function isPortalEntry(): boolean {
  const seg = window.location.pathname.split("/").filter(Boolean);
  return seg.length === 1 && seg[0] === "portal";
}

/**
 * 页面写在 URL 上：/desk/ledger、/portal/wallet。
 * 以前 page 只是内存 state，刷新就回首页，链接也发不出去（F-10）。
 */
function readPage(kind: "desk" | "portal", fallback: string): string {
  const seg = window.location.pathname.split("/").filter(Boolean);
  const candidate = kind === "portal" ? seg[1] : seg[0] === "desk" ? seg[1] : seg[0];
  return candidate || fallback;
}

function writeUrl(kind: "desk" | "portal", page: string) {
  const path = kind === "portal" ? `/portal/${page}` : `/desk/${page}`;
  if (window.location.pathname !== path) {
    window.history.pushState({ page }, "", path);
  }
}

export default function App() {
  const [kind] = useState<"desk" | "portal">(resolveKind);
  const isPortal = kind === "portal";
  const [session, setSessionState] = useState<Session | null>(() =>
    isPortalEntry() ? null : getSession(kind)
  );
  const [booting, setBooting] = useState(true);
  const [page, setPage] = useState(() =>
    readPage(kind, resolveKind() === "portal" ? "wallet" : "overview")
  );

  // 浏览器标签标题随端切换：客户端（门户）是「海外客户端系统」，内部后台保留系统全名。
  useEffect(() => {
    document.title = kind === "portal" ? "海外客户端系统" : "海外广告代理业务系统";
  }, [kind]);

  // 门户根地址 = 登录入口：清掉残留的门户会话，直接渲染登录页。
  // 只认根地址，不碰 /portal/<page>，所以页内刷新不会被踢出去。
  useEffect(() => {
    if (kind === "portal" && isPortalEntry()) {
      setSession("portal", null);
      setSessionState(null);
      setBooting(false);
    }
  }, [kind]);

  // 启动时用 /api/auth/me 校验 token 并刷新 perms：失效就直接回登录页，
  // 而不是让每个页面各自 401（F-03 ⑤）
  useEffect(() => {
    if (kind === "portal" && isPortalEntry()) return;
    let alive = true;
    loadSession(kind)
      .then((s) => {
        if (!alive) return;
        setSessionState(s);
      })
      .finally(() => {
        if (alive) setBooting(false);
      });
    return () => {
      alive = false;
    };
  }, [kind]);

  // 浏览器前进/后退跟随 URL
  useEffect(() => {
    const onPop = () => setPage(readPage(kind, kind === "portal" ? "wallet" : "overview"));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [kind]);

  const go = useCallback(
    (key: string) => {
      setPage(key);
      writeUrl(kind, key);
    },
    [kind]
  );

  function onLogin(s: Session) {
    setSessionState(s);
    go(s.role === "CLIENT" ? "wallet" : "overview");
  }

  function logout() {
    setSession(kind, null);
    setSessionState(null);
    window.history.replaceState(null, "", isPortal ? "/portal" : "/");
  }

  if (booting) {
    return (
      <div className="login-wrap">
        <div className="login-card">
          <div className="empty">正在校验登录状态…</div>
        </div>
      </div>
    );
  }

  if (!session) return <Login kind={kind} onLogin={onLogin} />;

  const isClient = session.role === "CLIENT";
  const allNav = isClient ? PORTAL_NAV : DESK_NAV;
  // 菜单按权限过滤：以前 8 项全渲染，sales01 点进去 3 个直接 403
  const nav = allNav.filter((n) => !n.perm || hasPerm(session, n.perm));
  const allowedKeys = new Set(nav.map((n) => n.key));
  // 老链接 /desk/recharge：「加款任务」已并入账户管理的「充值记录」Tab
  // 老链接 /portal/onboarding：「入驻进度」已并入开户申请页，按记录点「入驻进度」查看
  const normalizedPage =
    page === "recharge" ? "accounts" : page === "onboarding" ? "openreq" : page;
  const activePage = allowedKeys.has(normalizedPage) ? normalizedPage : nav[0]?.key ?? "profile";

  // 各模块页头的说明副标题已按需求全部去掉，只保留模块名。
  const titles: Record<string, string> = {
    overview: "资金总览",
    clients: "客户管理",
    leads: "客户申请",
    topups: "充值管理",
    open: "开户管理",
    accounts: "账户管理",
    bc: "BC 管理",
    transactions: "交易记录",
    spend: "消耗报表",
    ledger: "资金流水",
    wallet: "我的钱包",
    adaccounts: "广告账户",
    openreq: "开户申请",
    tickets: "问题工单",
    profile: "我的档案",
  };
  const title = titles[activePage] || "";

  return (
    <Layout
      session={session}
      nav={nav.map(({ key, label }) => ({ key, label }))}
      active={activePage}
      onNav={go}
      onLogout={logout}
      title={title}
    >
      {activePage === "overview" && !isClient && <DeskOverview session={session} />}
      {activePage === "clients" && !isClient && <DeskClients session={session} />}
      {activePage === "leads" && !isClient && <DeskLeads session={session} />}
      {activePage === "topups" && !isClient && <DeskTopups session={session} />}
      {activePage === "open" && !isClient && <DeskOpen session={session} />}
      {activePage === "accounts" && !isClient && <DeskAccounts session={session} />}
      {/* 管理端「交易记录」已按要求注释掉（页面文件保留在 pages/DeskTransactions.tsx）。 */}
      {/* {activePage === "transactions" && !isClient && <DeskTransactions session={session} />} */}
      {activePage === "spend" && !isClient && <DeskSpend session={session} />}
      {activePage === "ledger" && !isClient && <DeskLedger session={session} />}
      {activePage === "tickets" && !isClient && <DeskTickets session={session} />}
      {activePage === "wallet" && isClient && <PortalWallet session={session} />}
      {activePage === "adaccounts" && isClient && <PortalAccounts session={session} />}
      {activePage === "bc" && isClient && <PortalBc session={session} />}
      {activePage === "openreq" && isClient && <PortalOpen session={session} />}
      {activePage === "transactions" && isClient && <PortalTransactions session={session} />}
      {activePage === "tickets" && isClient && <PortalTickets session={session} />}
      {activePage === "profile" && <ProfilePage session={session} isClient={isClient} />}
    </Layout>
  );
}

function PortalTickets({ session }: { session: Session }) {
  const [items, setItems] = useState<any[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // 工单详情：点行展开，按需拉单条详情（列表里回复可能已更新）
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState("");
  const detailSeq = useRef(0);

  const load = useCallback(async () => {
    try {
      const d = await api.get<{ items: any[] }>("portal", "/api/portal/tickets");
      setItems(d.items ?? []);
    } catch (e: any) {
      setErr(e?.message || "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function toggleDetail(row: any) {
    if (openId === row.id) {
      detailSeq.current += 1; // 作废还在路上的请求
      setOpenId(null);
      setDetail(null);
      setDetailErr("");
      setDetailLoading(false);
      return;
    }
    const seq = ++detailSeq.current;
    setOpenId(row.id);
    setDetail(null);
    setDetailErr("");
    setDetailLoading(true);
    try {
      const d = await api.get<any>("portal", `/api/portal/tickets/${row.id}`);
      if (seq !== detailSeq.current) return;
      setDetail(d);
    } catch (e: any) {
      if (seq !== detailSeq.current) return;
      setDetailErr(e?.message || "工单详情加载失败");
    } finally {
      if (seq === detailSeq.current) setDetailLoading(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setMsg("");
    setErr("");
    try {
      await api.post("portal", "/api/portal/tickets", {
        title: title.trim(),
        content: content.trim() || null,
      });
      setTitle("");
      setContent("");
      setMsg("已提交，我们会尽快联系你");
      setOpenId(null);
      setDetail(null);
      detailSeq.current += 1;
      await load();
    } catch (e: any) {
      setErr(e?.message || "提交失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="card">
        <div className="card-head">
          <h2>提交问题</h2>
          <span className="faint">{session.display_name}</span>
        </div>
        <div className="card-body">
          <form onSubmit={submit} className="form-grid">
            <div className="full">
              <label>标题</label>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="账户余额对不上" />
            </div>
            <div className="full">
              <label>详细说明（可选）</label>
              <textarea rows={3} value={content} onChange={(e) => setContent(e.target.value)} />
            </div>
            <div className="full">
              <button className="btn primary" disabled={busy || !title.trim()}>
                提交
              </button>
            </div>
          </form>
          {msg && <div className="warnbox" style={{ marginTop: 10 }}>{msg}</div>}
          {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>我的工单</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th>编号</th>
              <th>标题</th>
              <th>状态</th>
              <th>处理说明</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <Fragment key={t.id}>
                <tr
                  className={openId === t.id ? "sel" : undefined}
                  style={{ cursor: "pointer" }}
                  onClick={() => toggleDetail(t)}
                >
                  <td className="mono faint">#{t.id}</td>
                  <td>{t.title}</td>
                  <td>
                    <span className={`badge ${badgeClass(t.status)}`}>
                      {t.status_label || t.status}
                    </span>
                  </td>
                  <td className="muted" style={{ whiteSpace: "pre-wrap", maxWidth: 360 }}>
                    {t.reply || "—"}
                  </td>
                  <td className="mono faint">{fmtTime(t.created_at)}</td>
                  <td>
                    <button
                      className="btn ghost sm"
                      aria-expanded={openId === t.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleDetail(t);
                      }}
                    >
                      {openId === t.id ? "收起" : "查看"}
                    </button>
                  </td>
                </tr>
                {openId === t.id && (
                  <tr className="ticket-detail-row">
                    <td colSpan={6}>
                      {detailLoading && <div className="empty">加载中…</div>}
                      {detailErr && <div className="err">{detailErr}</div>}
                      {!detailLoading && !detailErr && detail && (
                        <div className="ticket-detail">
                          <div className="kv">
                            <span className="k">状态</span>
                            <span className="v">
                              <span className={`badge ${badgeClass(detail.status)}`}>
                                {detail.status_label || detail.status}
                              </span>
                            </span>
                            <span className="k">提交时间</span>
                            <span className="v mono">{fmtTime(detail.created_at)}</span>
                            <span className="k">最近更新</span>
                            <span className="v mono">{fmtTime(detail.updated_at)}</span>
                          </div>
                          <div className="sub-title" style={{ marginTop: 12 }}>
                            问题描述
                          </div>
                          <div className="ticket-text">
                            {detail.content?.trim() || "（提交时未填写详细说明）"}
                          </div>
                          <div className="sub-title">处理说明</div>
                          {detail.reply?.trim() ? (
                            <>
                              <div className="ticket-text">{detail.reply}</div>
                              {detail.handled_at && (
                                <div className="faint mono" style={{ marginTop: 6 }}>
                                  处理时间 {fmtTime(detail.handled_at)}
                                </div>
                              )}
                            </>
                          ) : (
                            <div className="ticket-text faint">
                              还没有处理回复，我们会尽快跟进。
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={6}>
                  <div className="empty">还没有提交过工单</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** 权限展示：中文名优先；后端没给翻译时退回权限码，至少不出现空白。 */
function permChips(session: Session): { code: string; label: string }[] {
  return (session.perms ?? []).map((code) => ({
    code,
    label: session.perm_labels?.[code] ?? code,
  }));
}

function ProfilePage({ session, isClient }: { session: Session; isClient: boolean }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>当前身份</h2>
      </div>
      <div className="card-body kv">
        <span className="k">账号</span>
        <span className="v mono">{session.username}</span>
        <span className="k">姓名</span>
        <span className="v">{session.display_name}</span>
        <span className="k">角色</span>
        <span className="v">
          <span className={`badge ${badgeClass("ACTIVE")}`}>{session.role_label}</span>
        </span>
        {!isClient && (
          <>
            <span className="k">持有权限</span>
            <span className="v" style={{ fontSize: 13, lineHeight: 1.9 }}>
              {permChips(session).length === 0 && "—"}
              {permChips(session).map(({ code, label }) => (
                <span key={code} className="badge gray" title={code} style={{ marginRight: 6 }}>
                  {label}
                </span>
              ))}
            </span>
          </>
        )}
        {isClient && (
          <>
            <span className="k">客户档案</span>
            <span className="v mono">#{session.client_id ?? "—"}</span>
          </>
        )}
      </div>
    </div>
  );
}
