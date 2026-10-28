import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, money, qs, upload, type Session } from "../api";
import { AuthedThumb } from "../components/AuthedFile";
import { badgeClass, fmtTime } from "../utils";

interface WalletItem {
  currency: string;
  wallet_balance: string;
  frozen: string;
  cash_available: string;
  ad_accounts_balance: string;
  credit_limit: string;
  credit_used: string;
  credit_available: string;
  total_available: string;
}

interface WalletResp {
  company_name: string;
  settle_currency: string;
  items: WalletItem[];
  credit: { currency: string; limit: string; used: string; available: string };
  can_topup: boolean;
  can_repay: boolean;
}

interface Tx {
  id: number;
  biz_no: string;
  biz_type: string;
  biz_type_label: string;
  occurred_at: string;
  direction: "IN" | "OUT";
  amount: string;
  currency: string;
  balance_before: string;
  balance_after: string;
  remark: string | null;
  status: "DONE" | "REVERSED";
  status_label: string;
  feedback: string | null;
  operator: string | null;
}

interface Option {
  code: string;
  label: string;
}

interface Channel {
  code: string;
  label: string;
  enabled: boolean;
  note: string;
}

interface FxQuote {
  amount_usd: string;
  rate: string;
  payable_amount: string;
  payable_currency: string;
  rate_source: string;
  rate_source_label: string;
  quoted_at: string;
  fetched_at: string;
  stale: boolean;
  warning: string | null;
}

interface Filters {
  dateFrom: string;
  dateTo: string;
  bizNo: string;
  bizType: string;
  status: string;
}

const EMPTY_FILTERS: Filters = { dateFrom: "", dateTo: "", bizNo: "", bizType: "", status: "" };

function unitOf(ccy: string): string {
  if (ccy === "USD") return "$";
  if (ccy === "CNY") return "¥";
  if (ccy === "HKD") return "HK$";
  return ccy;
}

/** 充值金额只收 2 位小数；返回 null 表示通过。 */
function amountError(raw: string, max?: string): string | null {
  const s = raw.trim();
  if (!s) return "请输入充值金额";
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return "请输入数字，最多 2 位小数";
  const n = Number(s);
  if (!(n > 0)) return "金额必须大于 0";
  if (max !== undefined && n > Number(max)) return `不能超过 ${money(max)}`;
  return null;
}

function pageWindow(cur: number, pages: number): (number | "…")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const start = Math.max(2, Math.min(cur - 1, pages - 3));
  const end = Math.min(pages - 1, Math.max(cur + 1, 4));
  if (start > 2) out.push("…");
  for (let i = start; i <= end; i += 1) out.push(i);
  if (end < pages - 1) out.push("…");
  out.push(pages);
  return out;
}

export default function PortalWallet({ session }: { session: Session }) {
  const [wallet, setWallet] = useState<WalletResp | null>(null);
  const [txs, setTxs] = useState<Tx[]>([]);
  const [total, setTotal] = useState(0);
  const [bizOptions, setBizOptions] = useState<Option[]>([]);
  const [statusOptions, setStatusOptions] = useState<Option[]>([]);
  const [topups, setTopups] = useState<any[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);

  const [tab, setTab] = useState("ledger");
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<"topup" | "repay" | null>(null);

  // 连续 load 时按序号丢弃过期响应（项目约定：防请求竞态）
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setErr("");
    try {
      const [w, t, tp] = await Promise.all([
        api.get<WalletResp>("portal", "/api/portal/wallet"),
        api.get<any>(
          "portal",
          "/api/portal/wallet/transactions" +
            qs({
              date_from: applied.dateFrom || undefined,
              date_to: applied.dateTo || undefined,
              biz_no: applied.bizNo.trim() || undefined,
              biz_type: applied.bizType || undefined,
              status: applied.status || undefined,
              limit: pageSize,
              offset: (page - 1) * pageSize,
            })
        ),
        api.get<any>("portal", "/api/portal/topups"),
      ]);
      if (seq !== seqRef.current) return;
      setWallet(w);
      setTxs(t.items ?? []);
      setTotal(Number(t.total ?? 0));
      setBizOptions(t.biz_options ?? []);
      setStatusOptions(t.status_options ?? []);
      setTopups(tp.items ?? []);
      setChannels(tp.channels ?? []);
    } catch (e: any) {
      if (seq === seqRef.current) setErr(e?.message || "加载失败");
    }
  }, [applied, page, pageSize]);

  useEffect(() => {
    load();
  }, [load]);

  // 还没登录态/首次加载时先渲染空壳，避免闪一屏 0
  const primary: WalletItem | null = wallet?.items?.[0] ?? null;
  const ccy = primary?.currency || wallet?.settle_currency || "USD";
  const unit = unitOf(ccy);

  function applyFilters() {
    setPage(1);
    setApplied({ ...draft });
  }

  function resetFilters() {
    setDraft(EMPTY_FILTERS);
    setApplied({ ...EMPTY_FILTERS });
    setPage(1);
  }

  async function exportCsv() {
    try {
      await download(
        "portal",
        "/api/portal/wallet/transactions/export" +
          qs({
            date_from: applied.dateFrom || undefined,
            date_to: applied.dateTo || undefined,
            biz_no: applied.bizNo.trim() || undefined,
            biz_type: applied.bizType || undefined,
            status: applied.status || undefined,
          }),
        "我的流水.csv"
      );
    } catch (e: any) {
      setMsg(e?.message || "导出失败");
    }
  }

  const pages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <>
      {err && <div className="err">{err}</div>}
      {msg && <div className="warnbox">{msg}</div>}

      {/* ---------------- 资金账户三卡 ---------------- */}
      <div className="pw-cards">
        <div className="pw-card pw-card-total">
          <div className="pw-card-head">
            <span className="pw-card-title">资金总账户</span>
            <button
              type="button"
              className="pw-icon-btn"
              title="刷新余额"
              onClick={() => load()}
            >
              ⟳
            </button>
          </div>
          <div className="pw-card-label">
            可用余额<span className="pw-unit">{unit}</span>
          </div>
          <div className="pw-amount">{money(primary?.total_available)}</div>
          <div className="pw-card-foot">
            现金可用 {money(primary?.cash_available)} + 授信可用{" "}
            {money(primary?.credit_available)}
          </div>
          {Number(primary?.frozen ?? 0) > 0 && (
            <div className="pw-card-foot pw-warn">
              冻结中 {money(primary?.frozen)}（加款执行中，暂不可用）
            </div>
          )}
        </div>

        <div className="pw-card">
          <div className="pw-card-head">
            <span className="pw-card-title">
              现金账户
              <span className="pw-info" title="客户预存的钱，只能用于给广告账户加款或按规则退款">
                ?
              </span>
            </span>
            <button
              type="button"
              className="pw-link"
              disabled={!wallet?.can_topup}
              title={wallet?.can_topup ? "现金账户充值" : "客户未开通，暂不能充值"}
              onClick={() => setModal("topup")}
            >
              充值
            </button>
          </div>
          <div className="pw-card-label">
            可用余额<span className="pw-unit">{unit}</span>
          </div>
          <div className="pw-amount">{money(primary?.cash_available)}</div>
          <div className="pw-card-foot">
            钱包余额 {money(primary?.wallet_balance)}
            {Number(primary?.frozen ?? 0) > 0 && ` · 冻结 ${money(primary?.frozen)}`}
          </div>
        </div>

        <div className="pw-card">
          <div className="pw-card-head">
            <span className="pw-card-title">
              授信账户
              <span className="pw-info" title="财务审批的授信额度，已用部分占用额度，不产生现金流水">
                ?
              </span>
            </span>
            <button
              type="button"
              className="pw-link"
              disabled={!wallet?.can_repay}
              title={wallet?.can_repay ? "提交授信还款申请" : "当前没有已用授信"}
              onClick={() => setModal("repay")}
            >
              还款
            </button>
          </div>
          <div className="pw-card-label">
            可用余额<span className="pw-unit">{unit}</span>
          </div>
          <div className="pw-amount">{money(primary?.credit_available)}</div>
          <div className="pw-card-foot">
            额度 {money(primary?.credit_limit)} · 已用 {money(primary?.credit_used)}
          </div>
        </div>
      </div>

      <div className="subtabs pw-tabs">
        {[
          ["ledger", "资金账户流水"],
          ["topups", "充值记录"],
        ].map(([k, l]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {l}
          </button>
        ))}
      </div>

      {/* ---------------- 资金账户流水 ---------------- */}
      {tab === "ledger" && (
        <div className="card pw-ledger">
          <div className="card-head pw-filter-head">
            <h2>资金账户流水</h2>
            <div className="pw-filters">
              <label className="pw-date">
                <span>起始日期</span>
                <input
                  type="date"
                  value={draft.dateFrom}
                  onChange={(e) => setDraft({ ...draft, dateFrom: e.target.value })}
                />
              </label>
              <label className="pw-date">
                <span>截止日期</span>
                <input
                  type="date"
                  value={draft.dateTo}
                  onChange={(e) => setDraft({ ...draft, dateTo: e.target.value })}
                />
              </label>
              <input
                className="pw-filter-input"
                placeholder="交易编号：请输入"
                value={draft.bizNo}
                onChange={(e) => setDraft({ ...draft, bizNo: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") applyFilters();
                }}
              />
              <select
                value={draft.bizType}
                onChange={(e) => setDraft({ ...draft, bizType: e.target.value })}
              >
                <option value="">交易类型</option>
                {bizOptions.map((o) => (
                  <option key={o.code} value={o.code}>
                    {o.label}
                  </option>
                ))}
              </select>
              <select
                value={draft.status}
                onChange={(e) => setDraft({ ...draft, status: e.target.value })}
              >
                <option value="">状态</option>
                {statusOptions.map((o) => (
                  <option key={o.code} value={o.code}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button className="btn primary sm" onClick={applyFilters}>
                查询
              </button>
              <button className="btn sm" onClick={resetFilters} title="清空筛选条件">
                重置
              </button>
              <button className="btn sm" onClick={exportCsv}>
                导出
              </button>
            </div>
          </div>

          <div className="table-scroll">
            <table className="pw-table">
              <thead>
                <tr>
                  <th>交易编号</th>
                  <th>交易类型</th>
                  <th className="num">交易金额</th>
                  <th className="num">交易前资金账户余额</th>
                  <th className="num">交易后资金账户余额</th>
                  <th>操作备注</th>
                  <th>状态</th>
                  <th>反馈信息</th>
                  <th>操作人</th>
                  <th>操作时间</th>
                </tr>
              </thead>
              <tbody>
                {txs.map((t) => (
                  <tr key={t.id}>
                    <td className="mono pw-txn-no" title={t.biz_no}>
                      {t.biz_no}
                    </td>
                    <td>
                      <span className="badge gray">{t.biz_type_label}</span>
                    </td>
                    <td className="num mono">
                      {t.direction === "IN" ? "+" : "−"}
                      {money(t.amount)}
                    </td>
                    <td className="num mono">{money(t.balance_before)}</td>
                    <td className="num mono">{money(t.balance_after)}</td>
                    <td className="muted pw-remark">{t.remark || "--"}</td>
                    <td>
                      <span className={`badge ${t.status === "REVERSED" ? "gray" : "green"}`}>
                        {t.status_label}
                      </span>
                    </td>
                    <td className="muted pw-feedback">{t.feedback || "--"}</td>
                    <td>{t.operator || "--"}</td>
                    <td className="mono faint pw-time">{fmtTime(t.occurred_at)}</td>
                  </tr>
                ))}
                {txs.length === 0 && (
                  <tr>
                    <td colSpan={10}>
                      <div className="empty">没有符合筛选条件的流水</div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="pw-pager">
            <span className="faint">共 {total} 条</span>
            <select
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
            >
              {[10, 20, 50].map((n) => (
                <option key={n} value={n}>
                  {n} 条/页
                </option>
              ))}
            </select>
            <div className="pw-pages">
              <button
                className="pw-page"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                ‹
              </button>
              {pageWindow(page, pages).map((p, i) =>
                p === "…" ? (
                  <span key={`gap-${i}`} className="pw-gap">
                    …
                  </span>
                ) : (
                  <button
                    key={p}
                    className={`pw-page ${p === page ? "on" : ""}`}
                    onClick={() => setPage(p)}
                  >
                    {p}
                  </button>
                )
              )}
              <button
                className="pw-page"
                disabled={page >= pages}
                onClick={() => setPage((p) => Math.min(pages, p + 1))}
              >
                ›
              </button>
            </div>
            <span className="faint">共 {pages} 页</span>
          </div>
          <div className="pw-ledger-foot">
            任一历史时点余额 = 该时点最后一条流水的交易后余额；流水只追加，纠错走红字冲正。
          </div>
        </div>
      )}

      {/* ---------------- 充值记录 ---------------- */}
      {tab === "topups" && (
        <div className="card">
          <div className="card-head">
            <h2>充值记录</h2>
            <div className="toolbar">
              <span className="faint">线下汇款提交后由财务确认到账，确认前不入账</span>
              <button
                className="btn primary sm"
                disabled={!wallet?.can_topup}
                onClick={() => setModal("topup")}
              >
                提交充值
              </button>
            </div>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>单号</th>
                  <th>资金账户</th>
                  <th className="num">金额</th>
                  <th>支付方式</th>
                  <th>状态</th>
                  <th>凭证</th>
                  <th>提交时间</th>
                  <th>到账时间</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {topups.map((t, i) => (
                  <tr key={i}>
                    <td className="mono faint">{t.biz_no}</td>
                    <td>
                      <div>{t.fund_account_type_label ?? "现金账户"}</div>
                      <div className="faint" style={{ fontSize: 11 }}>
                        {t.op_type_label ?? "加款"}
                      </div>
                    </td>
                    <td className="num mono">
                      <div>
                        {money(t.amount)} <span className="faint">{t.currency}</span>
                      </div>
                      {t.payable_currency && t.payable_currency !== t.currency && (
                        <div className="faint" style={{ fontSize: 11 }}>
                          应付 {unitOf(t.payable_currency)}{money(t.payable_amount)}
                        </div>
                      )}
                    </td>
                    <td className="muted">
                      {t.payment_channel_label ?? t.payment_method_label ?? t.payment_method}
                    </td>
                    <td>
                      <span className={`badge ${badgeClass(t.status)}`}>
                        {t.status_label ?? t.status}
                      </span>
                      {t.reject_reason && (
                        <div className="faint" style={{ fontSize: 11 }}>
                          {t.reject_reason}
                        </div>
                      )}
                    </td>
                    <td>
                      {t.voucher_url ? (
                        <AuthedThumb
                          kind="portal"
                          path={t.voucher_url}
                          alt="凭证"
                          onClick={(blob) => window.open(blob, "_blank", "noopener")}
                        />
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td className="mono faint">{fmtTime(t.created_at)}</td>
                    <td className="mono faint">{fmtTime(t.confirmed_at)}</td>
                    <td>
                      {t.can_attach_voucher && (
                        <label className="pick-link">
                          <input
                            type="file"
                            accept=".jpg,.jpeg,.png,.webp,.pdf"
                            onChange={(e) => onAttachVoucher(e, t.biz_no)}
                          />
                          补传凭证
                        </label>
                      )}
                    </td>
                  </tr>
                ))}
                {topups.length === 0 && (
                  <tr>
                    <td colSpan={9}>
                      <div className="empty">暂无充值记录</div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {modal === "topup" && (
        <CashTopUpModal
          session={session}
          ccy={ccy}
          unit={unit}
          channels={channels}
          onClose={() => setModal(null)}
          onDone={async (text) => {
            setModal(null);
            setMsg(text);
            setTab("topups");
            await load();
          }}
        />
      )}

      {modal === "repay" && (
        <CreditRepayModal
          session={session}
          creditUsed={primary?.credit_used ?? "0"}
          ccy={ccy}
          unit={unit}
          onClose={() => setModal(null)}
          onDone={async (text) => {
            setModal(null);
            setMsg(text);
            setTab("topups");
            await load();
          }}
        />
      )}

    </>
  );

  async function onAttachVoucher(e: React.ChangeEvent<HTMLInputElement>, bizNo: string) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await upload("portal", file, "TOPUP_VOUCHER");
      const res = await api.post<any>("portal", `/api/portal/topups/${bizNo}/voucher`, {
        voucher: r.url,
      });
      setMsg(`凭证已补传（${bizNo}），当前状态：${res.status_label ?? res.status}`);
      await load();
    } catch (e: any) {
      setMsg(e?.message || "补传凭证失败");
    } finally {
      setBusy(false);
    }
  }
}

/* ------------------------------------------------------------------ 充值弹层 */

function VoucherPicker({
  value,
  name,
  uploading,
  onPick,
  onClear,
}: {
  value: string;
  name: string;
  uploading: boolean;
  onPick: (file: File) => void;
  onClear: () => void;
}) {
  if (value) {
    return (
      <div className="pw-upload pw-upload-done">
        <AuthedThumb
          kind="portal"
          path={value}
          alt="回执"
          className="pw-upload-thumb"
          onClick={(blob) => window.open(blob, "_blank", "noopener")}
        />
        <div className="pw-upload-meta">
          <span className="pw-upload-name" title={name}>
            {name || "已上传回执"}
          </span>
          <div className="pw-upload-actions">
            <label className="link-btn">
              <input
                type="file"
                accept=".jpg,.jpeg,.png,.webp,.pdf"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (f) onPick(f);
                }}
              />
              重新上传
            </label>
            <button type="button" className="link-btn" onClick={onClear}>
              移除
            </button>
          </div>
        </div>
      </div>
    );
  }
  return (
    <label
      className="pw-upload"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        const f = e.dataTransfer.files?.[0];
        if (f) onPick(f);
      }}
    >
      <input
        type="file"
        accept=".jpg,.jpeg,.png,.webp,.pdf"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) onPick(f);
        }}
      />
      {uploading ? (
        <span className="pw-upload-tip">上传中…</span>
      ) : (
        <>
          <span className="pw-upload-icon">⇪</span>
          <span className="pw-upload-main">点击或拖拽上传回执</span>
          <span className="pw-upload-tip">允许上传 jpg/png/pdf 格式的文件</span>
        </>
      )}
    </label>
  );
}

function CashTopUpModal({
  session,
  ccy,
  unit,
  channels,
  onClose,
  onDone,
}: {
  session: Session;
  ccy: string;
  unit: string;
  channels: Channel[];
  onClose: () => void;
  onDone: (text: string) => Promise<void>;
}) {
  const [channel, setChannel] = useState(
    channels.find((c) => c.enabled)?.code ?? "OFFLINE"
  );
  const [amount, setAmount] = useState("");
  const [remark, setRemark] = useState("");
  const [voucherUrl, setVoucherUrl] = useState("");
  const [voucherName, setVoucherName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [touched, setTouched] = useState(false);
  const [quote, setQuote] = useState<FxQuote | null>(null);
  const [quoteErr, setQuoteErr] = useState("");
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteNonce, setQuoteNonce] = useState(0);
  const quoteSeq = useRef(0);

  const amountErr = amountError(amount, "1000000");
  const selected = channels.find((c) => c.code === channel);

  // 金额 → 实时报价。连续输入用递增序号丢弃过期响应，避免 100 的报价盖住 200 的。
  useEffect(() => {
    const invalid = amountError(amount, "1000000");
    const seq = ++quoteSeq.current;
    setQuote(null);
    setQuoteErr("");
    if (ccy !== "USD") {
      setQuoteErr(`当前结算币种为 ${ccy}，门户充值仅支持 USD 到账，请联系客户经理调整`);
      setQuoteLoading(false);
      return;
    }
    if (invalid) {
      setQuoteLoading(false);
      return;
    }
    setQuoteLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const q = await api.get<FxQuote>(
          "portal",
          `/api/portal/topup-quote${qs({ amount: Number(amount).toFixed(2) })}`
        );
        if (seq !== quoteSeq.current) return;
        setQuote(q);
        setErr("");
      } catch (e: any) {
        if (seq !== quoteSeq.current) return;
        setQuoteErr(e?.message || "汇率获取失败，请稍后重试");
      } finally {
        if (seq === quoteSeq.current) setQuoteLoading(false);
      }
    }, 300);
    return () => {
      window.clearTimeout(timer);
      // 卸载时让在途请求失效，避免回来再 setState
      if (quoteSeq.current === seq) quoteSeq.current += 1;
    };
  }, [amount, ccy, quoteNonce]);

  async function pickVoucher(file: File) {
    setUploading(true);
    setErr("");
    try {
      const r = await upload("portal", file, "TOPUP_VOUCHER");
      setVoucherUrl(r.url);
      setVoucherName(r.original_name);
    } catch (e: any) {
      setErr(e?.message || "回执上传失败");
    } finally {
      setUploading(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (amountErr) return;
    if (!quote) {
      setErr(quoteLoading ? "正在获取实时汇率，请稍候" : quoteErr || "请先获取实时汇率");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<any>("portal", "/api/portal/topups", {
        amount: Number(amount).toFixed(2),
        currency: ccy,
        payment_channel: channel,
        expected_rate: quote.rate,
        payer: session.display_name,
        voucher: voucherUrl || null,
        remark: remark.trim() || null,
      });
      await onDone(
        `充值单 ${r.biz_no} 已提交（${r.payment_channel_label ?? selected?.label ?? ""}），` +
          `应付 ¥${money(r.payable_amount)}；${r.status_label ?? "待确认"}，财务确认到账后自动入账`
      );
    } catch (e: any) {
      setErr(e?.message || "提交失败");
      // 汇率漂移：服务端拒绝旧报价，立刻重新取价让客户看新金额
      if (e?.code === "RATE_CHANGED") setQuoteNonce((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div className="modal pw-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>现金账户充值</h2>
          <button type="button" className="pw-close" onClick={onClose} title="关闭">
            ×
          </button>
        </div>
        <form className="modal-body" onSubmit={submit}>
          <div className="pw-field">
            <label className="pw-label">
              <span className="pw-req">*</span> 支付方式
            </label>
            <div className="pw-channels">
              {channels.map((c) => (
                <button
                  key={c.code}
                  type="button"
                  className={`pw-channel ${channel === c.code ? "on" : ""}`}
                  disabled={!c.enabled}
                  title={c.enabled ? c.note : `${c.label}暂未开通，请选择线下汇款`}
                  onClick={() => setChannel(c.code)}
                >
                  {channel === c.code && c.enabled ? (
                    <span className="pw-check">✓</span>
                  ) : null}
                  {c.label}
                </button>
              ))}
            </div>
          </div>

          <div className="pw-field">
            <label className="pw-label">
              <span className="pw-req">*</span> 到账金额（{ccy}）
            </label>
            <div className={`pw-amount-input ${touched && amountErr ? "bad" : ""}`}>
              <span className="pw-amount-unit">{unit}</span>
              <input
                value={amount}
                inputMode="decimal"
                placeholder="请输入(最多2位小数)"
                onChange={(e) => setAmount(e.target.value)}
                onBlur={() => setTouched(true)}
                autoFocus
              />
            </div>
            {touched && amountErr && <div className="pw-field-err">{amountErr}</div>}
            <div className="pw-quote-hint">到账美金按 1 : 1 计入美元钱包，不因汇率缩水</div>
          </div>

          <div className="pw-payable">
            <span className="pw-payable-label">
              应付人民币 <b>¥</b>
            </span>
            <span className="pw-payable-value">
              {quoteLoading ? "…" : quote ? money(quote.payable_amount) : "—"}
            </span>
            <span className="pw-rate">
              {quote
                ? `1 USD = ${quote.rate} CNY · 报价 ${quote.quoted_at}（北京时间） · ${quote.rate_source}`
                : quoteErr
                  ? "汇率获取失败"
                  : "按提交时实时汇率折算"}
            </span>
          </div>
          {quote?.warning && <div className="pw-quote-warn">{quote.warning}</div>}
          {quoteErr && <div className="pw-field-err">{quoteErr}</div>}
          <div className="pw-quote-foot">
            汇率只决定需要转多少人民币；财务按单据上的汇率快照核对，确认到账后才入账。
          </div>

          <div className="pw-assist">
            <div className="pw-assist-title">认款辅助信息</div>
            <div className="pw-assist-sub">
              提交认款辅助信息，有助于确认到账，提升充值速度
            </div>

            <div className="pw-form-row">
              <label className="pw-form-label">转账回执图片</label>
              <VoucherPicker
                value={voucherUrl}
                name={voucherName}
                uploading={uploading}
                onPick={pickVoucher}
                onClear={() => {
                  setVoucherUrl("");
                  setVoucherName("");
                }}
              />
            </div>

            <div className="pw-form-row">
              <label className="pw-form-label">备注</label>
              <div className="pw-textarea-wrap">
                <textarea
                  rows={4}
                  maxLength={200}
                  placeholder="请输入"
                  value={remark}
                  onChange={(e) => setRemark(e.target.value)}
                />
                <span className="pw-counter">{remark.length} / 200</span>
              </div>
            </div>
          </div>

          {err && <div className="err">{err}</div>}

          <div className="modal-foot">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button className="btn primary" disabled={busy || uploading || !quote || quoteLoading}>
              {busy ? "提交中…" : "确认充值"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 授信还款弹层 */

function CreditRepayModal({
  session,
  creditUsed,
  ccy,
  unit,
  onClose,
  onDone,
}: {
  session: Session;
  creditUsed: string;
  ccy: string;
  unit: string;
  onClose: () => void;
  onDone: (text: string) => Promise<void>;
}) {
  const [amount, setAmount] = useState("");
  const [remark, setRemark] = useState("");
  const [voucherUrl, setVoucherUrl] = useState("");
  const [voucherName, setVoucherName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [touched, setTouched] = useState(false);

  const amountErr = amountError(amount, creditUsed);
  const payable = amountErr ? "0.00" : Number(amount).toFixed(2);

  async function pickVoucher(file: File) {
    setUploading(true);
    setErr("");
    try {
      const r = await upload("portal", file, "TOPUP_VOUCHER");
      setVoucherUrl(r.url);
      setVoucherName(r.original_name);
    } catch (e: any) {
      setErr(e?.message || "回执上传失败");
    } finally {
      setUploading(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (amountErr) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.post<any>("portal", "/api/portal/credit-repayments", {
        amount: Number(amount).toFixed(2),
        voucher: voucherUrl || null,
        remark: remark.trim() || null,
      });
      await onDone(
        `还款申请 ${r.biz_no} 已提交，待财务审核；审核通过后已用授信减少 ${money(
          r.amount
        )} ${r.currency}`
      );
    } catch (e: any) {
      setErr(e?.message || "提交失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div className="modal pw-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>授信账户还款</h2>
          <button type="button" className="pw-close" onClick={onClose} title="关闭">
            ×
          </button>
        </div>
        <form className="modal-body" onSubmit={submit}>
          <div className="pw-repay-tip">
            当前已用授信 <b>{money(creditUsed)}</b> {ccy}。还款申请审核通过后恢复额度，
            审核前不动任何余额。
          </div>

          <div className="pw-field">
            <label className="pw-label">
              <span className="pw-req">*</span> 还款金额（{ccy}）
            </label>
            <div className={`pw-amount-input ${touched && amountErr ? "bad" : ""}`}>
              <span className="pw-amount-unit">{unit}</span>
              <input
                value={amount}
                inputMode="decimal"
                placeholder="请输入(最多2位小数)"
                onChange={(e) => setAmount(e.target.value)}
                onBlur={() => setTouched(true)}
                autoFocus
              />
            </div>
            {touched && amountErr && <div className="pw-field-err">{amountErr}</div>}
          </div>

          <div className="pw-payable">
            <span className="pw-payable-label">
              本次还款 <b>{unit}</b>
            </span>
            <span className="pw-payable-value">{payable}</span>
            <span className="pw-rate">线下汇款，财务确认到账后减少已用授信</span>
          </div>

          <div className="pw-assist">
            <div className="pw-assist-title">认款辅助信息</div>
            <div className="pw-assist-sub">
              提交转账回执与备注，财务可以更快核对到账
            </div>
            <div className="pw-form-row">
              <label className="pw-form-label">转账回执图片</label>
              <VoucherPicker
                value={voucherUrl}
                name={voucherName}
                uploading={uploading}
                onPick={pickVoucher}
                onClear={() => {
                  setVoucherUrl("");
                  setVoucherName("");
                }}
              />
            </div>
            <div className="pw-form-row">
              <label className="pw-form-label">备注</label>
              <div className="pw-textarea-wrap">
                <textarea
                  rows={4}
                  maxLength={200}
                  placeholder="请输入"
                  value={remark}
                  onChange={(e) => setRemark(e.target.value)}
                />
                <span className="pw-counter">{remark.length} / 200</span>
              </div>
            </div>
          </div>

          {err && <div className="err">{err}</div>}

          <div className="modal-foot">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button className="btn primary" disabled={busy || uploading}>
              {busy ? "提交中…" : "提交还款申请"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
