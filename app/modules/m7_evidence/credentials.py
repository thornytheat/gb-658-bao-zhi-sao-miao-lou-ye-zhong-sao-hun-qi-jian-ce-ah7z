"""凭据与请求参数的读取。

凭据优先使用显式请求头，其次使用 `Authorization: Bearer <token>`。
这样外包站点既可用 `X-Station-Credential`，也可用标准 Bearer。
"""

from fastapi import Request


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def admin_token(request: Request) -> str | None:
    return (
        request.headers.get("x-admin-token")
        or request.headers.get("x-admin-credential")
        or _bearer(request)
    )


def audit_token(request: Request) -> str | None:
    return (
        request.headers.get("x-audit-token")
        or request.headers.get("x-audit-credential")
        or _bearer(request)
    )


def station_credential(request: Request) -> str | None:
    return (
        request.headers.get("x-station-credential")
        or request.headers.get("x-station-token")
        or _bearer(request)
    )


def idempotency_key(request: Request, payload: dict | None = None) -> str | None:
    """请求标识：优先请求头，其次请求体 request_id。"""
    header = request.headers.get("idempotency-key") or request.headers.get(
        "x-request-id"
    )
    if header:
        return header.strip() or None
    if payload:
        value = payload.get("request_id")
        if value is not None:
            return str(value).strip() or None
    return None


def parse_lines(values) -> list[int] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [item for item in values.replace(" ", "").split(",") if item]
    result = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("BAD_LINE") from exc
        if number < 1:
            raise ValueError("BAD_LINE")
        result.append(number)
    return result or None
