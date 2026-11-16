"""M7 证据交付的 HTTP 入口。

所有业务失败都转换为统一的 DomainError JSON（code/message），便于外包站点与审计
分别按状态码处理：401 凭据缺失/无效、403 越权、404 不存在、409 冲突、410 合作解除。
"""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.errors import DomainError
from app.web import page

from . import auth, credentials, service

router = APIRouter()

ROWS = [
    {"key": "A 站", "status": "合作中", "value": "本站行"},
    {"key": "B 站", "status": "合作中", "value": "本站行"},
    {"key": "总包审计", "status": "按用途", "value": "先记账"},
]


@router.get("/evidence", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "证据交付", "/api/evidence/stations", ROWS)


def _fail(exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message},
    )


def _csv(rows: list[dict]) -> str:
    from app.modules.m6_batches.exporter import evidence_csv

    return evidence_csv(
        [
            {
                "business_key": row["business_key"],
                "line_no": row["line_no"],
                "reason_code": row["reason_code"],
                "raw_value": row["raw_value"],
            }
            for row in rows
        ]
    )


# --- 管理员入口 ---------------------------------------------------------------


@router.post("/api/evidence/stations")
def create_station(request: Request, payload: dict = Body(default={})):
    try:
        auth.require_admin(credentials.admin_token(request))
        return service.register_station(payload.get("station_id"))
    except DomainError as exc:
        return _fail(exc)


@router.post("/api/evidence/stations/{station_id}/terminate")
def terminate(station_id: str, request: Request):
    try:
        auth.require_admin(credentials.admin_token(request))
        return service.terminate_station(station_id)
    except DomainError as exc:
        return _fail(exc)


@router.post("/api/evidence/materials")
def register_material(request: Request, payload: dict = Body(default={})):
    try:
        auth.require_admin(credentials.admin_token(request))
        return service.register_material(payload)
    except DomainError as exc:
        return _fail(exc)


@router.get("/api/evidence/materials")
def list_materials():
    return {"materials": service.list_materials()}


# --- 站点取件 -----------------------------------------------------------------


@router.post("/api/evidence/stations/{station_id}/deliver")
def deliver(station_id: str, request: Request, payload: dict = Body(default={})):
    try:
        line_numbers = credentials.parse_lines(payload.get("line_numbers"))
        return service.station_delivery(
            payload.get("batch_id") or payload.get("batch"),
            station_id,
            credentials.station_credential(request),
            line_numbers,
            request_id=credentials.idempotency_key(request, payload),
            purpose=payload.get("purpose"),
        )
    except DomainError as exc:
        return _fail(exc)
    except ValueError:
        return _fail(DomainError("BAD_LINE", "行号格式不正确", 422))


@router.get("/api/evidence/stations/{station_id}/scope")
def station_scope(station_id: str, request: Request):
    try:
        return service.station_scope(station_id, credentials.station_credential(request))
    except DomainError as exc:
        return _fail(exc)


# --- 审计取件 -----------------------------------------------------------------


@router.post("/api/evidence/audit/read")
def audit_read(request: Request, payload: dict = Body(default={})):
    try:
        return service.audit_read(
            payload.get("batch_id") or payload.get("batch"),
            payload.get("purpose"),
            credentials.idempotency_key(request, payload),
            credentials.audit_token(request),
        )
    except DomainError as exc:
        return _fail(exc)


# --- 现有下载入口：受控 -------------------------------------------------------


@router.get("/api/evidence/download")
def download(
    request: Request,
    batch_id: str | None = None,
    credential: str | None = None,
    format: str | None = None,
):
    try:
        delivered = service.controlled_export(
            batch_id,
            credential or credentials.station_credential(request),
            request_id=credentials.idempotency_key(request),
        )
    except DomainError as exc:
        return _fail(exc)
    if (format or "").lower() == "csv":
        return PlainTextResponse(
            _csv(delivered["rows"]),
            media_type="text/csv",
            headers={
                "content-disposition": f"attachment; filename=evidence-{batch_id}.csv"
            },
        )
    return delivered


# --- 回读 ---------------------------------------------------------------------


@router.get("/api/evidence/scope/{batch_id}")
def material_scope(batch_id: str):
    try:
        return service.material_scope(batch_id)
    except DomainError as exc:
        return _fail(exc)


@router.get("/api/evidence/stations")
def list_stations():
    return {"stations": service.station_scope_all()}


@router.get("/api/evidence/access")
def list_access(batch_id: str | None = None, request_id: str | None = None):
    return {
        "records": service.access_records(batch_id=batch_id, request_id=request_id)
    }
