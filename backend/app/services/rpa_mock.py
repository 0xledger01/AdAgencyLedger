"""Mock RPA 协议（契约 6.3）。

真机 RPA 还没接，这里只做三件事：开户排队、加款执行、心跳。
**每条回执都带 `"mock": true`**，防止后续有人把 Mock 结果当成真机结果对账。
"""

from __future__ import annotations

import json
from uuid import uuid4

from ..models.base import utcnow

MOCK_AGENT = "mock-rpa-01"
VERSION = "0.3.0-mock"

# 进程内待办计数（Mock 心跳用，不入库）
_pending: dict[str, int] = {}


def _now() -> str:
    return utcnow().isoformat(sep=" ", timespec="seconds")


def enqueue_open(order) -> str:  # noqa: ANN001
    """开户单进 RPA 队列，返回排队回执 JSON 字符串。"""
    task_id = f"TASK-{uuid4().hex[:12].upper()}"
    _pending[MOCK_AGENT] = _pending.get(MOCK_AGENT, 0) + 1
    payload = {
        "mock": True,
        "task_id": task_id,
        "agent": MOCK_AGENT,
        "doc": "open_request",
        "doc_id": order.id,
        "biz_no": order.biz_no,
        "platform": str(order.platform),
        "status": "QUEUED",
        "queued_at": _now(),
    }
    return json.dumps(payload, ensure_ascii=False)


def exec_recharge(order, outcome: str = "SUCCESS") -> dict:  # noqa: ANN001
    """执行一笔加款。outcome 由调用方传入：SUCCESS / UNKNOWN / REJECTED。"""
    outcome = (outcome or "SUCCESS").upper()
    stamp = f"{utcnow():%Y%m%d}"
    serial_no = None
    approval_no = None

    if outcome == "SUCCESS":
        serial_no = f"MOCK{stamp}{order.id:06d}{uuid4().hex[:4].upper()}"
        approval_no = f"APR{stamp}{order.id:05d}"
        message = "Mock 执行成功"
    elif outcome == "UNKNOWN":
        message = "Mock 无返回（模拟网络超时，真实结果未知）"
    elif outcome == "REJECTED":
        message = "Mock 被媒体侧拒绝"
    else:
        message = f"未知 outcome：{outcome}"

    raw = {
        "mock": True,
        "agent": MOCK_AGENT,
        "status": outcome,
        "code": 0 if outcome == "SUCCESS" else -1,
        "message": message,
        "at": _now(),
    }
    return {
        "mock": True,
        "outcome": outcome,
        "serial_no": serial_no,
        "approval_no": approval_no,
        "raw": raw,
    }


def heartbeat(agent_code: str = MOCK_AGENT) -> dict:
    return {
        "mock": True,
        "agent_code": agent_code,
        "ok": True,
        "at": _now(),
        "version": VERSION,
        "pending_tasks": int(_pending.get(agent_code, 0)),
    }
