"""查阅账：先记账、后出原文；同一请求重试复用同一账目。

关键约束：
- 只有账目写入成功并提交后，调用方才会拿到原文。
- request_id 唯一，重试不会新增账目；中断后可用同一 request_id 恢复原请求。
- 记账函数单独暴露，便于在故障注入时验证“记账失败不出原文”。
"""

from app.errors import DomainError

from .store import dumps, now

PENDING = "pending"
DELIVERED = "delivered"
DENIED = "denied"


def record_access(
    db,
    *,
    request_id: str,
    actor_kind: str,
    actor_id: str,
    batch_id: str | None,
    purpose: str | None,
    scope: dict,
    row_count: int,
    content_sha256: str,
) -> dict:
    """写入一条待交付账目；失败时抛异常，调用方不得返回原文。"""
    db.execute(
        "INSERT INTO evidence_access_log (request_id, actor_kind, actor_id, batch_id, purpose, scope_json, row_count, content_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            actor_kind,
            actor_id,
            batch_id,
            purpose,
            dumps(scope),
            row_count,
            content_sha256,
            PENDING,
            now(),
        ),
    )
    return {
        "request_id": request_id,
        "status": PENDING,
        "content_sha256": content_sha256,
    }


def mark_delivered(db, request_id: str) -> None:
    db.execute(
        "UPDATE evidence_access_log SET status = ?, delivered_at = ? WHERE request_id = ? AND status = ?",
        (DELIVERED, now(), request_id, PENDING),
    )


def mark_denied(db, request_id: str) -> None:
    db.execute(
        "UPDATE evidence_access_log SET status = ? WHERE request_id = ? AND status = ?",
        (DENIED, request_id, PENDING),
    )


def existing(db, request_id: str):
    return db.execute(
        "SELECT * FROM evidence_access_log WHERE request_id = ?", (request_id,)
    ).fetchone()


def check_replay(row, *, actor_kind: str, actor_id: str, batch_id, purpose, scope) -> None:
    """同一 request_id 只能恢复原请求，不能借它读取别的范围。"""
    if (
        row["actor_kind"] != actor_kind
        or row["actor_id"] != actor_id
        or (row["batch_id"] or None) != (batch_id or None)
        or (row["purpose"] or None) != (purpose or None)
        or row["scope_json"] != dumps(scope)
    ):
        raise DomainError("REQUEST_CONFLICT", "同一请求标识对应不同请求", 409)


