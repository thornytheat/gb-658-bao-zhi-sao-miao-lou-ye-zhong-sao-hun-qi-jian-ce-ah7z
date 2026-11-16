"""凭据校验：站点凭据只保存摘要，解除合作后按摘要立即失效。

并发约定：解除合作先登记“已解除”标记（按凭据摘要），再落库；取件在返回前
二次检查标记，因此解除与取件真正并发时，以解除为准。
"""

import hashlib
import hmac
import threading

from app import config
from app.errors import DomainError

ACTIVE = "active"
TERMINATED = "terminated"

_termination_lock = threading.RLock()
_terminated_digests: set[str] = set()

# 交付线性化点与解除合作的互斥闸门：谁先拿到闸门谁先生效。
# 解除拿闸门 → 交付必见终止标记并拒绝；交付先拿闸门 → 该请求在解除生效前已完成。
# 使用可重入锁：同一线程内“终止校验”可以安全地触发解除流程而不自锁。
delivery_gate = threading.RLock()


def mark_terminated(credential_digest: str) -> None:
    with _termination_lock:
        _terminated_digests.add(credential_digest)


def is_terminated(credential_digest: str) -> bool:
    with _termination_lock:
        return credential_digest in _terminated_digests


def digest(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _compare(left: str, right: str) -> bool:
    return bool(left) and bool(right) and hmac.compare_digest(left, right)


def require_admin(token: str | None) -> None:
    if not token:
        raise DomainError("MISSING_CREDENTIAL", "缺少管理员凭据", 401)
    if not _compare(token, config.EVIDENCE_ADMIN_TOKEN):
        raise DomainError("INVALID_CREDENTIAL", "管理员凭据无效", 401)


def require_audit(token: str | None) -> None:
    if not token:
        raise DomainError("MISSING_CREDENTIAL", "缺少审计凭据", 401)
    if not _compare(token, config.EVIDENCE_AUDIT_TOKEN):
        raise DomainError("INVALID_CREDENTIAL", "审计凭据无效", 401)


def require_purpose(purpose: str | None) -> str:
    value = (purpose or "").strip()
    if not value:
        raise DomainError("PURPOSE_REQUIRED", "审计读取原件必须声明用途", 422)
    allowed = config.EVIDENCE_AUDIT_PURPOSES
    if allowed and value.upper() not in allowed:
        raise DomainError("PURPOSE_NOT_ALLOWED", "用途不在允许范围内", 422)
    return value


def station_from_credential(db, credential: str | None, expected_station_id: str | None = None):
    """按凭据摘要定位站点；terminated 状态或终止标记即旧凭据作废。"""
    if not credential:
        raise DomainError("MISSING_CREDENTIAL", "缺少站点凭据", 401)
    row = db.execute(
        "SELECT * FROM evidence_stations WHERE credential_sha256 = ?",
        (digest(credential),),
    ).fetchone()
    if row is None:
        raise DomainError("INVALID_CREDENTIAL", "站点凭据无效", 401)
    if expected_station_id and row["station_id"] != expected_station_id:
        raise DomainError("STATION_FORBIDDEN", "该凭据无权取走其他站点材料", 403)
    if row["status"] != ACTIVE or is_terminated(row["credential_sha256"]):
        raise DomainError("STATION_TERMINATED", "合作已解除，凭据立即失效", 410)
    return row


def ensure_active(db, station_id: str) -> None:
    """交付返回前的二次检查；终止标记优先于数据库状态。"""
    row = db.execute(
        "SELECT status, credential_sha256 FROM evidence_stations WHERE station_id = ?",
        (station_id,),
    ).fetchone()
    if row is None:
        raise DomainError("STATION_NOT_FOUND", "站点不存在", 404)
    if is_terminated(row["credential_sha256"]) or row["status"] != ACTIVE:
        raise DomainError("STATION_TERMINATED", "合作已解除，凭据立即失效", 410)


def finalize_delivery(db, station_id: str, build):
    """在互斥闸门内完成“最终校验 + 产出交付物”。

    解除合作先置终止标记；此后任何 delivery 都会在闸门内看到标记并拒绝。
    若 delivery 先进入闸门，则它在解除生效前已经完成，属串行先行。
    """
    with delivery_gate:
        ensure_active(db, station_id)
        return build()
