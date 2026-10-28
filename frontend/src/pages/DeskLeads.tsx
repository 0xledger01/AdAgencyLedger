import { useCallback, useEffect, useRef, useState } from "react";
import { api, hasPerm, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface Item {
  id: number;
  biz_no: string | null;
  company_name: string;
  contact_name: string;
  phone: string | null;
  wechat: string | null;
  email: string | null;
  platform: string;
  platform_label: string;
  monthly_budget_expect: string;
  remark: string | null;
  source: string;
  source_label: string;
  status: string;
  status_label: string;
  next_statuses: string[];
  owner: string | null;
  note: string | null;
  close_reason: string | null;
  contacted_at: string | null;
  created_at: string;
  updated_at: string;
}

interface Summary {
  counts: Record<string, number>;
  total: number;
  new: number;
}

interface ListResp {
  total: number;
  limit: number;
  offset: number;
  summary: Summary;
  items: Item[];
}

interface EnumOpt {
  code: string;
  label: string;
}

interface Enums {
  client_lead_status: EnumOpt[];
  platform: EnumOpt[];
}

interface Filters {
  keyword: string;
  platform: string;
}

const EMPTY_FILTERS: Filters = { keyword: "", platform: "" };
const PAGE = 20;

function optionLabel(options: EnumOpt[], code: string): string {
  return options.find((o) => o.code === code)?.label ?? code;
}

export default function DeskLeads({ session }: { session: Session }) {
  const canHandle = hasPerm(session, "client.lead.handle");
  const [enums, setEnums] = useState<Enums | null>(null);
  const [tab, setTab] = useState("");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [query, setQuery] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [items, setItems] = useState<Item[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const [detail, setDetail] = useState<Item | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState("");
  const [form, setForm] = useState({ status: "", owner: "", note: "", close_reason: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [copied, setCopied] = useState("");

  const seqRef = useRef(0);
  const detailSeqRef = useRef(0);

  useEffect(() => {
    api
      .get<Enums>("desk", "/api/desk/enums")
      .then((d) => setEnums(d))
      .catch(() => setEnums(null));
  }, []);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const path = `/api/desk/client-leads${qs({
        status: tab || undefined,
        keyword: query.keyword.trim() || undefined,
        platform: query.platform || undefined,
        limit: PAGE,
        offset: page * PAGE,
      })}`;
      const d = await api.get<ListResp>("desk", path);
      if (seq !== seqRef.current) return;
      setItems(d.items ?? []);
      setSummary(d.summary);
      setTotal(d.total);
    } catch (e: any) {
      if (seq !== seqRef.current) return;
      setErr(e?.message || "加载失败");
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [tab, query, page]);

  useEffect(() => {
    load();
  }, [load]);

  function runQuery() {
    setPage(0);
    setQuery({ ...filters });
  }

  function resetQuery() {
    setFilters(EMPTY_FILTERS);
    setQuery(EMPTY_FILTERS);
    setPage(0);
  }

  function pickTab(code: string) {
    setTab(code);
    setPage(0);
  }

  async function openDetail(row: Item) {
    detailSeqRef.current += 1;
    setDetailOpen(true);
    setDetail(null);
    setMsg("");
    setDetailErr("");
    setCopied("");
    setDetailLoading(true);
    const seq = detailSeqRef.current;
    try {
      const d = await api.get<Item>("desk", `/api/desk/client-leads/${row.id}`);
      if (seq !== detailSeqRef.current) return;
      setDetail(d);
      setForm({
        status: d.status,
        owner: d.owner ?? (session.role === "SALES" ? session.display_name : ""),
        note: d.note ?? "",
        close_reason: d.close_reason ?? "",
      });
    } catch (e: any) {
      if (seq !== detailSeqRef.current) return;
      setDetailErr(e?.message || "加载详情失败");
    } finally {
      if (seq === detailSeqRef.current) setDetailLoading(false);
    }
  }

  function closeDetail() {
    detailSeqRef.current += 1;
    setDetailOpen(false);
    setDetail(null);
    setBusy(false);
    setMsg("");
  }

  async function save(statusOverride?: string) {
    if (!detail) return;
    const targetStatus = statusOverride || form.status;
    if (targetStatus === "CLOSED" && !(form.close_reason || "").trim()) {
      setDetailErr("关闭申请时请填写原因，方便后续复盘");
      return;
    }
    setBusy(true);
    setMsg("");
    setDetailErr("");
    try {
      const d = await api.post<Item>("desk", `/api/desk/client-leads/${detail.id}/handle`, {
        status: targetStatus,
        owner: form.owner.trim() || null,
        note: form.note.trim() || null,
        close_reason: targetStatus === "CLOSED" ? form.close_reason.trim() || null : null,
      });
      setDetail(d);
      setForm({
        status: d.status,
        owner: d.owner ?? "",
        note: d.note ?? "",
        close_reason: d.close_reason ?? "",
      });
      setMsg(
        statusOverride === "CONTACTED"
          ? "已标记为已联系，跟进人已记到当前账号"
          : "已保存"
      );
      await load();
    } catch (e: any) {
      setDetailErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function copy(text: string | null, key: string) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      window.setTimeout(() => setCopied(""), 1500);
    } catch {
      setCopied("");
    }
  }

  const statusOptions = enums?.client_lead_status ?? [];
  const platformOptions = enums?.platform ?? [];
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const counts = summary?.counts ?? {};

  return (
    <>
      <div className="subtabs">
        <button type="button" className={tab === "" ? "on" : ""} onClick={() => pickTab("")}>
          全部
          <span className="badge gray" style={{ marginLeft: 6 }}>
            {summary?.total ?? 0}
          </span>
        </button>
        {statusOptions.map((o) => (
          <button
            key={o.code}
            type="button"
            className={tab === o.code ? "on" : ""}
            onClick={() => pickTab(o.code)}
          >
            {o.label}
            <span className="badge gray" style={{ marginLeft: 6 }}>
              {counts[o.code] ?? 0}
            </span>
          </button>
        ))}
      </div>

      <div className="card">
        <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="toolbar">
            <div className="field">
              <label>关键词</label>
              <input
                value={filters.keyword}
                onChange={(e) => setFilters({ ...filters, keyword: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") runQuery();
                }}
                placeholder="公司 / 联系人 / 手机 / 微信 / 邮箱 / 编号"
                style={{ minWidth: 260 }}
              />
            </div>
            <div className="field">
              <label>意向平台</label>
              <select
                value={filters.platform}
                onChange={(e) => setFilters({ ...filters, platform: e.target.value })}
              >
                <option value="">平台：全部</option>
                {platformOptions.map((o) => (
                  <option key={o.code} value={o.code}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <button className="btn primary" onClick={runQuery} disabled={loading}>
              查询
            </button>
            <button className="btn" onClick={resetQuery} disabled={loading}>
              重置
            </button>
            <span className="spacer" />
            <span className="faint">
              {summary?.new ? `待联系 ${summary.new} 条` : "没有待联系的申请"}
            </span>
          </div>
          {err && <div className="err" style={{ marginTop: 10, marginBottom: 0 }}>{err}</div>}
        </div>

        <table>
          <thead>
            <tr>
              <th>提交时间</th>
              <th>公司 / 联系人</th>
              <th>联系方式</th>
              <th>意向平台</th>
              <th className="num">预计月消耗</th>
              <th>状态</th>
              <th>跟进人</th>
              <th className="right">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td className="mono faint" style={{ whiteSpace: "nowrap" }}>
                  {fmtTime(row.created_at)}
                </td>
                <td>
                  <button type="button" className="link-btn" onClick={() => openDetail(row)}>
                    {row.company_name}
                  </button>
                  <div className="faint" style={{ fontSize: 11 }}>
                    {row.contact_name}
                    {row.biz_no ? ` · ${row.biz_no}` : ""}
                  </div>
                </td>
                <td style={{ maxWidth: 230 }}>
                  {row.phone && <div className="mono">{row.phone}</div>}
                  {row.wechat && <div className="faint">微信 {row.wechat}</div>}
                  {row.email && <div className="faint">{row.email}</div>}
                  {!row.phone && !row.wechat && !row.email && "—"}
                </td>
                <td>{row.platform_label}</td>
                <td className="num mono">{money(row.monthly_budget_expect)}</td>
                <td>
                  <span className={`badge ${badgeClass(row.status)}`}>{row.status_label}</span>
                </td>
                <td className="muted" style={{ whiteSpace: "nowrap" }}>
                  {row.owner || "未认领"}
                </td>
                <td className="right">
                  <button
                    className={`btn sm ${canHandle ? "primary" : ""}`}
                    onClick={() => openDetail(row)}
                    disabled={busy}
                  >
                    {canHandle ? "跟进" : "查看"}
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8}>
                  <div className="empty">{loading ? "加载中…" : "没有符合条件的客户申请"}</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
          <div className="row">
            <span>
              共 {total} 条 · 第 {page + 1} / {pages} 页
            </span>
            <span className="spacer" />
            <button
              className="btn sm"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={loading || page === 0}
            >
              上一页
            </button>
            <button
              className="btn sm"
              onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
              disabled={loading || page >= pages - 1}
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      {detailOpen && (
        <div className="drawer-mask" onClick={() => !busy && closeDetail()}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>{detail ? `客户申请 · ${detail.company_name}` : "客户申请详情"}</h2>
                <p>{detail ? `${detail.biz_no || ""} · ${detail.source_label}` : "正在加载…"}</p>
              </div>
              <button className="btn ghost sm" onClick={closeDetail}>
                关闭
              </button>
            </div>

            <div className="drawer-body">
              {detailLoading && <div className="empty">加载中…</div>}
              {detailErr && <div className="err">{detailErr}</div>}
              {msg && <div className="warnbox">{msg}</div>}

              {detail && (
                <>
                  <div className="kv" style={{ marginBottom: 14 }}>
                    <span className="k">状态</span>
                    <span className="v">
                      <span className={`badge ${badgeClass(detail.status)}`}>
                        {detail.status_label}
                      </span>
                    </span>
                    <span className="k">提交时间</span>
                    <span className="v mono">{fmtTime(detail.created_at)}</span>
                    <span className="k">最近更新</span>
                    <span className="v mono">{fmtTime(detail.updated_at)}</span>
                    <span className="k">意向平台</span>
                    <span className="v">{detail.platform_label}</span>
                    <span className="k">预计月消耗</span>
                    <span className="v mono">{money(detail.monthly_budget_expect)}</span>
                    <span className="k">跟进人</span>
                    <span className="v">{detail.owner || "未认领"}</span>
                    <span className="k">首次联系</span>
                    <span className="v mono">{fmtTime(detail.contacted_at)}</span>
                  </div>

                  <div className="lead-contact">
                    <div className="lead-contact-row">
                      <span className="k">联系人</span>
                      <span className="v">{detail.contact_name}</span>
                    </div>
                    <div className="lead-contact-row">
                      <span className="k">手机</span>
                      <span className="v mono">{detail.phone || "—"}</span>
                      {detail.phone && (
                        <button className="btn ghost sm" onClick={() => copy(detail.phone, "phone")}>
                          {copied === "phone" ? "已复制" : "复制"}
                        </button>
                      )}
                    </div>
                    <div className="lead-contact-row">
                      <span className="k">微信</span>
                      <span className="v mono">{detail.wechat || "—"}</span>
                      {detail.wechat && (
                        <button className="btn ghost sm" onClick={() => copy(detail.wechat, "wechat")}>
                          {copied === "wechat" ? "已复制" : "复制"}
                        </button>
                      )}
                    </div>
                    <div className="lead-contact-row">
                      <span className="k">邮箱</span>
                      <span className="v mono">{detail.email || "—"}</span>
                      {detail.email && (
                        <button className="btn ghost sm" onClick={() => copy(detail.email, "email")}>
                          {copied === "email" ? "已复制" : "复制"}
                        </button>
                      )}
                    </div>
                  </div>

                  <div className="sub-title">客户留言</div>
                  <div className="ticket-text">{detail.remark?.trim() || "（未填写）"}</div>

                  {canHandle ? (
                    <>
                      <div className="sub-title">跟进处理</div>
                      <div className="form-grid">
                        <div>
                          <label>状态</label>
                          <select
                            value={form.status}
                            onChange={(e) => setForm({ ...form, status: e.target.value })}
                          >
                            {detail.next_statuses.map((code) => (
                              <option key={code} value={code}>
                                {optionLabel(statusOptions, code)}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label>跟进人</label>
                          <input
                            value={form.owner}
                            onChange={(e) => setForm({ ...form, owner: e.target.value })}
                            placeholder="未认领"
                            disabled={session.role === "SALES"}
                          />
                        </div>
                        <div className="full">
                          <label>跟进备注（内部可见，不会发给客户）</label>
                          <textarea
                            rows={4}
                            value={form.note}
                            onChange={(e) => setForm({ ...form, note: e.target.value })}
                            placeholder="例如：已电话联系，客户下周给素材；或暂不合作的原因"
                          />
                        </div>
                        {form.status === "CLOSED" && (
                          <div className="full">
                            <label>关闭原因</label>
                            <input
                              value={form.close_reason}
                              onChange={(e) => setForm({ ...form, close_reason: e.target.value })}
                              placeholder="例如：号码无效 / 已在他处开户 / 预算不匹配"
                            />
                          </div>
                        )}
                      </div>
                      <div className="row" style={{ marginTop: 12 }}>
                        <button className="btn primary" onClick={() => save()} disabled={busy}>
                          {busy ? "保存中…" : "保存"}
                        </button>
                        {detail.status === "NEW" && (
                          <button
                            className="btn"
                            onClick={() => save("CONTACTED")}
                            disabled={busy}
                          >
                            标记已联系
                          </button>
                        )}
                      </div>
                      <div className="faint" style={{ marginTop: 8, lineHeight: 1.6 }}>
                        线索转客户后，请到「客户管理」正式建档并填写入驻资料。
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="sub-title">内部跟进</div>
                      <div className="ticket-text">{detail.note?.trim() || "还没有跟进记录"}</div>
                    </>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
