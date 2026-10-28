import { useCallback, useEffect, useRef, useState } from "react";
import { api, hasPerm, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface Item {
  id: number;
  client_id: number;
  client_name: string | null;
  client_no: string | null;
  title: string;
  category: string | null;
  excerpt: string | null;
  status: string;
  status_label: string;
  priority: string;
  priority_label: string;
  replied: boolean;
  handled_by: string | null;
  handled_at: string | null;
  created_at: string;
  updated_at: string;
}

interface ClientInfo {
  id: number;
  company_name: string;
  biz_no: string | null;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  sales_owner: string | null;
  ae_owner: string | null;
}

interface Detail extends Item {
  content: string | null;
  reply: string | null;
  next_statuses: string[];
  client: ClientInfo | null;
}

interface Summary {
  counts: Record<string, number>;
  total: number;
  pending: number;
  processing: number;
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
  ticket_status: EnumOpt[];
  ticket_priority: EnumOpt[];
}

interface Filters {
  keyword: string;
  client: string;
  priority: string;
}

const EMPTY_FILTERS: Filters = { keyword: "", client: "", priority: "" };
const PAGE = 20;

/** 优先级只影响徽章颜色，中文一律用后端下发的 priority_label。 */
function priorityBadge(code: string): string {
  if (code === "HIGH") return "red";
  if (code === "LOW") return "gray";
  return "blue";
}

function optionLabel(options: EnumOpt[], code: string): string {
  return options.find((o) => o.code === code)?.label ?? code;
}

export default function DeskTickets({ session }: { session: Session }) {
  const canHandle = hasPerm(session, "ticket.handle");
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

  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [form, setForm] = useState({ status: "", priority: "NORMAL", reply: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [detailErr, setDetailErr] = useState("");

  // 连续查询时用递增序号丢弃过期响应，避免慢请求把新结果覆盖回旧数据
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
      const path = `/api/desk/tickets${qs({
        status: tab || undefined,
        priority: query.priority || undefined,
        keyword: query.keyword.trim() || undefined,
        client: query.client.trim() || undefined,
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
    setDetailLoading(true);
    const seq = detailSeqRef.current;
    try {
      const d = await api.get<Detail>("desk", `/api/desk/tickets/${row.id}`);
      if (seq !== detailSeqRef.current) return;
      setDetail(d);
      setForm({ status: d.status, priority: d.priority, reply: "" });
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
  }

  async function save() {
    if (!detail) return;
    setBusy(true);
    setMsg("");
    setDetailErr("");
    try {
      const d = await api.post<Detail>("desk", `/api/desk/tickets/${detail.id}/handle`, {
        status: form.status,
        priority: form.priority,
        reply: form.reply.trim() || null,
      });
      setDetail(d);
      setForm({ status: d.status, priority: d.priority, reply: "" });
      setMsg("已保存；回复内容客户可在门户「问题工单」里看到");
      await load();
    } catch (e: any) {
      setDetailErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  const statusOptions = enums?.ticket_status ?? [];
  const priorityOptions = enums?.ticket_priority ?? [];
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
                placeholder="标题 / 内容 / #编号"
                style={{ minWidth: 200 }}
              />
            </div>
            <div className="field">
              <label>客户</label>
              <input
                value={filters.client}
                onChange={(e) => setFilters({ ...filters, client: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") runQuery();
                }}
                placeholder="客户名称 / 客户编号"
              />
            </div>
            <div className="field">
              <label>优先级</label>
              <select
                value={filters.priority}
                onChange={(e) => setFilters({ ...filters, priority: e.target.value })}
              >
                <option value="">优先级：全部</option>
                {priorityOptions.map((o) => (
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
              {summary?.pending ? `待处理 ${summary.pending} 条` : "没有待处理的工单"}
            </span>
          </div>
          {err && (
            <div className="err" style={{ marginTop: 10, marginBottom: 0 }}>
              {err}
            </div>
          )}
        </div>

        <table>
          <thead>
            <tr>
              <th>编号</th>
              <th>客户</th>
              <th>标题</th>
              <th>优先级</th>
              <th>状态</th>
              <th>提交时间</th>
              <th>最近更新</th>
              <th>处理人</th>
              <th className="right">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.id}>
                <td className="mono faint">#{t.id}</td>
                <td>
                  <div>{t.client_name || "—"}</div>
                  {t.client_no && (
                    <div className="mono faint" style={{ fontSize: 11 }}>
                      {t.client_no}
                    </div>
                  )}
                </td>
                <td style={{ maxWidth: 340 }}>
                  <button type="button" className="link-btn" onClick={() => openDetail(t)}>
                    {t.title}
                  </button>
                  {t.excerpt && (
                    <div className="faint" style={{ fontSize: 11 }}>
                      {t.excerpt}
                    </div>
                  )}
                </td>
                <td>
                  <span className={`badge ${priorityBadge(t.priority)}`}>{t.priority_label}</span>
                </td>
                <td>
                  <span className={`badge ${badgeClass(t.status)}`}>{t.status_label}</span>
                </td>
                <td className="mono faint" style={{ whiteSpace: "nowrap" }}>
                  {fmtTime(t.created_at)}
                </td>
                <td className="mono faint" style={{ whiteSpace: "nowrap" }}>
                  {fmtTime(t.updated_at)}
                </td>
                <td className="muted" style={{ whiteSpace: "nowrap" }}>
                  {t.handled_by || "—"}
                </td>
                <td className="right">
                  <div className="row-btns" style={{ justifyContent: "flex-end" }}>
                    <button
                      className={`btn sm ${canHandle ? "primary" : ""}`}
                      onClick={() => openDetail(t)}
                      disabled={busy}
                    >
                      {canHandle ? "处理" : "查看"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={9}>
                  <div className="empty">{loading ? "加载中…" : "没有符合条件的工单"}</div>
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
                <h2>{detail ? `工单 #${detail.id} · ${detail.title}` : "工单详情"}</h2>
                <p>
                  {detail?.client
                    ? `${detail.client.company_name}${detail.client.biz_no ? ` · ${detail.client.biz_no}` : ""}`
                    : "正在加载…"}
                </p>
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
                  <div className="kv" style={{ marginBottom: 16 }}>
                    <span className="k">状态</span>
                    <span className="v">
                      <span className={`badge ${badgeClass(detail.status)}`}>
                        {detail.status_label}
                      </span>{" "}
                      <span className={`badge ${priorityBadge(detail.priority)}`}>
                        {detail.priority_label}
                      </span>
                    </span>
                    <span className="k">提交时间</span>
                    <span className="v mono">{fmtTime(detail.created_at)}</span>
                    <span className="k">最近更新</span>
                    <span className="v mono">{fmtTime(detail.updated_at)}</span>
                    <span className="k">处理人</span>
                    <span className="v">
                      {detail.handled_by || "—"}
                      {detail.handled_at && (
                        <span className="faint mono" style={{ marginLeft: 8 }}>
                          {fmtTime(detail.handled_at)}
                        </span>
                      )}
                    </span>
                    <span className="k">联系人</span>
                    <span className="v">
                      {detail.client?.contact_name || "—"}
                      {detail.client?.phone && (
                        <span className="mono faint" style={{ marginLeft: 8 }}>
                          {detail.client.phone}
                        </span>
                      )}
                    </span>
                    <span className="k">邮箱</span>
                    <span className="v mono">{detail.client?.email || "—"}</span>
                    <span className="k">销售 / AE</span>
                    <span className="v">
                      {detail.client?.sales_owner || "—"} / {detail.client?.ae_owner || "—"}
                    </span>
                  </div>

                  <div className="sub-title" style={{ marginTop: 0 }}>
                    问题描述
                  </div>
                  <div className="ticket-text">
                    {detail.content?.trim() || "（客户未填写详细说明）"}
                  </div>

                  {detail.reply?.trim() && (
                    <>
                      <div className="sub-title">处理记录（客户可见）</div>
                      <div className="ticket-text">{detail.reply}</div>
                    </>
                  )}

                  {canHandle ? (
                    <>
                      <div className="sub-title">处理</div>
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
                          <label>优先级</label>
                          <select
                            value={form.priority}
                            onChange={(e) => setForm({ ...form, priority: e.target.value })}
                          >
                            {priorityOptions.map((o) => (
                              <option key={o.code} value={o.code}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="full">
                          <label>回复内容（客户可见）</label>
                          <textarea
                            rows={4}
                            value={form.reply}
                            onChange={(e) => setForm({ ...form, reply: e.target.value })}
                            placeholder="写清处理结果或需要客户补充的信息；多次回复会按时间追加"
                          />
                        </div>
                        <div className="full faint" style={{ fontSize: 11 }}>
                          结单（已解决 / 已关闭）前必须至少有一条回复，客户会在门户看到处理结果。
                        </div>
                      </div>
                    </>
                  ) : (
                    <div className="hint">当前角色只能查看工单；受理与回复需要「处理问题工单」权限。</div>
                  )}
                </>
              )}
            </div>

            <div className="drawer-foot">
              <button className="btn" onClick={closeDetail} disabled={busy}>
                {canHandle && detail ? "取消" : "关闭"}
              </button>
              {canHandle && detail && (
                <button className="btn primary" onClick={save} disabled={busy || detailLoading}>
                  {busy ? "保存中…" : "保存处理"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
