"""M7 证据交付服务。

三类取件：
- 站点取件：凭本站凭据，只拿到属于自己的行；越权行号、越权批次一律拒绝。
- 审计取件：凭审计令牌 + 用途，先记查阅账并提交，成功后才返回整批原件。
- 现有下载入口：同样要求批次与站点凭据，复用同一套裁剪规则。

并发与幂等：
- 解除合作第一步就登记终止标记，交付在返回前做最终校验；两者并发时以解除为准。
- 交付使用短事务：读取与记账分开，解除操作不会被长时间写锁拖住。
- 每次取件都登记查阅账；request_id 唯一，重试复用原账目、不重复登记。
- 账目写入失败会回滚并抛错，调用方拿不到任何原文。
"""

import json
import secrets
import sqlite3
import uuid

from app.errors import DomainError

from . import access_log, auth, materials
from .store import connect, ensure_schema, read_all, writing

ADMIN = "admin"
STATION = "station"
AUDIT = "audit"
DEFAULT_STATION_PURPOSE = "RESCAN_EVIDENCE"


# --- 管理员：站点登记与凭据签发 ------------------------------------------------


def register_station(station_id: str) -> dict:
    station_id = (station_id or "").strip()
    if not station_id:
        raise DomainError("STATION_REQUIRED", "必须提供站点标识", 422)
    credential = secrets.token_urlsafe(32)
    ensure_schema()
    with writing() as db:
        found = db.execute(
            "SELECT * FROM evidence_stations WHERE station_id = ?", (station_id,)
        ).fetchone()
        if found is not None and found["status"] == auth.ACTIVE:
            raise DomainError("STATION_EXISTS", "站点已登记", 409)
        credential_digest = auth.digest(credential)
        if found is None:
            db.execute(
                "INSERT INTO evidence_stations (station_id, credential_sha256, status, created_at) VALUES (?, ?, ?, ?)",
                (station_id, credential_digest, auth.ACTIVE, materials.now()),
            )
        else:
            # 已解除的站点可重新合作：签发全新凭据，旧凭据摘要依旧失效。
            db.execute(
                "UPDATE evidence_stations SET credential_sha256 = ?, status = ?, created_at = ?, terminated_at = NULL WHERE station_id = ?",
                (credential_digest, auth.ACTIVE, materials.now(), station_id),
            )
    return {
        "station_id": station_id,
        "credential": credential,
        "status": auth.ACTIVE,
        "note": "凭据明文仅此一次返回，服务端只保存摘要",
    }


def terminate_station(station_id: str) -> dict:
    ensure_schema()
    found = _station_row(station_id)
    # 第一步：登记终止标记。此后任何取件都会看到“已解除”。
    auth.mark_terminated(found["credential_sha256"])
    # 第二步：进入交付闸门。与并发的最终交付串行：解除先到则交付被拒；
    # 交付先完成则它在解除生效前已结束，不会出现“解除后仍下发”的情况。
    with auth.delivery_gate:
        with writing() as db:
            db.execute(
                "UPDATE evidence_stations SET status = ?, terminated_at = ? WHERE station_id = ? AND status = ?",
                (auth.TERMINATED, materials.now(), station_id, auth.ACTIVE),
            )
            result = db.execute(
                "SELECT * FROM evidence_stations WHERE station_id = ?", (station_id,)
            ).fetchone()
            return {
                "station_id": result["station_id"],
                "status": result["status"],
                "terminated_at": result["terminated_at"],
            }


def _station_row(station_id: str):
    db = connect()
    try:
        row = db.execute(
            "SELECT * FROM evidence_stations WHERE station_id = ?", (station_id,)
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise DomainError("STATION_NOT_FOUND", "站点不存在", 404)
    return row


def register_material(payload: dict) -> dict:
    ensure_schema()
    with writing() as db:
        return materials.register(db, payload)


# --- 站点取件 ----------------------------------------------------------------


def station_delivery(
    batch_id: str,
    station_id: str | None,
    credential: str | None,
    line_numbers=None,
    request_id: str | None = None,
    purpose: str | None = None,
) -> dict:
    ensure_schema()
    db = connect()
    try:
        station = auth.station_from_credential(db, credential, station_id)
        _require_batch(db, batch_id)
        rows = materials.station_rows(db, batch_id, station["station_id"], line_numbers)
        _assert_owned(db, batch_id, station["station_id"], rows, line_numbers)
        summary = materials.material_summary(db, batch_id)
    finally:
        db.close()

    scope = _station_scope(batch_id, station["station_id"], line_numbers)
    digest = materials.scope_digest(batch_id, rows)
    clean_purpose = (purpose or DEFAULT_STATION_PURPOSE).strip() or DEFAULT_STATION_PURPOSE
    logged = _log_once(
        request_id=request_id,
        actor_kind=STATION,
        actor_id=station["station_id"],
        batch_id=batch_id,
        purpose=clean_purpose,
        scope=scope,
        row_count=len(rows),
        content_sha256=digest,
        deliver=False,
    )
    # 交付前最终校验：解除合作一旦发生，以解除为准，正文不下发且账目转为拒绝。
    try:
        _finalize_delivery(logged["request_id"], station["station_id"])
    except DomainError:
        _mark(logged["request_id"], access_log.DENIED)
        raise
    return {
        "batch_id": batch_id,
        "station_id": station["station_id"],
        "request_id": logged["request_id"],
        "rows": rows,
        "row_count": len(rows),
        "source_sha256": summary["source_sha256"],
        "content_sha256": digest,
        "scope": "station",
        "note": "仅返回本站行，其他站点行与原始全文不下发",
    }


def station_scope(station_id: str, credential: str | None) -> dict:
    ensure_schema()
    db = connect()
    try:
        station = auth.station_from_credential(db, credential, station_id)
        rows = db.execute(
            "SELECT batch_id, COUNT(*) AS count, MIN(line_no) AS first_line, MAX(line_no) AS last_line FROM evidence_rows WHERE station_id = ? GROUP BY batch_id ORDER BY batch_id",
            (station["station_id"],),
        ).fetchall()
        result = {
            "station_id": station["station_id"],
            "status": station["status"],
            "batches": {
                row["batch_id"]: {
                    "rows": row["count"],
                    "first_line": row["first_line"],
                    "last_line": row["last_line"],
                }
                for row in rows
            },
        }
        # 回读也在闸门内做最终状态校验，解除后不返回可交付范围。
        with auth.delivery_gate:
            auth.ensure_active(db, station["station_id"])
        return result
    finally:
        db.close()


def _station_scope(batch_id: str, station_id: str, line_numbers) -> dict:
    return {
        "batch": batch_id,
        "scope": "station",
        "station_id": station_id,
        "line_numbers": sorted({int(n) for n in line_numbers})
        if line_numbers is not None
        else None,
    }


def _assert_owned(db, batch_id, station_id, rows, line_numbers) -> None:
    if line_numbers is not None:
        wanted = {int(number) for number in line_numbers}
        got = {row["line_no"] for row in rows}
        if wanted - got:
            raise DomainError("ROW_FORBIDDEN", "请求包含不属于本站或越界的行", 403)
        return
    if rows:
        return
    # 整批取件但本站毫无材料：视为越权，避免借批量取件探测他站批次。
    total = db.execute(
        "SELECT COUNT(*) AS count FROM evidence_rows WHERE batch_id = ?", (batch_id,)
    ).fetchone()["count"]
    if int(total) > 0:
        raise DomainError("STATION_FORBIDDEN", "该批次没有本站可交付材料", 403)


# --- 查阅账 -------------------------------------------------------------------


def _log_once(
    *,
    request_id,
    actor_kind,
    actor_id,
    batch_id,
    purpose,
    scope,
    row_count,
    content_sha256,
    deliver: bool = True,
) -> dict:
    """登记一次取件；同一 request_id 不重复登记，返回已存在的账目即视为恢复。"""
    request_id = (request_id or "").strip() or uuid.uuid4().hex
    with writing() as db:
        replay = access_log.existing(db, request_id)
        if replay is not None:
            access_log.check_replay(
                replay,
                actor_kind=actor_kind,
                actor_id=actor_id,
                batch_id=batch_id,
                purpose=purpose,
                scope=scope,
            )
            if deliver and replay["status"] != access_log.DENIED:
                access_log.mark_delivered(db, request_id)
            return {"request_id": request_id, "resumed": True}
        try:
            access_log.record_access(
                db,
                request_id=request_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                batch_id=batch_id,
                purpose=purpose,
                scope=scope,
                row_count=row_count,
                content_sha256=content_sha256,
            )
        except sqlite3.IntegrityError as exc:
            raise DomainError("REQUEST_CONFLICT", "同一请求标识已并发登记", 409) from exc
        if deliver:
            access_log.mark_delivered(db, request_id)
        return {"request_id": request_id, "resumed": False}


def _ensure_active(station_id: str) -> None:
    db = connect()
    try:
        auth.ensure_active(db, station_id)
    finally:
        db.close()


def _finalize_delivery(request_id: str, station_id: str) -> None:
    """在解除/交付互斥闸门内做最终校验并落交付标记。

    - 解除先取得闸门：交付在此看到终止标记，转 DENIED 并拒绝。
    - 交付先取得闸门：交付在解除生效前完成，账目为 delivered。
    """
    db = connect()
    try:
        def commit():
            _mark(request_id, access_log.DELIVERED)
            return True

        auth.finalize_delivery(db, station_id, commit)
    finally:
        db.close()


def _mark(request_id: str, status: str) -> None:
    with writing() as db:
        if status == access_log.DELIVERED:
            access_log.mark_delivered(db, request_id)
        elif status == access_log.DENIED:
            access_log.mark_denied(db, request_id)


# --- 审计取件：先记账，后出原文 ------------------------------------------------


def audit_read(
    batch_id: str,
    purpose: str | None,
    request_id: str,
    audit_token: str | None,
) -> dict:
    ensure_schema()
    auth.require_audit(audit_token)
    clean_purpose = auth.require_purpose(purpose)
    request_id = (request_id or "").strip()
    if not request_id:
        raise DomainError(
            "REQUEST_ID_REQUIRED", "审计读取必须携带请求标识以便幂等与恢复", 422
        )
    scope = {"batch": batch_id, "scope": "original"}

    # 第一阶段：账目。已有已交付账目直接重放；已有 pending 账目则只补交付。
    recorded = _prepare_audit_log(batch_id, clean_purpose, request_id, scope)
    if recorded["status"] == access_log.DENIED:
        raise DomainError("ACCESS_NOT_DELIVERED", "原请求未完成交付", 409)

    # 第二阶段：账目已提交，才读取并返回原文；交付状态更新失败则原文不出函数。
    with writing() as db:
        access_log.mark_delivered(db, request_id)
        return _delivery(db, batch_id, clean_purpose, request_id)


def _prepare_audit_log(batch_id, purpose, request_id, scope) -> dict:
    with writing() as db:
        replay = access_log.existing(db, request_id)
        if replay is not None:
            access_log.check_replay(
                replay,
                actor_kind=AUDIT,
                actor_id=AUDIT,
                batch_id=batch_id,
                purpose=purpose,
                scope=scope,
            )
            return {"status": replay["status"]}
        _require_batch(db, batch_id)
        text = materials.original_text(db, batch_id)
        digest = materials.content_sha256(text)
        try:
            access_log.record_access(
                db,
                request_id=request_id,
                actor_kind=AUDIT,
                actor_id=AUDIT,
                batch_id=batch_id,
                purpose=purpose,
                scope=scope,
                row_count=_row_count(db, batch_id),
                content_sha256=digest,
            )
        except sqlite3.IntegrityError as exc:  # 并发重试抢先登记
            raise DomainError("REQUEST_CONFLICT", "同一请求标识已并发登记", 409) from exc
        return {"status": access_log.PENDING}


def _delivery(db, batch_id, purpose, request_id) -> dict:
    text = materials.original_text(db, batch_id)
    digest = materials.content_sha256(text)
    return {
        "batch_id": batch_id,
        "request_id": request_id,
        "purpose": purpose,
        "content_sha256": digest,
        "content": text,
        "row_count": _row_count(db, batch_id),
        "scope": "original",
    }


def _row_count(db, batch_id: str) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS count FROM evidence_rows WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return int(row["count"])


def _require_batch(db, batch_id: str | None) -> None:
    if not batch_id:
        raise DomainError("BATCH_REQUIRED", "必须指明批次", 422)
    found = db.execute(
        "SELECT 1 FROM evidence_materials WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if found is None:
        raise DomainError("BATCH_NOT_FOUND", "批次不存在", 404)


# --- 现有下载入口：受控 -------------------------------------------------------


def controlled_export(
    batch_id: str | None,
    credential: str | None,
    line_numbers=None,
    request_id: str | None = None,
) -> dict:
    """替代原有无凭据导出；必须指明批次并出示站点凭据。"""
    ensure_schema()
    db = connect()
    try:
        station = auth.station_from_credential(db, credential)
        _require_batch(db, batch_id)
        rows = materials.station_rows(db, batch_id, station["station_id"], line_numbers)
        _assert_owned(db, batch_id, station["station_id"], rows, line_numbers)
        if not rows:
            raise DomainError("NOTHING_TO_EXPORT", "没有本站可交付的材料", 404)
    finally:
        db.close()

    scope = _station_scope(batch_id, station["station_id"], line_numbers)
    digest = materials.scope_digest(batch_id, rows)
    logged = _log_once(
        request_id=request_id,
        actor_kind=STATION,
        actor_id=station["station_id"],
        batch_id=batch_id,
        purpose=DEFAULT_STATION_PURPOSE,
        scope=scope,
        row_count=len(rows),
        content_sha256=digest,
        deliver=False,
    )
    try:
        _finalize_delivery(logged["request_id"], station["station_id"])
    except DomainError:
        _mark(logged["request_id"], access_log.DENIED)
        raise
    return {
        "batch_id": batch_id,
        "station_id": station["station_id"],
        "request_id": logged["request_id"],
        "rows": rows,
        "row_count": len(rows),
        "content_sha256": digest,
    }


# --- 回读 --------------------------------------------------------------------


def list_materials() -> list[dict]:
    ensure_schema()
    db = connect()
    try:
        return [
            materials.material_summary(db, row["batch_id"])
            for row in db.execute(
                "SELECT batch_id FROM evidence_materials ORDER BY batch_id"
            ).fetchall()
        ]
    finally:
        db.close()


def material_scope(batch_id: str) -> dict:
    ensure_schema()
    db = connect()
    try:
        _require_batch(db, batch_id)
        return materials.material_summary(db, batch_id)
    finally:
        db.close()


def station_scope_all() -> list[dict]:
    ensure_schema()
    result = []
    for station in read_all(
        "SELECT station_id, status, created_at, terminated_at FROM evidence_stations ORDER BY station_id"
    ):
        counts = read_all(
            "SELECT batch_id, COUNT(*) AS count, MIN(line_no) AS first_line, MAX(line_no) AS last_line FROM evidence_rows WHERE station_id = ? GROUP BY batch_id ORDER BY batch_id",
            (station["station_id"],),
        )
        result.append(
            {
                "station_id": station["station_id"],
                "status": station["status"],
                "created_at": station["created_at"],
                "terminated_at": station["terminated_at"],
                "batches": {
                    row["batch_id"]: {
                        "rows": row["count"],
                        "first_line": row["first_line"],
                        "last_line": row["last_line"],
                    }
                    for row in counts
                },
            }
        )
    return result


def access_records(
    batch_id: str | None = None, request_id: str | None = None
) -> list[dict]:
    ensure_schema()
    clauses, params = [], []
    if batch_id is not None:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    if request_id is not None:
        clauses.append("request_id = ?")
        params.append(request_id)
    sql = "SELECT * FROM evidence_access_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY log_id"
    records = []
    for row in read_all(sql, tuple(params)):
        item = dict(row)
        item["scope"] = _loads(item.pop("scope_json"))
        records.append(item)
    return records


def _loads(value: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
