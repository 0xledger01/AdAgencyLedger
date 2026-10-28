const STATUS_STYLE: Record<string, string> = {
  ACTIVE: "green",
  PENDING_REVIEW: "amber",
  PENDING_RISK: "amber",
  PENDING_LIMIT: "blue",
  REJECTED: "red",
  DISABLED: "gray",
  PENDING_PAY: "amber",
  PENDING_CONFIRM: "amber",
  APPROVED: "green",
  RECEIVED: "green",
  RETURNED: "red",
  REVERSED: "gray",
  NORMAL: "green",
  LIMITED: "amber",
  BANNED: "red",
  TO_VERIFY: "amber",
  SUCCESS: "green",
  PENDING_EXEC: "blue",
  EXECUTING: "blue",
  AUDITING: "blue",
  CANCELLED: "gray",
  // 交易记录（广告账户交易执行口径，与充值单精确状态分开）
  TXN_PENDING: "gray",
  TXN_RUNNING: "blue",
  TXN_VERIFY: "amber",
  TXN_DONE: "green",
  TXN_FAILED: "red",
  TXN_CANCELLED: "gray",
  PENDING_APPROVAL: "amber",
  PENDING_SUBMIT: "blue",
  AGENT_REVIEWING: "blue",
  OPENED: "green",
  AGENT_REJECTED: "red",
  // 问题工单
  PENDING: "amber",
  PROCESSING: "blue",
  RESOLVED: "green",
  CLOSED: "gray",
  // 客户申请（销售线索）
  NEW: "amber",
  CONTACTED: "blue",
  CONVERTED: "green",
};

export function badgeClass(status: string): string {
  return STATUS_STYLE[status] || "gray";
}

const BIZ_LABEL: Record<string, string> = {
  TOPUP: "充值",
  RECHARGE: "加款",
  SPEND: "消耗",
  REBATE: "返点",
  REFUND: "退款",
  ADJUST: "调账",
  TRANSFER: "打给一代",
};

export function bizLabel(code: string, fallback?: string): string {
  return fallback || BIZ_LABEL[code] || code;
}

const STEP_LABEL: Record<string, string> = {
  REVIEW: "销售初审",
  RISK: "风控审核",
  LIMIT: "财务定额",
  RESUBMIT: "客户重提",
};

export function stepLabel(step: string): string {
  return STEP_LABEL[step] || step;
}

const ACTION_LABEL: Record<string, string> = {
  APPROVE: "通过",
  REJECT: "驳回",
  RESUBMIT: "重新提交",
};

export function actionLabel(a: string): string {
  return ACTION_LABEL[a] || a;
}

export function fmtTime(v?: string | null): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(0, 19);
}

export function fmtDate(v?: string | null): string {
  if (!v) return "—";
  return v.slice(0, 10);
}
