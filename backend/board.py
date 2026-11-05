from __future__ import annotations

COLUMN_LABELS = {
    "waiting": "待处理",
    "active": "进行中",
    "attention": "需要关注",
    "done": "已完成",
}
_ATTENTION = {"failed", "changes_requested", "uncertain", "needs_configuration", "inconclusive"}
_DONE = {"merged", "closed", "deleted", "success"}


def board_column(*, status: str, kind: str = "", stage: str = "", deleted: bool = False) -> str:
    visible = "deleted" if deleted else status
    if status in _ATTENTION:
        return "attention"
    if visible in _DONE or status in _DONE:
        return "done"
    if status == "running" or (kind == "pr" and stage == "review" and status in {"queued", "running"}):
        return "active"
    return "waiting"
