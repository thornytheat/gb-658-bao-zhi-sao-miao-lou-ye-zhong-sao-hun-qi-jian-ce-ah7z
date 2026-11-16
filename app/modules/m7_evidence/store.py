"""M7 证据交付的持久层。

设计要点：
- 原件（批次全文）与逐行材料都完整保留，站点交付只是“裁剪视图”，绝不删除或改写原始行。
- 站点凭据只存 SHA-256 摘要，明文仅在签发时返回一次。
- 查阅账与业务材料同库，可取回；账目写入失败时不返回任何原文。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import app.store as app_store
from app.config import TIMEZONE

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS evidence_stations (
        station_id TEXT PRIMARY KEY,
        credential_sha256 TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        terminated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_materials (
        batch_id TEXT PRIMARY KEY,
        original_text TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        accepted_count INTEGER NOT NULL,
        quarantined_count INTEGER NOT NULL,
        registered_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_rows (
        batch_id TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        station_id TEXT,
        kind TEXT NOT NULL,
        business_key TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        raw_value TEXT NOT NULL,
        raw_line TEXT NOT NULL DEFAULT '',
        fields_json TEXT NOT NULL,
        PRIMARY KEY (batch_id, line_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_access_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        actor_kind TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        batch_id TEXT,
        purpose TEXT,
        scope_json TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        content_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        delivered_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_rows_station ON evidence_rows (station_id, batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_rows_batch ON evidence_rows (batch_id, line_no)",
)


def now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def connect() -> sqlite3.Connection:
    """连接当前配置的数据库；模块级 DATABASE_PATH 可被测试替换。"""
    db = app_store.connect()
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def ensure_schema() -> None:
    db = connect()
    try:
        for statement in SCHEMA:
            db.execute(statement)
        # 兼容空库以外的旧库：缺列时补齐，不改动既有数据。
        columns = {row["name"] for row in db.execute("PRAGMA table_info(evidence_rows)")}
        if "raw_line" not in columns:
            db.execute(
                "ALTER TABLE evidence_rows ADD COLUMN raw_line TEXT NOT NULL DEFAULT ''"
            )
        db.commit()
    finally:
        db.close()


@contextmanager
def writing():
    """串行化写事务：解除合作与取件登记都在这里排序。"""
    db = connect()
    db.isolation_level = None
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")
    finally:
        db.close()


def read_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    ensure_schema()
    db = connect()
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
