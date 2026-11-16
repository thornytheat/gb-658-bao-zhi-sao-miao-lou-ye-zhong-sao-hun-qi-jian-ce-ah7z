import os
from pathlib import Path

APP_VERSION = os.getenv("APP_VERSION", "SF-2026.11")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runs.sqlite3"))
RULE_VERSION = "scanfolio-rules-1"
TIMEZONE = "Asia/Shanghai"

# M7 证据交付：管理员令牌只用于登记站点与签发凭据，不能读取原件。
EVIDENCE_ADMIN_TOKEN = os.getenv("EVIDENCE_ADMIN_TOKEN", "scanfolio-admin-dev")
# 审计令牌只用于按用途读取原件，且每次读取先记查阅账。
EVIDENCE_AUDIT_TOKEN = os.getenv("EVIDENCE_AUDIT_TOKEN", "scanfolio-audit-dev")
# 审计用途白名单为空表示接受任意非空用途（仍会逐次记入查阅账）。
EVIDENCE_AUDIT_PURPOSES = tuple(
    item.strip().upper()
    for item in os.getenv("EVIDENCE_AUDIT_PURPOSES", "").split(",")
    if item.strip()
)

