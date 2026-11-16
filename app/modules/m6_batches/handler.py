from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from app.web import page
from .service import demo

router = APIRouter()
ROWS = [{"key":"RUN-658-A","status":"守恒","value":"28"},{"key":"RUN-658-B","status":"复用","value":"28"},{"key":"RUN-658-C","status":"隔离","value":"3"}]

@router.get("/batches", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m6.html", "批次对账", "/api/reconcile", ROWS)

@router.post("/api/reconcile")
def execute():
    return demo()

@router.get("/api/export/evidence.csv")
def export_evidence(request: Request, batch_id: str | None = None, credential: str | None = None):
    """原下载入口已受控：必须指明批次并出示站点凭据，仅导出本站行。"""
    from app.errors import DomainError
    from app.modules.m7_evidence import credentials as evidence_credentials
    from app.modules.m7_evidence.service import controlled_export
    from .exporter import evidence_csv
    try:
        delivered = controlled_export(
            batch_id,
            credential or evidence_credentials.station_credential(request),
            request_id=evidence_credentials.idempotency_key(request),
        )
    except DomainError as exc:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})
    rows = [
        {
            "business_key": row["business_key"],
            "line_no": row["line_no"],
            "reason_code": row["reason_code"],
            "raw_value": row["raw_value"],
        }
        for row in delivered["rows"]
    ]
    body = evidence_csv(rows)
    return PlainTextResponse(body, media_type="text/csv", headers={"content-disposition": f"attachment; filename=evidence-{batch_id}.csv"})
