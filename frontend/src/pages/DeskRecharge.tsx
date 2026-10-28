import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, hasPerm, money, qs, type Session } from "../api";
import { badgeClass, fmtTime } from "../utils";

interface Item {
  id: number;
  biz_no: string;
  client_id: number;
  client_name: string;
  ad_account_id: number;
  account_id: string;
  platform: string;
  amount: string;
  currency: string;
  status: string;
  status_label: string;
  approval_no: string | null;
  serial_no: string | null;
  initiated_by: string;
  retry_count: number;
  receipt: string | null;
  created_at: string;
  elapsed_ms: number | null;
}

interface ListResp {
  total: number;
  items: Item[];
  summary: Record<string, number>;
}

interface ClientOption {
  id: number;
  company_name: string;
  biz_no: string | null;
}

interface Account {
  id: number;
  account_id: string;
  platform: string;
  currency: string;
  balance: string;
  daily_limit: string;
  status: string;
  channel: string | null;
}

interface CircuitItem {
  platform: string;
  enabled: boolean;
  persisted?: boolean;
}

interface ExecResp {
  id: number;
  status: string;
  status_label: string;
  serial_no?: string | null;
  balance?: string;
  account_balance?: string;
}

/** 发起加款的响应：单条 item + 幂等命中标记 */
type CreateResp = Item & { deduped?: boolean };

type Outcome = "SUCCESS" | "UNKNOWN" | "REJECTED";

type Modal =
  | { kind: "exec"; item: Item }
  | { kind: "resolve"; item: Item }
  | { kind: "cancel"; item: Item }
  | null;

/** 幂等键：现代浏览器用 randomUUID，否则退回时间戳 + 随机串（契约要求 ≥6 字符） */
function newRequestNo(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") return `rc-${c.randomUUID()}`;
  return `rc-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function DeskRecharge({ session }: { session: Session }) {
  const [status, setStatus] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  // 中文只取接口返回的 status_label
  const [statusLabels, setStatusLabels] = useState<Record<string, string>>({});

  const [circuit, setCircuit] = useState<CircuitItem[]>([]);
  const [circuitPersisted, setCircuitPersisted] = useState<boolean | null>(null);

  const [modal, setModal] = useState<Modal>(null);
  const [outcome, setOutcome] = useState<Outcome>("SUCCESS");
  const [resolveResult, setResolveResult] = useState<"SUCCESS" | "FAILED">("SUCCESS");
  const [resolveReason, setResolveReason] = useState("");
  const [cancelReason, setCancelReason] = useState("");

  // 发起加款表单（受控）
  const [clients, setClients] = useState<ClientOption[]>([]);
  const [fClient, setFClient] = useState("");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [fAccount, setFAccount] = useState("");
  const [fAmount, setFAmount] = useState("");
  const [fRemark, setFRemark] = useState("");
  // 幂等键：跟随本次提交；成功后或失败后都换新，避免改动金额重试被当成同一笔
  const [requestNo, setRequestNo] = useState(newRequestNo);

  // 执行是真的出账，权限必须与"看列表"分开（后端已拆出 recharge.exec）
  const canExec = hasPerm(session, "recharge.exec");
  const canResolve = hasPerm(session, "recharge.resolve");
  const canCancel = canResolve;

  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "desk",
        "/api/desk/recharges" + qs({ status: status || undefined, limit: 200 })
      );
      if (stale()) return;
      setItems(d.items);
      setSummary(d.summary ?? {});
      setTotal(d.total);
      setStatusLabels((prev) => {
        const next = { ...prev };
        for (const r of d.items) if (r.status && r.status_label) next[r.status] = r.status_label;
        return next;
      });
    } catch (e) {
      if (stale()) return;
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [status]);

  const loadCircuit = useCallback(async () => {
    try {
      const d = await api.get<{ items: CircuitItem[]; persisted?: boolean }>("desk", "/api/desk/recharges/circuit");
      setCircuit(d.items ?? []);
      setCircuitPersisted(typeof d.persisted === "boolean" ? d.persisted : null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "熔断状态加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadCircuit();
    api
      .get<{ items: ClientOption[] }>("desk", "/api/desk/clients?limit=200")
      .then((d) => setClients(d.items))
      .catch(() => {});
  }, [loadCircuit]);

  // 客户切换 → 拉该客户的广告账户，供加款表单下拉
  useEffect(() => {
    if (!fClient) {
      setAccounts([]);
      setFAccount("");
      return;
    }
    let alive = true;
    api
      .get<{ accounts: Account[] }>("desk", `/api/desk/clients/${fClient}`)
      .then((d) => {
        if (!alive) return;
        setAccounts(d.accounts ?? []);
        setFAccount(d.accounts?.length ? String(d.accounts[0].id) : "");
      })
      .catch(() => {
        if (alive) {
          setAccounts([]);
          setFAccount("");
        }
      });
    return () => {
      alive = false;
    };
  }, [fClient]);

  // 状态筛选来自后端枚举（F-15）
  const [enumStatus, setEnumStatus] = useState<[string, string][]>([]);
  useEffect(() => {
    api
      .get<{ recharge_status: { code: string; label: string }[] }>("desk", "/api/desk/enums")
      .then((d) => setEnumStatus(d.recharge_status.map((x) => [x.code, x.label])))
      .catch(() => {});
  }, []);

  const statusOptions = useMemo(
    () => (enumStatus.length ? enumStatus : Object.entries(statusLabels)),
    [enumStatus, statusLabels]
  );

  function openModal(m: Modal) {
    setModal(m);
    setErr("");
    setMsg("");
    setOutcome("SUCCESS");
    setResolveResult("SUCCESS");
    setResolveReason("");
    setCancelReason("");
  }

  async function createRecharge() {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const r = await api.post<CreateResp>("desk", "/api/desk/recharges", {
        ad_account_id: Number(fAccount),
        amount: fAmount,
        remark: fRemark || null,
        request_no: requestNo,
      });
      setFAmount("");
      setFRemark("");
      setMsg(
        r.deduped
          ? "该请求已提交过，已返回原单（未重复冻结）"
          : "已发起加款，金额已冻结占位，等待执行"
      );
      setRequestNo(newRequestNo());
      await load();
    } catch (e) {
      // 失败后换新幂等键：否则改了金额重试会被后端当成同一笔去重
      setRequestNo(newRequestNo());
      setErr(e instanceof Error ? e.message : "发起失败");
    } finally {
      setBusy(false);
    }
  }

  async function doCancel(item: Item) {
    if (!cancelReason.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await api.post<Item>("desk", `/api/desk/recharges/${item.id}/cancel`, { reason: cancelReason.trim() });
      setModal(null);
      setMsg("已撤销，冻结额已释放");
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "撤销失败");
    } finally {
      setBusy(false);
    }
  }

  async function doExec(item: Item) {
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<ExecResp>("desk", `/api/desk/recharges/${item.id}/exec`, { outcome });
      setModal(null);
      setMsg(
        `执行完成：${r.status_label}${r.serial_no ? ` · 流水号 ${r.serial_no}` : ""}${
          r.account_balance ? ` · 账户余额 ${money(r.account_balance)}` : ""
        }`
      );
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "执行失败");
    } finally {
      setBusy(false);
    }
  }

  async function doResolve(item: Item) {
    if (!resolveReason.trim()) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<ExecResp>("desk", `/api/desk/recharges/${item.id}/resolve`, {
        result: resolveResult,
        reason: resolveReason,
      });
      setModal(null);
      setMsg(`核查完成：${r.status_label}`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "核查失败");
    } finally {
      setBusy(false);
    }
  }

  async function doRetry(item: Item) {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const r = await api.post<Item>("desk", `/api/desk/recharges/${item.id}/retry`);
      setMsg(`已重试：${r.status_label}（重试次数 ${r.retry_count ?? 0}）`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "重试失败");
    } finally {
      setBusy(false);
    }
  }

  async function toggleCircuit(c: CircuitItem) {
    setBusy(true);
    setErr("");
    try {
      await api.post<CircuitItem>("desk", "/api/desk/recharges/circuit", {
        platform: c.platform,
        enabled: !c.enabled,
      });
      await loadCircuit();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "熔断开关更新失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="kpis" style={{ marginBottom: 14 }}>
        <div className="kpi">
          <div className="k">待核查</div>
          <div className="v">{summary.TO_VERIFY ?? 0}</div>
          <div className="s">媒体结果未知，人工核查前不动账</div>
        </div>
        <div className="kpi">
          <div className="k">待执行</div>
          <div className="v">{summary.PENDING_EXEC ?? 0}</div>
          <div className="s">已冻结钱包金额</div>
        </div>
        <div className="kpi">
          <div className="k">合计单数</div>
          <div className="v">{total}</div>
          <div className="s">受当前筛选影响</div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>充值记录</h2>
          <div className="toolbar">
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">全部状态</option>
              {statusOptions.map(([code, label]) => (
                <option key={code} value={code}>
                  {label}
                </option>
              ))}
            </select>
            <button className="btn sm" onClick={load} disabled={loading}>
              刷新
            </button>
          </div>
        </div>

        <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="toolbar">
            <span className="faint">平台熔断</span>
            {circuit.length === 0 && <span className="faint">加载中…</span>}
            {circuit.map((c) => (
              <label key={c.platform} className="row" style={{ gap: 6, marginRight: 8 }}>
                <input
                  type="checkbox"
                  checked={c.enabled}
                  disabled={busy || !canResolve}
                  onChange={() => toggleCircuit(c)}
                />
                <span>
                  {c.platform} {c.enabled ? <span className="out">已熔断</span> : <span className="in">正常</span>}
                </span>
              </label>
            ))}
            {circuitPersisted === false && <span className="faint">（内存实现，重启丢失）</span>}
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

        <table>
          <thead>
            <tr>
              <th>单号</th>
              <th>客户</th>
              <th>账户</th>
              <th className="num">金额</th>
              <th>状态</th>
              <th>发起人</th>
              <th>流水号</th>
              <th className="num">重试</th>
              <th>时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className={r.status === "TO_VERIFY" ? "row-warn" : ""}>
                <td className="mono faint">{r.biz_no}</td>
                <td>{r.client_name}</td>
                <td>
                  <div className="mono">{r.account_id}</div>
                  <div className="faint" style={{ fontSize: 11 }}>
                    {r.platform}
                  </div>
                </td>
                <td className="num mono out">
                  {money(r.amount)} <span className="faint">{r.currency}</span>
                </td>
                <td>
                  <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                  {r.status === "TO_VERIFY" && (
                    <div className="warnbox" style={{ margin: "4px 0 0", padding: "2px 6px", fontSize: 11 }}>
                      需人工核查
                    </div>
                  )}
                  {r.elapsed_ms !== null && (
                    <div className="faint" style={{ fontSize: 11 }}>
                      {(r.elapsed_ms / 1000).toFixed(1)}s
                    </div>
                  )}
                </td>
                <td className="muted">{r.initiated_by}</td>
                <td className="mono faint">{r.serial_no || "—"}</td>
                <td className="num mono muted">{r.retry_count}</td>
                <td className="mono faint">{fmtTime(r.created_at)}</td>
                <td className="right">
                  <div className="row" style={{ justifyContent: "flex-end" }}>
                    {r.status === "PENDING_EXEC" && canExec && (
                      <button className="btn sm primary" onClick={() => openModal({ kind: "exec", item: r })} disabled={busy}>
                        执行
                      </button>
                    )}
                    {r.status === "TO_VERIFY" && (
                      <>
                        <button
                          className="btn sm primary"
                          onClick={() => openModal({ kind: "resolve", item: r })}
                          disabled={busy || !canResolve}
                        >
                          核查
                        </button>
                        <button className="btn sm" onClick={() => doRetry(r)} disabled={busy || !canResolve}>
                          重试
                        </button>
                      </>
                    )}
                    {r.status === "REJECTED" && (
                      <button className="btn sm" onClick={() => doRetry(r)} disabled={busy || !canResolve}>
                        重试
                      </button>
                    )}
                    {(r.status === "PENDING_EXEC" || r.status === "TO_VERIFY") && canCancel && (
                      <button
                        className="btn sm danger"
                        onClick={() => openModal({ kind: "cancel", item: r })}
                        disabled={busy}
                        title="撤销后释放冻结额，单据状态改为已撤销"
                      >
                        撤销
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && !loading && (
              <tr>
                <td colSpan={10}>
                  <div className="empty">没有符合条件的充值记录</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>发起充值</h2>
          <span className="faint">发起即冻结客户钱包可用余额，执行成功才真正出账</span>
        </div>
        <div className="card-body">
          <div className="toolbar">
            <select value={fClient} onChange={(e) => setFClient(e.target.value)} style={{ minWidth: 200 }}>
              <option value="">选择客户</option>
              {clients.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.company_name}
                </option>
              ))}
            </select>
            <select value={fAccount} onChange={(e) => setFAccount(e.target.value)} style={{ minWidth: 220 }}>
              <option value="">{fClient ? (accounts.length ? "选择广告账户" : "该客户暂无广告账户") : "先选客户"}</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.account_id} · {a.platform} · 余额 {money(a.balance)} {a.currency}
                </option>
              ))}
            </select>
            <input
              value={fAmount}
              onChange={(e) => setFAmount(e.target.value)}
              placeholder="充值金额"
              inputMode="decimal"
              style={{ minWidth: 120 }}
            />
            <input
              value={fRemark}
              onChange={(e) => setFRemark(e.target.value)}
              placeholder="备注（可选）"
              style={{ minWidth: 160 }}
            />
            <button
              className="btn primary"
              onClick={createRecharge}
              disabled={busy || !fAccount || !fAmount.trim()}
            >
              发起充值
            </button>
          </div>
          <div className="faint" style={{ marginTop: 8 }}>
            客户钱包可用余额不足时接口会返回 INSUFFICIENT_BALANCE，错误原文会显示在上方。
          </div>
          <div className="faint mono" style={{ marginTop: 4, fontSize: 11 }}>
            本次幂等键 request_no：{requestNo}（提交成功或失败后自动换新）
          </div>
        </div>
      </div>

      {modal?.kind === "exec" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
          <h2>执行充值 · {modal.item.biz_no}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="kv" style={{ marginBottom: 12 }}>
                <span className="k">账户</span>
                <span className="v mono">
                  {modal.item.account_id}（{modal.item.platform}）
                </span>
                <span className="k">金额</span>
                <span className="v mono">
                  {money(modal.item.amount)} {modal.item.currency}
                </span>
              </div>
              <div className="field">
                <label>执行结果（Mock RPA 三态）</label>
                <select value={outcome} onChange={(e) => setOutcome(e.target.value as Outcome)}>
                  <option value="SUCCESS">SUCCESS · 成功（解冻 + 出账 + 广告账户加钱）</option>
                  <option value="UNKNOWN">UNKNOWN · 结果未知（状态转为待核查，不动账）</option>
                  <option value="REJECTED">REJECTED · 被拒绝（解冻，不加钱）</option>
                </select>
              </div>
              {err && <div className="err">{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button className="btn primary" onClick={() => doExec(modal.item)} disabled={busy}>
                确认执行
              </button>
            </div>
          </div>
        </div>
      )}

      {modal?.kind === "resolve" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>核查未知结果 · {modal.item.biz_no}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                核查前该笔不动账：冻结不释放。核查为成功后走成功路径（幂等键防重复），失败则解冻并置为拒绝。
              </div>
              <div className="field">
                <label>核查结论</label>
                <select value={resolveResult} onChange={(e) => setResolveResult(e.target.value as "SUCCESS" | "FAILED")}>
                  <option value="SUCCESS">SUCCESS · 媒体后台已到账</option>
                  <option value="FAILED">FAILED · 媒体后台未到账</option>
                </select>
              </div>
              <div className="field">
                <label>核查说明（必填）</label>
                <textarea
                  rows={3}
                  value={resolveReason}
                  onChange={(e) => setResolveReason(e.target.value)}
                  placeholder="媒体后台已到账"
                />
              </div>
              {err && <div className="err">{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => doResolve(modal.item)}
                disabled={busy || !resolveReason.trim()}
              >
                提交核查
              </button>
            </div>
          </div>
        </div>
      )}

      {modal?.kind === "cancel" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>撤销充值单 · {modal.item.biz_no}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="warnbox">
                撤销会释放该笔冻结额（{money(modal.item.amount)} {modal.item.currency}），单据状态改为「已撤销」，不可恢复。
              </div>
              <div className="field">
                <label>撤销原因（必填）</label>
                <textarea
                  rows={3}
                  value={cancelReason}
                  onChange={(e) => setCancelReason(e.target.value)}
                  placeholder="客户取消加款"
                />
              </div>
              {err && <div className="err">{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn danger"
                onClick={() => doCancel(modal.item)}
                disabled={busy || !cancelReason.trim()}
              >
                确认撤销
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
