import { useCallback, useEffect, useRef, useState } from "react";
import { api, qs, type Session } from "../api";

interface BcidRow {
  id: number;
  bcid: string;
  source: "MANUAL" | "HISTORY";
  source_label: string;
  source_detail: string | null;
  source_detail_label: string | null;
  is_default: boolean;
  created_by: string | null;
  created_at: string | null;
}

interface Summary {
  manual: number;
  history: number;
  default_bcid: string | null;
}

const PAGE_SIZES = [10, 20, 50];

/** 分页按钮：首尾页 + 当前页左右各一页，其余用 … 收掉。 */
function pageList(current: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
  const out: (number | "…")[] = [];
  const push = (v: number | "…") => {
    if (out[out.length - 1] !== v) out.push(v);
  };
  push(1);
  if (current > 3) push("…");
  for (let p = Math.max(2, current - 1); p <= Math.min(totalPages - 1, current + 1); p += 1) {
    push(p);
  }
  if (current < totalPages - 2) push("…");
  push(totalPages);
  return out;
}

/**
 * 客户门户 · BC 管理。
 *
 * 客户只看/管自己的 BCID（client_id 由后端从登录态反查，前端不传）：
 * - 「历史绑定」由广告账户 / 开户单自动归纳，读取列表时幂等补齐；
 * - 「手工添加」可自行录入，删除后不会随刷新自动回来；
 * - 勾选「开户后默认绑定此 BCID」为单选，提交开户申请时自动预选。
 */
export default function PortalBc(_props: { session: Session }) {
  const [items, setItems] = useState<BcidRow[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<Summary>({ manual: 0, history: 0, default_bcid: null });
  const [keyword, setKeyword] = useState("");
  const [appliedKeyword, setAppliedKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [jump, setJump] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const [addOpen, setAddOpen] = useState(false);
  const [addValue, setAddValue] = useState("");
  const [addDefault, setAddDefault] = useState(false);
  const [addErr, setAddErr] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<BcidRow | null>(null);
  const [busy, setBusy] = useState(false);

  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<{ items: BcidRow[]; total: number; summary: Summary }>(
        "portal",
        "/api/portal/bcids" +
          qs({
            keyword: appliedKeyword,
            limit: pageSize,
            offset: (page - 1) * pageSize,
          })
      );
      if (seq !== seqRef.current) return; // 连续翻页 / 搜索时丢弃过期响应
      setItems(d.items ?? []);
      setTotal(d.total ?? 0);
      setSummary(d.summary ?? { manual: 0, history: 0, default_bcid: null });
    } catch (e: any) {
      if (seq === seqRef.current) setErr(e?.message || "加载失败");
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [appliedKeyword, page, pageSize]);

  useEffect(() => {
    load();
  }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  function refresh() {
    setMsg("");
    setErr("");
    load();
  }

  function search() {
    setPage(1);
    setAppliedKeyword(keyword);
  }

  async function toggleDefault(row: BcidRow) {
    if (busy) return;
    setBusy(true);
    setMsg("");
    setErr("");
    try {
      await api.post("portal", `/api/portal/bcids/${row.id}/default`, {
        is_default: !row.is_default,
      });
      await load();
      setMsg(row.is_default ? "已取消默认绑定" : `已将 ${row.bcid} 设为开户默认绑定`);
    } catch (e: any) {
      setErr(e?.message || "设置默认绑定失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitAdd() {
    const value = addValue.trim();
    if (!value) {
      setAddErr("请输入 BCID");
      return;
    }
    setBusy(true);
    setAddErr("");
    try {
      const r = await api.post<{
        created: boolean;
        revived?: boolean;
        item: BcidRow;
      }>("portal", "/api/portal/bcids", { bcid: value, set_default: addDefault });
      setAddOpen(false);
      setAddValue("");
      setAddDefault(false);
      setPage(1);
      setKeyword("");
      setAppliedKeyword("");
      await load();
      setMsg(
        r.created
          ? `已添加 ${r.item.bcid}`
          : r.revived
            ? `已恢复 ${r.item.bcid}，并标记为手工添加`
            : `${r.item.bcid} 已存在，已标记为手工添加`
      );
    } catch (e: any) {
      setAddErr(e?.message || "添加失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitDelete() {
    if (!deleteTarget) return;
    setBusy(true);
    setErr("");
    try {
      await api.del("portal", `/api/portal/bcids/${deleteTarget.id}`);
      const removed = deleteTarget.bcid;
      setDeleteTarget(null);
      await load();
      setMsg(`已删除 ${removed}`);
    } catch (e: any) {
      setErr(e?.message || "删除失败");
    } finally {
      setBusy(false);
    }
  }

  function goPage(p: number) {
    const next = Math.min(Math.max(1, p), totalPages);
    setPage(next);
    setJump(String(next));
  }

  return (
    <>
      <div className="card">
        <div className="card-body">
          <div className="bc-head-row">
            <div>
              <div className="bc-scope-hint">
                管理你绑定过或手工添加的 BCID，并设置开户后默认绑定项。
              </div>
              <div className="bc-scope-hint faint" style={{ marginTop: 4 }}>
                默认项会在提交开户申请时自动预选；历史绑定来自广告账户与开户记录。
              </div>
            </div>
            <button className="btn sm" onClick={refresh} disabled={loading}>
              刷新
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>
            BCID 列表
            <span className="faint bc-count">
              共 {total} 条 · 手工 {summary.manual} · 历史 {summary.history}
            </span>
          </h2>
          <div className="toolbar">
            <input
              placeholder="搜索 BCID"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
              style={{ minWidth: 170 }}
              aria-label="搜索 BCID"
            />
            <button className="btn sm" onClick={search} disabled={loading}>
              查询
            </button>
            <button
              className="btn sm"
              onClick={() => {
                setAddErr("");
                setAddValue("");
                setAddDefault(!summary.default_bcid);
                setAddOpen(true);
              }}
            >
              ＋ 添加 BCID
            </button>
          </div>
        </div>

        {msg && (
          <div className="card-body" style={{ paddingBottom: 0 }}>
            <div className="warnbox">{msg}</div>
          </div>
        )}
        {err && (
          <div className="card-body" style={{ paddingBottom: 0 }}>
            <div className="err">{err}</div>
          </div>
        )}

        <table className="bc-table">
          <thead>
            <tr>
              <th style={{ width: 360 }}>BCID</th>
              <th style={{ width: 120 }}>来源</th>
              <th className="right">默认绑定</th>
              <th className="right" style={{ width: 90 }}>
                操作
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td className="mono bc-bcid">{row.bcid}</td>
                <td>
                  <span className={`badge ${row.source === "MANUAL" ? "blue" : "gray"}`}>
                    {row.source_label}
                  </span>
                </td>
                <td className="right">
                  <label className="bc-default">
                    <input
                      type="checkbox"
                      checked={row.is_default}
                      disabled={busy}
                      onChange={() => toggleDefault(row)}
                    />
                    开户后默认绑定此 BCID
                  </label>
                </td>
                <td className="right">
                  <button className="bc-delete" onClick={() => setDeleteTarget(row)}>
                    删除
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={4}>
                  <div className="empty">
                    {loading
                      ? "加载中…"
                      : appliedKeyword
                        ? "没有匹配的 BCID"
                        : "还没有 BCID 绑定，点「添加 BCID」补一条"}
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="client-pager">
          <span className="faint">
            共 {total} 条 · 第 {Math.min(page, totalPages)} / {totalPages} 页
            {summary.default_bcid ? ` · 默认 ${summary.default_bcid}` : " · 未设默认"}
          </span>
          <div className="pager-btns">
            <select
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
              aria-label="每页条数"
            >
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {n}条/页
                </option>
              ))}
            </select>
            <button className="btn sm" onClick={() => goPage(page - 1)} disabled={page <= 1 || loading}>
              上一页
            </button>
            {pageList(page, totalPages).map((p, idx) =>
              p === "…" ? (
                <span key={`e${idx}`} className="faint">
                  …
                </span>
              ) : (
                <button
                  key={p}
                  className={`btn sm ${p === page ? "primary" : ""}`}
                  onClick={() => goPage(p)}
                  disabled={loading}
                >
                  {p}
                </button>
              )
            )}
            <button
              className="btn sm"
              onClick={() => goPage(page + 1)}
              disabled={page >= totalPages || loading}
            >
              下一页
            </button>
            <span className="faint">前往</span>
            <input
              className="pager-jump"
              value={jump}
              onChange={(e) => setJump(e.target.value.replace(/[^\d]/g, ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") goPage(Number(jump) || 1);
              }}
              onBlur={() => goPage(Number(jump) || page)}
              aria-label="跳转页码"
            />
            <span className="faint">页</span>
          </div>
        </div>
      </div>

      {addOpen && (
        <div className="modal-mask" onClick={() => !busy && setAddOpen(false)}>
          <div className="modal" style={{ width: 460 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>添加 BCID</h2>
              <button className="btn ghost sm" onClick={() => setAddOpen(false)} disabled={busy}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="field">
                <label>BCID *</label>
                <input
                  autoFocus
                  className="mono"
                  value={addValue}
                  onChange={(e) => setAddValue(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && submitAdd()}
                  placeholder="例如 765557932675017934"
                />
              </div>
              <label className="bc-modal-default">
                <input
                  type="checkbox"
                  checked={addDefault}
                  onChange={(e) => setAddDefault(e.target.checked)}
                />
                同时设为开户后默认绑定
              </label>
              <div className="field-hint">
                同一个 BCID 已存在时不会重复添加，只把它标记为「手工添加」。
              </div>
              {addErr && (
                <div className="err" style={{ marginTop: 8 }}>
                  {addErr}
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setAddOpen(false)} disabled={busy}>
                取消
              </button>
              <button className="btn primary" onClick={submitAdd} disabled={busy || !addValue.trim()}>
                添加
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-mask" onClick={() => !busy && setDeleteTarget(null)}>
          <div className="modal" style={{ width: 440 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>删除 BCID 绑定</h2>
              <button className="btn ghost sm" onClick={() => setDeleteTarget(null)} disabled={busy}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                将删除与 <b className="mono">{deleteTarget.bcid}</b> 的绑定关系。
                广告账户与已提交的开户单不受影响；删除后不会随刷新自动补回，
                需要时用「添加 BCID」重新加入。
              </div>
              {deleteTarget.source === "HISTORY" && (
                <div className="field-hint" style={{ marginTop: 8 }}>
                  这是一条历史绑定，删除后会保持隐藏（不会改变广告账户和开户记录）。
                </div>
              )}
              {deleteTarget.is_default && (
                <div className="field-hint" style={{ marginTop: 8 }}>
                  这条是当前的开户默认绑定，删除后不会自动改选其它 BCID。
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setDeleteTarget(null)} disabled={busy}>
                取消
              </button>
              <button className="btn danger" onClick={submitDelete} disabled={busy}>
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
