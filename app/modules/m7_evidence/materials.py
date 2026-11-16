"""原件登记与站点裁剪。

- 登记使用与采集完全相同的 M1 解析器，不新增/修改采集必需列。
- 站点归属既可从材料内的附加列读取，也可由独立的行映射提供；绝不写回原始 CSV。
- 原始文本、原始行号、原始字段一律保形保存，站点交付只是过滤视图。
"""

import hashlib
import json

from app.errors import DomainError
from app.modules.m1_intake.parser import parse_manifest

from .store import dumps, now

ACCEPTED = "accepted"
QUARANTINED = "quarantined"

STATION_FIELDS = ("station_id", "station", "source_station", "station_code")
CONTENT_FIELDS = ("content", "material", "text", "raw")


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _business_key(row: dict) -> str:
    parts = (row.get("issue"), row.get("edition"), row.get("page"))
    if any(part is None for part in parts):
        return ""
    return "|".join(str(part) for part in parts)


def _row_station(row: dict) -> str | None:
    for field in STATION_FIELDS:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _station_for(line_no: int, key: str, inline: str | None, mapping: dict) -> str | None:
    for candidate in (str(line_no), key):
        if candidate and candidate in mapping:
            value = mapping[candidate]
            if value is None:
                return None
            station_id = str(value).strip()
            return station_id or None
    return inline


def parse_material(text: str) -> tuple[list[dict], list[dict]]:
    """返回 (accepted, quarantined)，字段与原件一致。"""
    accepted_rows, bad_rows = parse_manifest(text)
    physical = _physical_lines(text)
    accepted = []
    for row in accepted_rows:
        fields = {
            key: value
            for key, value in row.items()
            if key != "line_no"
        }
        accepted.append(
            {
                "line_no": int(row["line_no"]),
                "kind": ACCEPTED,
                "business_key": _business_key(row),
                "reason_code": "ACCEPTED",
                "raw_value": row.get("file_name", ""),
                "raw_line": physical.get(int(row["line_no"]), ""),
                "fields": fields,
                "station_id": _row_station(row),
            }
        )
    quarantined = []
    for row in bad_rows:
        line_no = int(row.get("line_no") or 0)
        quarantined.append(
            {
                "line_no": line_no,
                "kind": QUARANTINED,
                "business_key": "",
                "reason_code": str(row.get("reason_code", "BAD_ROW")),
                "raw_value": str(row.get("raw", "")),
                "raw_line": physical.get(line_no, str(row.get("raw", ""))),
                "fields": {},
                "station_id": None,
            }
        )
    return accepted, quarantined


def _physical_lines(text: str) -> dict[int, str]:
    """保留物理行号对应的原始行，去掉行尾换行符，不做字段改写。"""
    lines = {}
    for index, line in enumerate(text.splitlines(keepends=False), 1):
        lines[index] = line.rstrip("\r")
    return lines


def normalize_mapping(row_stations) -> dict:
    if row_stations is None:
        return {}
    if isinstance(row_stations, str):
        try:
            row_stations = json.loads(row_stations)
        except ValueError as exc:
            raise DomainError("BAD_STATION_MAP", "站点映射不是合法 JSON") from exc
    if isinstance(row_stations, dict):
        return {str(key): value for key, value in row_stations.items()}
    if isinstance(row_stations, list):
        mapping = {}
        for item in row_stations:
            if not isinstance(item, dict):
                raise DomainError("BAD_STATION_MAP", "站点映射格式不正确")
            if "line_no" in item:
                mapping[str(item["line_no"])] = item.get("station_id")
            elif "business_key" in item:
                mapping[str(item["business_key"])] = item.get("station_id")
            else:
                raise DomainError("BAD_STATION_MAP", "站点映射缺少行号或业务键")
        return mapping
    raise DomainError("BAD_STATION_MAP", "站点映射格式不正确")


def _extract_content(payload: dict) -> str:
    for field in CONTENT_FIELDS:
        if field in payload:
            value = payload[field]
            if callable(value):
                value = value()
            return "" if value is None else str(value)
    raise DomainError("MATERIAL_REQUIRED", "必须提供真实材料内容", 422)


def register(db, payload: dict) -> dict:
    batch_id = str(payload.get("batch_id") or payload.get("batch") or "").strip()
    if not batch_id:
        raise DomainError("BATCH_REQUIRED", "必须提供批次标识", 422)
    text = _extract_content(payload)

    mapping = normalize_mapping(
        payload.get("row_stations") if "row_stations" in payload else payload.get("stations")
    )
    accepted, quarantined = parse_material(text)
    rows = accepted + quarantined
    digest = content_sha256(text)

    existing = db.execute(
        "SELECT * FROM evidence_materials WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if existing is not None:
        if existing["source_sha256"] == digest and existing["original_text"] == text:
            return material_summary(db, batch_id)
        raise DomainError("BATCH_CONFLICT", "批次已登记且内容不同", 409)

    db.execute(
        "INSERT INTO evidence_materials (batch_id, original_text, source_sha256, row_count, accepted_count, quarantined_count, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, text, digest, len(rows), len(accepted), len(quarantined), now()),
    )
    for row in rows:
        station_id = _station_for(
            row["line_no"], row["business_key"], row.get("station_id"), mapping
        )
        db.execute(
            "INSERT INTO evidence_rows (batch_id, line_no, station_id, kind, business_key, reason_code, raw_value, raw_line, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                row["line_no"],
                station_id,
                row["kind"],
                row["business_key"],
                row["reason_code"],
                row["raw_value"],
                row.get("raw_line", ""),
                dumps(row["fields"]),
            ),
        )
    return material_summary(db, batch_id)


def material_summary(db, batch_id: str) -> dict:
    material = db.execute(
        "SELECT * FROM evidence_materials WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if material is None:
        raise DomainError("BATCH_NOT_FOUND", "批次不存在", 404)
    rows = db.execute(
        "SELECT station_id, COUNT(*) AS count, MIN(line_no) AS first_line, MAX(line_no) AS last_line FROM evidence_rows WHERE batch_id = ? GROUP BY station_id ORDER BY station_id",
        (batch_id,),
    ).fetchall()
    scope = {}
    for item in rows:
        key = item["station_id"] or ""
        scope[key] = {
            "rows": item["count"],
            "first_line": item["first_line"],
            "last_line": item["last_line"],
        }
    return {
        "batch_id": material["batch_id"],
        "source_sha256": material["source_sha256"],
        "row_count": material["row_count"],
        "accepted": material["accepted_count"],
        "quarantined": material["quarantined_count"],
        "registered_at": material["registered_at"],
        "stations": {key: value for key, value in scope.items() if key},
        "unassigned": scope.get("", {}).get("rows", 0),
    }


def row_payload(row) -> dict:
    return {
        "line_no": row["line_no"],
        "station_id": row["station_id"],
        "kind": row["kind"],
        "business_key": row["business_key"],
        "reason_code": row["reason_code"],
        "raw_value": row["raw_value"],
        "raw_line": row["raw_line"],
    }


def station_rows(db, batch_id: str, station_id: str, line_numbers=None) -> list[dict]:
    sql = "SELECT * FROM evidence_rows WHERE batch_id = ? AND station_id = ?"
    params: list = [batch_id, station_id]
    if line_numbers is not None:
        if not line_numbers:
            return []
        placeholders = ",".join("?" for _ in line_numbers)
        sql += f" AND line_no IN ({placeholders})"
        params.extend(sorted(int(n) for n in line_numbers))
    sql += " ORDER BY line_no"
    return [row_payload(row) for row in db.execute(sql, tuple(params)).fetchall()]


def original_text(db, batch_id: str) -> str:
    row = db.execute(
        "SELECT original_text FROM evidence_materials WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise DomainError("BATCH_NOT_FOUND", "批次不存在", 404)
    return row["original_text"]


def scope_digest(batch_id: str, rows: list[dict], full_text: str | None = None) -> str:
    if full_text is not None:
        return content_sha256(full_text)
    return content_sha256(dumps({"batch_id": batch_id, "rows": rows}))
