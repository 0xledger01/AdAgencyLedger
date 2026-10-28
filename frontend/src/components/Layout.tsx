import type { ReactNode } from "react";
import type { Session } from "../api";

interface Props {
  session: Session;
  nav: { key: string; label: string }[];
  active: string;
  onNav: (key: string) => void;
  onLogout: () => void;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}

export default function Layout({
  session,
  nav,
  active,
  onNav,
  onLogout,
  title,
  actions,
  children,
}: Props) {
  // 品牌名分端：客户端（门户）叫「海外客户端系统」，内部后台保持「海外广告代理」。
  const brand = session.role === "CLIENT" ? "海外客户端系统" : "海外广告代理";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <b>{brand}</b>
          {/* 副标题「业务系统 v0.2」已按要求注释，需要时去掉注释符即可恢复。 */}
          {/* <span>业务系统 v0.2</span> */}
        </div>

        {nav.map((n) => (
          <button
            key={n.key}
            className={`nav-item ${active === n.key ? "on" : ""}`}
            onClick={() => onNav(n.key)}
          >
            <span className="dot" />
            {n.label}
          </button>
        ))}

        <div className="sidebar-foot">
          <div className="who">
            <b>{session.display_name}</b>
            <span>{session.role_label}</span>
          </div>
          <button className="nav-item" onClick={onLogout}>
            退出登录
          </button>
        </div>
      </aside>

      <main className="main">
        <div className="page-head">
          <div>
            <h1>{title}</h1>
          </div>
          {actions && <div className="row">{actions}</div>}
        </div>
        {children}
      </main>
    </div>
  );
}
