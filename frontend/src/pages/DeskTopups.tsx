import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, download, hasPerm, money, qs, upload, type Session } from "../api";
import { AuthedThumb, isPdf, useFileUrl } from "../components/AuthedFile";
import { badgeClass, fmtTime } from "../utils";

interface Item {
  id: number;
  biz_no: string;
  client_id: number;
  client_name: string | null;
  client_no: string | null;
  fund_account_type: string;
  fund_account_type_label: string;
  op_type: string;
  op_type_label: string;
  txn_no: string | null;
  amount: string;
  currency: string;
  payable_amount: string;
  payable_currency: string;
  received_amount: string;
  exchange_rate: string;
  rate_source: string | null;
  status: string;
  status_label: string;
  pending: boolean;
  voucher: string | null;
  voucher_url: string | null;
  has_voucher: boolean;
  voucher_missing: boolean;
  payer: string | null;
  remark: string | null;
  reject_reason: string | null;
  review_note: string | null;
  applied_by: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
}

interface AmountRow {
  currency: string;
  amount: string;
  count: number;
}

interface Summary {
  counts: Record<string, number>;
  pending_count: number;
  pending_amount: AmountRow[];
  approved_amount: AmountRow[];
}

interface ListResp {
  total: number;
  items: Item[];
  summary: Summary;
}

interface ClientOption {
  id: number;
  company_name: string;
  biz_no: string | null;
  settle_currency: string;
  status: string;
  credit_limit: string;
  credit_available: string;
}

interface EnumOpt {
  code: string;
  label: string;
}

interface LedgerRow {
  id: number;
  seq: number;
  direction: string;
  amount: string;
  currency: string;
  balance_after: string;
  biz_type: string;
  operator: string | null;
  remark: string | null;
  occurred_at: string;
}

interface Filters {
  account: string;
  op: string;
  bill: string;
  txn: string;
  client: string;
  applicant: string;
}

const EMPTY_FILTERS: Filters = { account: "", op: "", bill: "", txn: "", client: "", applicant: "" };

type Modal =
  | { kind: "create" }
  | { kind: "review"; item: Item }
  | { kind: "reject"; item: Item }
  | { kind: "view"; item: Item }
  | null;

interface Preview {
  url: string;
  name: string;
}

const ACCEPT = ".jpg,.jpeg,.png,.webp,.pdf";

/** 币种中文名：填单页写「美金」比写 USD 直白，代码仍按 ISO 三字母走。 */
const CCY_NAME: Record<string, string> = {
  USD: "美金",
  EUR: "欧元",
  JPY: "日元",
  HKD: "港币",
  GBP: "英镑",
  AUD: "澳元",
  CAD: "加元",
  SGD: "新加坡元",
  CHF: "瑞士法郎",
  NZD: "新西兰元",
  THB: "泰铢",
  KRW: "韩元",
  RUB: "卢布",
  AED: "迪拉姆",
  SAR: "里亚尔",
  TRY: "土耳其里拉",
  MXN: "墨西哥比索",
  PLN: "兹罗提",
  DKK: "丹麦克朗",
  SEK: "瑞典克朗",
  NOK: "挪威克朗",
  HUF: "福林",
  MYR: "林吉特",
  ZAR: "兰特",
  MOP: "澳门元",
};

function ccyName(code: string): string {
  return CCY_NAME[(code || "").toUpperCase()] ?? code;
}

/** 金额栏写法：美金 USD */
function ccyLabel(code: string): string {
  return `${ccyName(code)} ${(code || "").toUpperCase()}`;
}

/** 资金流向决定颜色：钱进客户（加款）绿、钱出客户（减款）红。 */
function opBadgeClass(code: string): string {
  return code === "DECREASE" ? "red" : "green";
}

function accountBadgeClass(code: string): string {
  return code === "CREDIT" ? "amber" : "blue";
}

function amountText(rows: AmountRow[]): string {
  if (!rows.length) return "—";
  return rows.map((r) => `${money(r.amount)} ${r.currency}`).join(" / ");
}

/** 列表凭证格：缩略图可点开大图；voucher_missing 标红。 */
function VoucherCell({ row, onPreview }: { row: Item; onPreview: (url: string, name: string) => void }) {
  if (row.voucher_missing) {
    return (
      <div className="thumb-cell">
        <span className="thumb-missing">凭证丢失</span>
        <span className="faint" style={{ fontSize: 11 }}>
          文件已不在磁盘
        </span>
      </div>
    );
  }
  if (!row.voucher_url) {
    return row.has_voucher ? (
      <span className="faint" title={row.voucher ?? ""}>
        历史路径不可预览
      </span>
    ) : (
      <span className="faint">—</span>
    );
  }
  return (
    <AuthedThumb
      kind="desk"
      path={row.voucher_url}
      alt="凭证"
      className="thumb"
      onClick={() => onPreview(row.voucher_url as string, row.biz_no)}
    />
  );
}

/** 发起申请时刚上传的凭证：同样要带 Bearer 才能读，统一走 blob。 */
function PathThumb({ path, onClick }: { path: string; onClick: () => void }) {
  const { url, failed } = useFileUrl("desk", path);
  if (failed) return <span className="thumb-missing">无法读取</span>;
  if (!url) return <span className="faint">加载中…</span>;
  return isPdf(path) ? (
    <div className="thumb pdf" onClick={onClick}>
      PDF
    </div>
  ) : (
    <img className="thumb" src={url} alt="凭证" onClick={onClick} />
  );
}

export default function DeskTopups({ session }: { session: Session }) {
  const [tab, setTab] = useState<"all" | "pending">("all");
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);

  const [items, setItems] = useState<Item[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const [modal, setModal] = useState<Modal>(null);
  const [preview, setPreview] = useState<Preview | null>(null);

  // 申请单操作（审核通过 / 驳回）
  const [reviewNote, setReviewNote] = useState("");
  const [reviewReceived, setReviewReceived] = useState("");
  const [noVoucherReason, setNoVoucherReason] = useState("");
  const [rejectReason, setRejectReason] = useState("");

  // 发起申请表单
  const [clients, setClients] = useState<ClientOption[]>([]);
  const [fClient, setFClient] = useState("");
  const [fAccount, setFAccount] = useState("CASH");
  const [fOp, setFOp] = useState("INCREASE");
  const [fAmount, setFAmount] = useState("");
  const [fReceived, setFReceived] = useState("");
  const [fTxn, setFTxn] = useState("");
  const [fPayer, setFPayer] = useState("");
  const [fVoucher, setFVoucher] = useState("");
  const [fVoucherName, setFVoucherName] = useState("");
  const [fUploading, setFUploading] = useState(false);
  const [fVoucherErr, setFVoucherErr] = useState("");
  const [fRemark, setFRemark] = useState("");

  // 详情（查看申请单）
  const [detail, setDetail] = useState<{ item: Item; ledger: LedgerRow[] } | null>(null);
  const [detailErr, setDetailErr] = useState("");

  const [enums, setEnums] = useState<{ account: EnumOpt[]; op: EnumOpt[] }>({ account: [], op: [] });

  // 权限一律来自后端下发的 perms（与 require_perm 同源）
  const canApply = hasPerm(session, "topup.apply");
  const canReview = hasPerm(session, "topup.confirm");

  const seqRef = useRef(0);

  useEffect(() => {
    api
      .get<{ fund_account_type: EnumOpt[]; fund_op_type: EnumOpt[] }>("desk", "/api/desk/enums")
      .then((d) => setEnums({ account: d.fund_account_type ?? [], op: d.fund_op_type ?? [] }))
      .catch(() => {});
    api
      .get<{ items: ClientOption[] }>("desk", "/api/desk/clients?limit=500")
      .then((d) => setClients(d.items))
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const stale = () => seq !== seqRef.current;
    setLoading(true);
    setErr("");
    try {
      const d = await api.get<ListResp>(
        "desk",
        "/api/desk/fund-applications" +
          qs({
            pending: tab === "pending" ? 1 : undefined,
            fund_account_type: filters.account || undefined,
            op_type: filters.op || undefined,
            bill_no: filters.bill || undefined,
            txn_no: filters.txn || undefined,
            client_keyword: filters.client || undefined,
            applicant: filters.applicant || undefined,
            limit: 200,
          })
      );
      if (stale()) return;
      setItems(d.items);
      setSummary(d.summary);
      setTotal(d.total);
    } catch (e) {
      if (stale()) return;
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [tab, filters]);

  useEffect(() => {
    load();
  }, [load]);

  const pendingCount = summary?.pending_count ?? 0;

  function query() {
    setFilters({ ...draft });
  }

  function resetQuery() {
    setDraft(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
  }

  function openModal(m: Modal) {
    setModal(m);
    setErr("");
    setMsg("");
    setReviewNote("");
    setNoVoucherReason("");
    setRejectReason("");
    if (m?.kind === "review") {
      setReviewReceived(m.item.received_amount);
    }
    if (m?.kind === "create") {
      setFVoucherErr("");
    }
  }

  function openPreview(url: string, name: string) {
    setPreview({ url, name });
  }

  async function openDetail(row: Item) {
    setDetail({ item: row, ledger: [] });
    setDetailErr("");
    try {
      const d = await api.get<{ item: Item; ledger: LedgerRow[] }>(
        "desk",
        `/api/desk/fund-applications/${row.id}`
      );
      setDetail(d);
    } catch (e) {
      setDetailErr(e instanceof Error ? e.message : "加载详情失败");
    }
  }

  // 只有已开通客户能做资金操作（后端也会拦，这里少让人白填一遍）
  const activeClients = useMemo(() => clients.filter((c) => c.status === "ACTIVE"), [clients]);
  const selectedClient = useMemo(
    () => activeClients.find((c) => String(c.id) === fClient) ?? null,
    [activeClients, fClient]
  );
  const clientCurrency = selectedClient?.settle_currency ?? "USD";

  const payablePreview = useMemo(() => {
    if (fOp === "DECREASE") return "0.00";
    const a = Number(fAmount || 0);
    return a > 0 ? a.toFixed(2) : "0.00";
  }, [fAmount, fOp]);

  async function onPickVoucher(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setFUploading(true);
    setFVoucherErr("");
    try {
      const r = await upload("desk", file, "TOPUP_VOUCHER");
      setFVoucher(r.url);
      setFVoucherName(r.original_name);
    } catch (e2) {
      setFVoucher("");
      setFVoucherName("");
      setFVoucherErr(e2 instanceof Error ? e2.message : "上传失败");
    } finally {
      setFUploading(false);
    }
  }

  async function submitCreate() {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const app = await api.post<Item>("desk", "/api/desk/fund-applications", {
        client_id: Number(fClient),
        fund_account_type: fAccount,
        op_type: fOp,
        amount: fAmount,
        received_amount: fReceived || fAmount,
        txn_no: fTxn.trim() || null,
        payer: fPayer.trim() || null,
        voucher: fVoucher || null,
        remark: fRemark.trim() || null,
      });
      setModal(null);
      setFAmount("");
      setFReceived("");
      setFTxn("");
      setFPayer("");
      setFVoucher("");
      setFVoucherName("");
      setFRemark("");
      setMsg(`已发起申请：${app.biz_no}（${app.status_label}），等待财务审核`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "发起失败");
    } finally {
      setBusy(false);
    }
  }

  async function doApprove(item: Item) {
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<Item>("desk", `/api/desk/fund-applications/${item.id}/approve`, {
        note: reviewNote.trim() || null,
        received_amount: reviewReceived || null,
        no_voucher_reason: noVoucherReason.trim() || null,
      });
      setModal(null);
      setMsg(`审核通过：${r.biz_no} · 到账 ${money(r.received_amount)} ${r.currency}`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "审核失败");
    } finally {
      setBusy(false);
    }
  }

  async function doReject(item: Item) {
    if (!rejectReason.trim()) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<Item>("desk", `/api/desk/fund-applications/${item.id}/reject`, {
        reason: rejectReason.trim(),
      });
      setModal(null);
      setMsg(`已驳回：${r.biz_no}`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "驳回失败");
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    download(
      "desk",
      "/api/desk/fund-applications/export" +
        qs({
          pending: tab === "pending" ? 1 : undefined,
          fund_account_type: filters.account || undefined,
          op_type: filters.op || undefined,
          bill_no: filters.bill || undefined,
          txn_no: filters.txn || undefined,
          client_keyword: filters.client || undefined,
          applicant: filters.applicant || undefined,
        }),
      "线下汇款申请单.csv"
    ).catch((e: unknown) => setErr(e instanceof Error ? e.message : "导出失败"));
  }

  const needNoVoucherReason =
    modal?.kind === "review" &&
    modal.item.op_type === "INCREASE" &&
    modal.item.fund_account_type === "CASH" &&
    !modal.item.has_voucher;

  return (
    <>
      <div className="subtabs">
        <button className={tab === "all" ? "on" : ""} onClick={() => setTab("all")} type="button">
          全部
        </button>
        <button className={tab === "pending" ? "on" : ""} onClick={() => setTab("pending")} type="button">
          待处理{" "}
          {pendingCount > 0 && (
            <span className="badge amber" style={{ marginLeft: 2 }}>
              {pendingCount}
            </span>
          )}
        </button>
      </div>

      <div className="card">
        <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="toolbar">
            <select
              value={draft.account}
              onChange={(e) => setDraft({ ...draft, account: e.target.value })}
              title="资金账户类型"
            >
              <option value="">资金账户类型：请选择</option>
              {enums.account.map((o) => (
                <option key={o.code} value={o.code}>
                  {o.label}
                </option>
              ))}
            </select>
            <select
              value={draft.op}
              onChange={(e) => setDraft({ ...draft, op: e.target.value })}
              title="操作类型"
            >
              <option value="">操作类型：请选择</option>
              {enums.op.map((o) => (
                <option key={o.code} value={o.code}>
                  {o.label}
                </option>
              ))}
            </select>
            <input
              placeholder="账单ID：请输入"
              value={draft.bill}
              onChange={(e) => setDraft({ ...draft, bill: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && query()}
              style={{ minWidth: 170 }}
            />
            <input
              placeholder="交易编号：请输入"
              value={draft.txn}
              onChange={(e) => setDraft({ ...draft, txn: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && query()}
              style={{ minWidth: 170 }}
            />
            <input
              placeholder="客户名称/编号：请输入"
              value={draft.client}
              onChange={(e) => setDraft({ ...draft, client: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && query()}
              style={{ minWidth: 180 }}
            />
            <input
              placeholder="申请人：请输入"
              value={draft.applicant}
              onChange={(e) => setDraft({ ...draft, applicant: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && query()}
              style={{ minWidth: 140 }}
            />
            <button className="btn primary" onClick={query} disabled={loading}>
              查询
            </button>
            <button className="btn" onClick={resetQuery} disabled={loading}>
              重置
            </button>
            {canApply && (
              <button className="btn primary" onClick={() => openModal({ kind: "create" })}>
                + 发起申请
              </button>
            )}
            <button className="btn" onClick={exportCsv}>
              导出
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

        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>资金账户类型</th>
              <th>操作类型</th>
              <th>账单ID</th>
              <th>交易编号</th>
              <th>客户名称</th>
              <th className="num">申请金额</th>
              <th className="num">应付金额</th>
              <th className="num">到账金额</th>
              <th>状态</th>
              <th>申请人</th>
              <th>审核人</th>
              <th>创建时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id}>
                <td className="mono faint">{r.id}</td>
                <td>
                  <span className={`badge ${accountBadgeClass(r.fund_account_type)}`}>
                    {r.fund_account_type_label}
                  </span>
                </td>
                <td>
                  <span className={`badge ${opBadgeClass(r.op_type)}`}>{r.op_type_label}</span>
                </td>
                <td className="mono faint">
                  <button type="button" className="link-btn" onClick={() => openDetail(r)} disabled={busy}>
                    {r.biz_no}
                  </button>
                </td>
                <td className="mono faint">{r.txn_no || "--"}</td>
                <td>
                  <div>{r.client_name || "—"}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>
                    {r.client_no || ""}
                  </div>
                </td>
                <td className="num mono" style={{ whiteSpace: "nowrap" }}>
                  {money(r.amount)} <span className="faint">{r.currency}</span>
                </td>
                <td className="num mono muted" style={{ whiteSpace: "nowrap" }}>
                  {money(r.payable_amount)}{" "}
                  <span className="faint">{r.payable_currency}</span>
                </td>
                <td className="num mono" style={{ whiteSpace: "nowrap" }}>
                  {money(r.received_amount)}
                </td>
                <td>
                  <span className={`badge ${badgeClass(r.status)}`}>{r.status_label}</span>
                  {r.reject_reason && (
                    <div className="faint" style={{ fontSize: 11, maxWidth: 160 }}>
                      {r.reject_reason}
                    </div>
                  )}
                </td>
                <td className="muted" style={{ whiteSpace: "nowrap" }}>
                  {r.applied_by || "—"}
                </td>
                <td className="muted" style={{ whiteSpace: "nowrap" }}>
                  {r.reviewed_by || "—"}
                </td>
                <td className="mono faint" style={{ whiteSpace: "nowrap" }}>
                  {fmtTime(r.created_at)}
                </td>
                <td className="right">
                  <div className="row" style={{ justifyContent: "flex-end" }}>
                    {r.pending && canReview ? (
                      <button className="btn sm primary" onClick={() => openModal({ kind: "review", item: r })} disabled={busy}>
                        处理申请单
                      </button>
                    ) : (
                      <button className="btn sm ghost" onClick={() => openDetail(r)} disabled={busy}>
                        查看申请单
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && !loading && (
              <tr>
                <td colSpan={14}>
                  <div className="empty">没有符合条件的申请单</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="card-body faint" style={{ borderTop: "1px solid var(--border)" }}>
          共 {total} 条 · 待处理 {pendingCount} 笔 · 待处理金额 {amountText(summary?.pending_amount ?? [])} ·
          审核通过金额 {amountText(summary?.approved_amount ?? [])}
          <span style={{ marginLeft: 8 }}>（金额受当前筛选影响）</span>
        </div>
      </div>

      {/* ---------------- 发起申请 ---------------- */}
      {modal?.kind === "create" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" style={{ width: "min(720px, 96vw)" }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>发起线下汇款申请</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="form-grid">
                <div className="full">
                  <label>客户</label>
                  <select value={fClient} onChange={(e) => setFClient(e.target.value)}>
                    <option value="">请选择客户</option>
                    {activeClients.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.company_name}
                        {c.biz_no ? `（${c.biz_no}）` : ""} · {c.settle_currency}
                      </option>
                    ))}
                  </select>
                  {selectedClient && fAccount === "CREDIT" && (
                    <div className="faint" style={{ marginTop: 4, fontSize: 11 }}>
                      授信额度 {money(selectedClient.credit_limit)} · 可用 {money(selectedClient.credit_available)}{" "}
                      {selectedClient.settle_currency}
                    </div>
                  )}
                </div>
                <div>
                  <label>资金账户类型</label>
                  <select value={fAccount} onChange={(e) => setFAccount(e.target.value)}>
                    {enums.account.map((o) => (
                      <option key={o.code} value={o.code}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>操作类型</label>
                  <select value={fOp} onChange={(e) => setFOp(e.target.value)}>
                    {enums.op.map((o) => (
                      <option key={o.code} value={o.code}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>申请金额（{ccyLabel(clientCurrency)}）</label>
                  <input
                    value={fAmount}
                    onChange={(e) => {
                      setFAmount(e.target.value);
                      setFReceived(e.target.value);
                    }}
                    placeholder="1000"
                    inputMode="decimal"
                  />
                </div>
                <div>
                  <label>
                    {fOp === "DECREASE"
                      ? "应付金额（减款不产生应付）"
                      : `应付金额（${ccyLabel(clientCurrency)}，与申请金额一致）`}
                  </label>
                  <input value={payablePreview} readOnly style={{ opacity: 0.75 }} />
                </div>
                <div>
                  <label>到账金额（{ccyLabel(clientCurrency)}）</label>
                  <input
                    value={fReceived}
                    onChange={(e) => setFReceived(e.target.value)}
                    placeholder="与申请金额一致时可留空"
                    inputMode="decimal"
                  />
                </div>
                <div>
                  <label>交易编号（银行流水号，可选）</label>
                  <input value={fTxn} onChange={(e) => setFTxn(e.target.value)} placeholder="BANK-20261023-001" />
                </div>
                <div>
                  <label>付款方（可选）</label>
                  <input value={fPayer} onChange={(e) => setFPayer(e.target.value)} placeholder="张三" />
                </div>
                <div className="full">
                  <label>凭证（图片 / PDF，≤5MB；现金加款必须有凭证或审核时写明无凭证原因）</label>
                  <div className="upload-row">
                    <label className="pick">
                      {fUploading ? "上传中…" : fVoucher ? "重新选择" : "选择文件"}
                      <input type="file" accept={ACCEPT} onChange={onPickVoucher} disabled={fUploading || busy} />
                    </label>
                    {fUploading && <span className="thumb-loading" />}
                    {!fUploading && fVoucher && (
                      <>
                        <PathThumb path={fVoucher} onClick={() => openPreview(fVoucher, fVoucherName)} />
                        <span className="upload-name" title={fVoucherName}>
                          {fVoucherName || "已上传"}
                        </span>
                      </>
                    )}
                    {!fUploading && !fVoucher && <span className="faint" style={{ fontSize: 11 }}>可选，未上传</span>}
                  </div>
                  {fVoucherErr && (
                    <div className="err" style={{ marginTop: 8, marginBottom: 0 }}>
                      {fVoucherErr}
                    </div>
                  )}
                </div>
                <div className="full">
                  <label>备注</label>
                  <input value={fRemark} onChange={(e) => setFRemark(e.target.value)} placeholder="客户微信通知" />
                </div>
              </div>
              <div className="faint" style={{ marginTop: 10 }}>
                {fAccount === "CREDIT"
                  ? "授信账户：加款=客户还款（减少已用授信），减款=占用额度（增加已用授信）。审核通过后生效，不产生现金流水。"
                  : "现金账户：审核通过后才入客户钱包；加款进账、减款退款，全部走账本流水，不可删改。"}
              </div>
              {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={submitCreate}
                disabled={busy || fUploading || !fClient || !fAmount.trim()}
              >
                提交申请
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---------------- 处理申请单 ---------------- */}
      {modal?.kind === "review" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>处理申请单 · {modal.item.biz_no}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="kv" style={{ marginBottom: 12 }}>
                <span className="k">客户</span>
                <span className="v">
                  {modal.item.client_name} <span className="faint mono">{modal.item.client_no}</span>
                </span>
                <span className="k">资金账户</span>
                <span className="v">
                  {modal.item.fund_account_type_label} · {modal.item.op_type_label}
                </span>
                <span className="k">申请金额</span>
                <span className="v mono">
                  {money(modal.item.amount)} {modal.item.currency}
                </span>
                <span className="k">应付金额</span>
                <span className="v mono">
                  {money(modal.item.payable_amount)} {modal.item.payable_currency}
                </span>
                <span className="k">交易编号</span>
                <span className="v mono">{modal.item.txn_no || "—"}</span>
                <span className="k">申请人</span>
                <span className="v">{modal.item.applied_by || "—"}</span>
                <span className="k">备注</span>
                <span className="v">{modal.item.remark || "—"}</span>
                <span className="k">凭证</span>
                <span className="v">
                  <VoucherCell row={modal.item} onPreview={openPreview} />
                </span>
              </div>

              {modal.item.voucher_missing && (
                <div className="warnbox">凭证文件已丢失，审核会被拒绝。请让申请人重新上传。</div>
              )}
              {needNoVoucherReason && (
                <div className="field">
                  <label>无凭证原因（必填，不少于 5 字，会写入流水备注与审计）</label>
                  <textarea
                    rows={2}
                    value={noVoucherReason}
                    onChange={(e) => setNoVoucherReason(e.target.value)}
                    placeholder="线下现金收款，已当面核对收款人与身份证"
                  />
                </div>
              )}
              <div className="field">
                <label>到账金额（{modal.item.currency}，按银行实际到账可修正）</label>
                <input value={reviewReceived} onChange={(e) => setReviewReceived(e.target.value)} inputMode="decimal" />
              </div>
              <div className="field">
                <label>审核备注（可选）</label>
                <input
                  value={reviewNote}
                  onChange={(e) => setReviewNote(e.target.value)}
                  placeholder="已核对银行流水"
                />
              </div>
              <div className="warnbox">
                {modal.item.fund_account_type === "CREDIT"
                  ? "授信账户审核通过只改已用授信，不动钱包现金，也不产生流水。"
                  : modal.item.op_type === "INCREASE"
                  ? `审核通过后客户钱包立即进账 ${money(reviewReceived || modal.item.received_amount)} ${modal.item.currency}，重复审核不会重复入账。`
                  : `审核通过后从客户钱包扣减 ${money(reviewReceived || modal.item.received_amount)} ${modal.item.currency}，余额不足会被拒绝。`}
              </div>
              {err && <div className="err">{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn danger" onClick={() => openModal({ kind: "reject", item: modal.item })} disabled={busy}>
                驳回
              </button>
              <span className="spacer" />
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => doApprove(modal.item)}
                disabled={busy || (needNoVoucherReason && noVoucherReason.trim().length < 5)}
                title={needNoVoucherReason && noVoucherReason.trim().length < 5 ? "无凭证审核需填写不少于 5 字的原因" : ""}
              >
                审核通过
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---------------- 驳回 ---------------- */}
      {modal?.kind === "reject" && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>驳回申请 · {modal.item.biz_no}</h2>
              <button className="btn ghost sm" onClick={() => setModal(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <div className="field">
                <label>驳回原因（必填，申请人可见）</label>
                <textarea
                  rows={3}
                  value={rejectReason}
                  onChange={(e) => setRejectReason(e.target.value)}
                  placeholder="银行流水金额与申请不一致"
                />
              </div>
              {err && <div className="err">{err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setModal(null)} disabled={busy}>
                取消
              </button>
              <button className="btn danger" onClick={() => doReject(modal.item)} disabled={busy || !rejectReason.trim()}>
                确认驳回
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---------------- 查看申请单 ---------------- */}
      {detail && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h2>申请单 {detail.item.biz_no}</h2>
                <p>
                  {detail.item.client_name} ·{" "}
                  <span className={`badge ${badgeClass(detail.item.status)}`}>{detail.item.status_label}</span>
                </p>
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>
            <div className="drawer-body">
              {detailErr && <div className="err">{detailErr}</div>}
              <div className="kv">
                <span className="k">资金账户</span>
                <span className="v">
                  {detail.item.fund_account_type_label} · {detail.item.op_type_label}
                </span>
                <span className="k">申请金额</span>
                <span className="v mono">
                  {money(detail.item.amount)} {detail.item.currency}
                </span>
                <span className="k">应付金额</span>
                <span className="v mono">
                  {money(detail.item.payable_amount)} {detail.item.payable_currency}
                </span>
                <span className="k">折算汇率</span>
                <span className="v mono">{detail.item.exchange_rate || "1"}</span>
                <span className="k">汇率来源</span>
                <span className="v">{detail.item.rate_source || "—"}</span>
                <span className="k">到账金额</span>
                <span className="v mono">
                  {money(detail.item.received_amount)} {detail.item.currency}
                </span>
                <span className="k">交易编号</span>
                <span className="v mono">{detail.item.txn_no || "—"}</span>
                <span className="k">付款方</span>
                <span className="v">{detail.item.payer || "—"}</span>
                <span className="k">凭证</span>
                <span className="v">
                  <VoucherCell row={detail.item} onPreview={openPreview} />
                </span>
                <span className="k">申请人</span>
                <span className="v">{detail.item.applied_by || "—"}</span>
                <span className="k">审核人</span>
                <span className="v">
                  {detail.item.reviewed_by || "—"}
                  {detail.item.reviewed_at ? ` · ${fmtTime(detail.item.reviewed_at)}` : ""}
                </span>
                <span className="k">备注</span>
                <span className="v">{detail.item.remark || "—"}</span>
                {detail.item.review_note && (
                  <>
                    <span className="k">审核意见</span>
                    <span className="v">{detail.item.review_note}</span>
                  </>
                )}
                {detail.item.reject_reason && (
                  <>
                    <span className="k">驳回原因</span>
                    <span className="v">{detail.item.reject_reason}</span>
                  </>
                )}
              </div>

              <h3 style={{ marginTop: 18 }}>关联流水</h3>
              <table>
                <thead>
                  <tr>
                    <th>seq</th>
                    <th>方向</th>
                    <th className="num">金额</th>
                    <th className="num">变动后余额</th>
                    <th>业务类型</th>
                    <th>操作人</th>
                    <th>备注</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.ledger.map((tx) => (
                    <tr key={tx.id}>
                      <td className="mono faint">{tx.seq}</td>
                      <td className={tx.direction === "IN" ? "in" : "out"}>{tx.direction === "IN" ? "进" : "出"}</td>
                      <td className="num mono">
                        {money(tx.amount)} {tx.currency}
                      </td>
                      <td className="num mono">{money(tx.balance_after)}</td>
                      <td className="muted">{tx.biz_type}</td>
                      <td className="muted">{tx.operator || "—"}</td>
                      <td className="faint">{tx.remark || "—"}</td>
                    </tr>
                  ))}
                  {detail.ledger.length === 0 && (
                    <tr>
                      <td colSpan={7}>
                        <div className="empty">
                          {detail.item.fund_account_type === "CREDIT"
                            ? "授信账户不产生现金流水，审核通过只改已用授信"
                            : "该单还没有产生流水（未审核通过）"}
                        </div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
              <div className="faint" style={{ marginTop: 8 }}>
                流水只追加，不可改不可删；纠错走资金流水页的冲正（需两人确认）。
              </div>
            </div>
          </div>
        </div>
      )}

      {preview && <PreviewModal preview={preview} onClose={() => setPreview(null)} />}
    </>
  );
}

function PreviewModal({ preview, onClose }: { preview: Preview; onClose: () => void }) {
  const { url, failed } = useFileUrl("desk", preview.url);
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>凭证预览 · {preview.name}</h2>
          <button className="btn ghost sm" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="modal-body voucher-viewer">
          {failed ? (
            <div className="empty">凭证读取失败</div>
          ) : !url ? (
            <div className="empty">加载中…</div>
          ) : isPdf(preview.url) ? (
            <iframe src={url} title="凭证 PDF" />
          ) : (
            <img src={url} alt="凭证" />
          )}
        </div>
      </div>
    </div>
  );
}
