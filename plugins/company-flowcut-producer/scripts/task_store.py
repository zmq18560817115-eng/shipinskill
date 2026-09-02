#!/usr/bin/env python3
"""Standalone SQLite task store for Company FlowCut Producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUSES = {
    "queued", "running", "awaiting_human", "succeeded", "failed",
    "blocked", "needs_review", "cancelled",
}
STAGES = {"intake", "context", "planning", "editing", "qa", "export"}
TRANSITIONS = {
    "queued": {"running", "blocked", "cancelled"},
    "running": {"awaiting_human", "succeeded", "failed", "blocked", "needs_review", "cancelled"},
    "awaiting_human": {"running", "blocked", "cancelled"},
    "needs_review": {"running", "succeeded", "failed", "cancelled"},
    "failed": {"queued", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "succeeded": set(),
    "cancelled": set(),
}
JOB_ID_RE = re.compile(r"^VID-\d{8}-[A-Z0-9]{6}$")
REQUIRED_REQUEST_FIELDS = {
    "product_id", "goal", "platform", "aspect_ratio", "duration_seconds",
    "language", "edit_mode", "ai_generation_policy", "output_count",
}
REQUEST_FIELDS = REQUIRED_REQUEST_FIELDS | {
    "voice_mode", "source_media", "must_keep", "must_avoid", "notes",
}
ASPECT_RATIOS = {"9:16", "16:9", "1:1", "4:5"}
VOICE_MODES = {"none", "existing", "tts", "original_audio"}
EDIT_MODES = {"existing_footage", "ai_generated", "hybrid", "remix", "multi_variant"}
AI_POLICIES = {"none", "confirm_exact"}
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PLUGIN_ROOT / "department-config" / "company-video.json"
CURRENT_SCHEMA_VERSION = 2


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    parent_job_id       TEXT REFERENCES jobs(job_id) ON DELETE RESTRICT,
    status              TEXT NOT NULL CHECK (status IN (
                            'queued','running','awaiting_human','succeeded','failed',
                            'blocked','needs_review','cancelled'
                        )),
    stage               TEXT NOT NULL DEFAULT 'intake' CHECK (stage IN (
                            'intake','context','planning','editing','qa','export'
                        )),
    request_json        TEXT NOT NULL,
    source_context_json TEXT NOT NULL DEFAULT '{}',
    directives_json     TEXT NOT NULL DEFAULT '[]',
    currency            TEXT NOT NULL DEFAULT 'CNY',
    estimated_cost      REAL,
    actual_cost         REAL,
    revision            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    occurred_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    link_type       TEXT NOT NULL,
    value           TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    UNIQUE(job_id, link_type, value)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    operation       TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    scope_json      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    decided_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER PRIMARY KEY,
    description     TEXT NOT NULL,
    applied_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON job_events(job_id, event_id);
CREATE INDEX IF NOT EXISTS idx_links_job_id ON job_links(job_id, link_id);
CREATE INDEX IF NOT EXISTS idx_approvals_job_id ON approvals(job_id, approval_id);
"""


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_db_path() -> Path:
    try:
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        configured = str(config.get("storage", {}).get("task_database", {}).get("default_local") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        configured = ""
    if configured:
        expanded = os.path.expandvars(configured)
        if "%" not in expanded:
            return Path(expanded)
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / ".local" / "share")
    return Path(base) / "CompanyVideoWorkbench" / "tasks.sqlite3"


def resolve_db_path(value: str | None) -> Path:
    raw = value or os.environ.get("COMPANY_VIDEO_DB_PATH", "")
    path = Path(os.path.expandvars(raw)) if raw.strip() else default_db_path()
    if _is_network_path(path):
        raise ValueError("SQLite task database must be on a local disk, not a network share")
    if any(_paths_overlap(path, root) for root in _configured_source_roots()):
        raise ValueError("SQLite task database must not be stored inside a configured product or asset source root")
    if path.name in {"", ".", ".."} or path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        raise ValueError("task database path must name a .sqlite, .sqlite3, or .db file")
    return path.resolve()


def _is_network_path(path: Path) -> bool:
    if str(path).startswith("\\\\"):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        import ctypes

        drive_root = f"{path.drive}\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive_root)) == 4
    except (AttributeError, OSError, ValueError):
        return False


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expandvars(str(path))))


def _paths_overlap(left: Path, right: Path) -> bool:
    left_value = _normalized_path(left)
    right_value = _normalized_path(right)
    try:
        common = os.path.commonpath([left_value, right_value])
    except ValueError:
        return False
    return common in {left_value, right_value}


def _configured_source_roots() -> list[Path]:
    try:
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        config = {}
    roots: list[Path] = []
    for kind, fallback_env in (
        ("product_sources", "COMPANY_VIDEO_PRODUCT_ROOTS"),
        ("asset_sources", "COMPANY_VIDEO_ASSET_ROOTS"),
    ):
        entry = config.get("storage", {}).get(kind, {})
        env_name = str(entry.get("paths_env") or fallback_env)
        override = os.environ.get(env_name, "").strip()
        values = [item.strip() for item in override.split(";") if item.strip()] if override else [
            str(item) for item in entry.get("paths", [])
        ]
        roots.extend(Path(os.path.expandvars(item)) for item in values)
    return roots


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db_connection(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> dict[str, Any]:
    with db_connection(db_path) as conn:
        recorded_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        has_migration_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone() is not None
        if has_migration_table:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            recorded_version = max(recorded_version, int(row[0] or 0))
        if recorded_version > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"task database schema {recorded_version} is newer than supported {CURRENT_SCHEMA_VERSION}"
            )
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        migrations = []
        if "parent_job_id" not in columns:
            migrations.append("ALTER TABLE jobs ADD COLUMN parent_job_id TEXT REFERENCES jobs(job_id) ON DELETE RESTRICT")
        if "stage" not in columns:
            migrations.append(
                "ALTER TABLE jobs ADD COLUMN stage TEXT NOT NULL DEFAULT 'intake' "
                "CHECK (stage IN ('intake','context','planning','editing','qa','export'))"
            )
        if "directives_json" not in columns:
            migrations.append("ALTER TABLE jobs ADD COLUMN directives_json TEXT NOT NULL DEFAULT '[]'")
        for statement in migrations:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
            (CURRENT_SCHEMA_VERSION, "standalone task store with recovery metadata", utc_now()),
        )
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
    return {
        "ok": True,
        "database": str(db_path),
        "storage_backend": "standalone_sqlite",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "migrations_applied": len(migrations),
        "tables": tables,
    }


def require_existing_db(db_path: Path) -> None:
    if not db_path.is_file():
        raise ValueError(f"task database does not exist: {db_path}; run init only for a new workspace")
    init_db(db_path)


def validate_request(payload: dict[str, Any]) -> None:
    missing = sorted(field for field in REQUIRED_REQUEST_FIELDS if field not in payload)
    if missing:
        raise ValueError(f"missing request fields: {', '.join(missing)}")
    unexpected = sorted(set(payload) - REQUEST_FIELDS)
    if unexpected:
        raise ValueError(f"unsupported request fields: {', '.join(unexpected)}")
    for field in ("product_id", "goal", "platform", "language"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if payload.get("aspect_ratio") not in ASPECT_RATIOS:
        raise ValueError("unsupported aspect_ratio")
    if payload.get("edit_mode") not in EDIT_MODES:
        raise ValueError("unsupported edit_mode")
    if payload.get("ai_generation_policy") not in AI_POLICIES:
        raise ValueError("unsupported ai_generation_policy")
    if "voice_mode" in payload and payload.get("voice_mode") not in VOICE_MODES:
        raise ValueError("unsupported voice_mode")
    if not isinstance(payload.get("duration_seconds"), int) or not 1 <= payload["duration_seconds"] <= 600:
        raise ValueError("duration_seconds must be an integer from 1 to 600")
    if not isinstance(payload.get("output_count"), int) or not 1 <= payload["output_count"] <= 20:
        raise ValueError("output_count must be an integer from 1 to 20")
    for field in ("source_media", "must_keep", "must_avoid"):
        if field in payload and (
            not isinstance(payload[field], list) or not all(isinstance(item, str) for item in payload[field])
        ):
            raise ValueError(f"{field} must be an array of strings")
    if "notes" in payload and not isinstance(payload["notes"], str):
        raise ValueError("notes must be a string")


def generate_job_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"VID-{stamp}-{''.join(secrets.choice(alphabet) for _ in range(6))}"


def read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _contains_key(value: Any, forbidden: str) -> bool:
    if isinstance(value, dict):
        return forbidden in value or any(_contains_key(item, forbidden) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def validate_source_context(request: dict[str, Any], source_context: dict[str, Any]) -> None:
    if source_context.get("product_id") != request.get("product_id"):
        raise ValueError("source context product_id must match request product_id")
    if source_context.get("blocking_issue"):
        raise ValueError("source context contains a blocking issue")
    if source_context.get("source_level") in {None, "", "unavailable"}:
        raise ValueError("source context must identify an approved source level")
    documents = source_context.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("source context must include at least one source document")
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("source context documents must be objects")
        if not all(isinstance(document.get(key), str) and document[key] for key in ("path", "sha256", "modified_at")):
            raise ValueError("each source document requires path, sha256, and modified_at")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", document["sha256"]):
            raise ValueError("source document sha256 must contain 64 hexadecimal characters")
    if _contains_key(source_context, "content"):
        raise ValueError("source context must contain metadata only, not document content")
    if len(_json(source_context).encode("utf-8")) > 256 * 1024:
        raise ValueError("source context metadata exceeds 256 KiB")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _event(conn: sqlite3.Connection, job_id: str, event_type: str, *, from_status: str | None = None,
           to_status: str | None = None, payload: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO job_events(job_id, occurred_at, event_type, from_status, to_status, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, utc_now(), event_type, from_status, to_status, _json(payload or {})),
    )


def create_job(
    db_path: Path,
    request_path: Path,
    source_context_path: Path | None,
    parent_job_id: str | None = None,
) -> dict[str, Any]:
    request = read_json_object(request_path)
    validate_request(request)
    if source_context_path is None:
        raise ValueError("source context file is required for product video tasks")
    source_context = read_json_object(source_context_path)
    if not source_context:
        raise ValueError("source context cannot be empty")
    validate_source_context(request, source_context)
    if parent_job_id:
        _require_job_id(parent_job_id)
    init_db(db_path)
    job_id = generate_job_id()
    now = utc_now()
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if parent_job_id and conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (parent_job_id,)
            ).fetchone() is None:
                raise ValueError("parent job not found")
            conn.execute(
                "INSERT INTO jobs(job_id, parent_job_id, status, request_json, source_context_json, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?)",
                (job_id, parent_job_id, _json(request), _json(source_context), now, now),
            )
            _event(conn, job_id, "created", to_status="queued", payload={
                "storage_backend": "standalone_sqlite",
                "parent_job_id": parent_job_id,
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def _require_job_id(job_id: str) -> None:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid job id")


def read_job(db_path: Path, job_id: str) -> dict[str, Any]:
    _require_job_id(job_id)
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("job not found")
        links = [dict(item) for item in conn.execute(
            "SELECT link_type, value, metadata_json, created_at FROM job_links WHERE job_id = ? ORDER BY link_id",
            (job_id,),
        )]
        approvals = [dict(item) for item in conn.execute(
            "SELECT operation, decision, scope_json, actor, decided_at FROM approvals WHERE job_id = ? ORDER BY approval_id",
            (job_id,),
        )]
        events = [dict(item) for item in conn.execute(
            "SELECT event_id, occurred_at, event_type, from_status, to_status, payload_json "
            "FROM job_events WHERE job_id = ? ORDER BY event_id",
            (job_id,),
        )]
    payload = dict(row)
    payload["request"] = json.loads(payload.pop("request_json"))
    payload["source_context"] = json.loads(payload.pop("source_context_json"))
    payload["directives"] = json.loads(payload.pop("directives_json"))
    for item in links:
        item["metadata"] = json.loads(item.pop("metadata_json"))
    for item in approvals:
        item["scope"] = json.loads(item.pop("scope_json"))
    for item in events:
        item["payload"] = json.loads(item.pop("payload_json"))
    payload.update({"storage_backend": "standalone_sqlite", "links": links, "approvals": approvals, "events": events})
    return payload


def list_jobs(db_path: Path, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    require_existing_db(db_path)
    if status and status not in STATUSES:
        raise ValueError(f"unsupported status: {status}")
    sql = (
        "SELECT j.job_id, j.parent_job_id, j.status, j.stage, j.request_json, j.revision, "
        "j.created_at, j.updated_at, "
        "(SELECT l.value FROM job_links AS l WHERE l.job_id = j.job_id "
        "AND l.link_type = 'chatcut_project_id' ORDER BY l.link_id DESC LIMIT 1) AS chatcut_project_id "
        "FROM jobs AS j"
    )
    params: list[Any] = []
    if status:
        sql += " WHERE j.status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    jobs = []
    for row in rows:
        request = json.loads(row["request_json"])
        jobs.append({
            "job_id": row["job_id"], "parent_job_id": row["parent_job_id"],
            "status": row["status"], "stage": row["stage"], "revision": row["revision"],
            "product_id": request.get("product_id"), "goal": request.get("goal"),
            "duration_seconds": request.get("duration_seconds"),
            "aspect_ratio": request.get("aspect_ratio"),
            "chatcut_project_id": row["chatcut_project_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
    return {"database": str(db_path), "storage_backend": "standalone_sqlite", "jobs": jobs}


def transition(db_path: Path, job_id: str, target: str, expected: str | None, note: str) -> dict[str, Any]:
    _require_job_id(job_id)
    if target not in STATUSES:
        raise ValueError(f"unsupported status: {target}")
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status, revision FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("job not found")
            current = str(row["status"])
            if expected and current != expected:
                raise ValueError(f"status changed: expected {expected}, found {current}")
            if target not in TRANSITIONS.get(current, set()):
                raise ValueError(f"invalid transition: {current} -> {target}")
            now = utc_now()
            updated = conn.execute(
                "UPDATE jobs SET status = ?, revision = revision + 1, updated_at = ? "
                "WHERE job_id = ? AND status = ? AND revision = ?",
                (target, now, job_id, current, row["revision"]),
            )
            if updated.rowcount != 1:
                raise ValueError("concurrent task update detected")
            _event(conn, job_id, "transition", from_status=current, to_status=target, payload={"note": note})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def add_link(db_path: Path, job_id: str, link_type: str, value: str, metadata_path: Path | None = None) -> dict[str, Any]:
    _require_job_id(job_id)
    if not link_type.strip() or not value.strip():
        raise ValueError("link type and value are required")
    metadata = read_json_object(metadata_path)
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise ValueError("job not found")
            inserted = conn.execute(
                "INSERT OR IGNORE INTO job_links(job_id, link_type, value, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, link_type.strip(), value.strip(), _json(metadata), utc_now()),
            )
            if inserted.rowcount == 1:
                conn.execute("UPDATE jobs SET revision = revision + 1, updated_at = ? WHERE job_id = ?", (utc_now(), job_id))
                _event(conn, job_id, "link_added", payload={"link_type": link_type.strip(), "value": value.strip()})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def record_approval(db_path: Path, job_id: str, operation: str, decision: str, scope_path: Path, actor: str) -> dict[str, Any]:
    _require_job_id(job_id)
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if not operation.strip() or not actor.strip():
        raise ValueError("operation and actor are required")
    scope = read_json_object(scope_path)
    if not scope:
        raise ValueError("approval scope cannot be empty")
    required_scope = {"operation", "content", "quantity", "target", "estimated_credits"}
    missing_scope = sorted(required_scope - set(scope))
    if missing_scope:
        raise ValueError(f"approval scope missing fields: {', '.join(missing_scope)}")
    if scope.get("operation") != operation:
        raise ValueError("approval scope operation must match --operation")
    if not isinstance(scope.get("content"), str) or not scope["content"].strip():
        raise ValueError("approval scope content must be a non-empty string")
    if not isinstance(scope.get("target"), str) or not scope["target"].strip():
        raise ValueError("approval scope target must be a non-empty string")
    if not isinstance(scope.get("quantity"), int) or scope["quantity"] <= 0:
        raise ValueError("approval scope quantity must be a positive integer")
    if not isinstance(scope.get("estimated_credits"), (int, float)) or scope["estimated_credits"] < 0:
        raise ValueError("approval scope estimated_credits must be zero or greater")
    scope_hash = hashlib.sha256(_json(scope).encode("utf-8")).hexdigest()
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise ValueError("job not found")
            now = utc_now()
            conn.execute(
                "INSERT INTO approvals(job_id, operation, decision, scope_json, actor, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, operation.strip(), decision, _json(scope), actor.strip(), now),
            )
            conn.execute("UPDATE jobs SET revision = revision + 1, updated_at = ? WHERE job_id = ?", (now, job_id))
            _event(conn, job_id, "approval_recorded", payload={
                "operation": operation.strip(),
                "decision": decision,
                "actor": actor.strip(),
                "actor_is_self_asserted_label": True,
                "scope_sha256": scope_hash,
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def set_cost(db_path: Path, job_id: str, estimated: float | None, actual: float | None, currency: str) -> dict[str, Any]:
    _require_job_id(job_id)
    if estimated is None and actual is None:
        raise ValueError("provide estimated or actual cost")
    if (estimated is not None and estimated < 0) or (actual is not None and actual < 0):
        raise ValueError("cost cannot be negative")
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT estimated_cost, actual_cost FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("job not found")
            next_estimated = estimated if estimated is not None else row["estimated_cost"]
            next_actual = actual if actual is not None else row["actual_cost"]
            now = utc_now()
            conn.execute(
                "UPDATE jobs SET estimated_cost = ?, actual_cost = ?, currency = ?, revision = revision + 1, updated_at = ? WHERE job_id = ?",
                (next_estimated, next_actual, currency.upper(), now, job_id),
            )
            _event(conn, job_id, "cost_updated", payload={"estimated": next_estimated, "actual": next_actual, "currency": currency.upper()})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def set_stage(db_path: Path, job_id: str, stage: str, note: str) -> dict[str, Any]:
    _require_job_id(job_id)
    if stage not in STAGES:
        raise ValueError(f"unsupported stage: {stage}")
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT stage FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("job not found")
            current = str(row["stage"])
            if current != stage:
                now = utc_now()
                conn.execute(
                    "UPDATE jobs SET stage = ?, revision = revision + 1, updated_at = ? WHERE job_id = ?",
                    (stage, now, job_id),
                )
                _event(conn, job_id, "stage_changed", payload={"from": current, "to": stage, "note": note})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def record_directive(db_path: Path, job_id: str, directive_path: Path, actor: str) -> dict[str, Any]:
    _require_job_id(job_id)
    if not actor.strip():
        raise ValueError("actor is required")
    directive = read_json_object(directive_path)
    if not directive:
        raise ValueError("directive cannot be empty")
    encoded = _json(directive).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("directive exceeds 64 KiB")
    require_existing_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT directives_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("job not found")
            now = utc_now()
            directives = json.loads(row["directives_json"])
            directives.append({"at": now, "actor": actor.strip(), "directive": directive})
            conn.execute(
                "UPDATE jobs SET directives_json = ?, revision = revision + 1, updated_at = ? WHERE job_id = ?",
                (_json(directives), now, job_id),
            )
            _event(conn, job_id, "directive_recorded", payload={
                "actor": actor.strip(),
                "directive_sha256": hashlib.sha256(encoded).hexdigest(),
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def adopt_existing(
    db_path: Path,
    request_path: Path,
    source_context_path: Path,
    project_id: str,
    baseline_timeline_id: str,
    actor: str,
    historical_cost_state: str,
) -> dict[str, Any]:
    if not all(value.strip() for value in (project_id, baseline_timeline_id, actor)):
        raise ValueError("project id, baseline timeline id, and actor are required")
    if historical_cost_state not in {"unknown", "known_recorded"}:
        raise ValueError("historical cost state must be unknown or known_recorded")
    request = read_json_object(request_path)
    validate_request(request)
    source_context = read_json_object(source_context_path)
    if not source_context:
        raise ValueError("source context cannot be empty")
    validate_source_context(request, source_context)
    init_db(db_path)
    job_id = generate_job_id()
    with db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now()
            conn.execute(
                "INSERT INTO jobs(job_id, status, request_json, source_context_json, created_at, updated_at) "
                "VALUES (?, 'queued', ?, ?, ?, ?)",
                (job_id, _json(request), _json(source_context), now, now),
            )
            _event(conn, job_id, "created", to_status="queued", payload={
                "storage_backend": "standalone_sqlite",
                "adopt_existing": True,
            })
            for link_type, value in (
                ("chatcut_project_id", project_id.strip()),
                ("baseline_timeline_id", baseline_timeline_id.strip()),
            ):
                conn.execute(
                    "INSERT INTO job_links(job_id, link_type, value, metadata_json, created_at) VALUES (?, ?, ?, '{}', ?)",
                    (job_id, link_type, value, now),
                )
            conn.execute(
                "UPDATE jobs SET stage = 'editing', status = 'awaiting_human', revision = revision + 1, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            _event(conn, job_id, "recovered_from_chatcut", from_status="queued", to_status="awaiting_human", payload={
                "actor": actor.strip(),
                "historical_cost_state": historical_cost_state,
                "history_reconstructed": False,
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_job(db_path, job_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path; defaults to COMPANY_VIDEO_DB_PATH or LocalAppData")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    create = sub.add_parser("create")
    create.add_argument("--request-file", type=Path, required=True)
    create.add_argument("--source-context-file", type=Path, required=True)
    create.add_argument("--parent-job-id")
    adopt = sub.add_parser("adopt-existing")
    adopt.add_argument("--request-file", type=Path, required=True)
    adopt.add_argument("--source-context-file", type=Path, required=True)
    adopt.add_argument("--chatcut-project-id", required=True)
    adopt.add_argument("--baseline-timeline-id", required=True)
    adopt.add_argument("--actor", required=True)
    adopt.add_argument("--historical-cost-state", choices=["unknown", "known_recorded"], default="unknown")
    read = sub.add_parser("read")
    read.add_argument("--job-id", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=sorted(STATUSES))
    listing.add_argument("--limit", type=int, default=50)
    move = sub.add_parser("transition")
    move.add_argument("--job-id", required=True)
    move.add_argument("--status", choices=sorted(STATUSES), required=True)
    move.add_argument("--expected-status")
    move.add_argument("--note", default="")
    stage = sub.add_parser("set-stage")
    stage.add_argument("--job-id", required=True)
    stage.add_argument("--stage", choices=sorted(STAGES), required=True)
    stage.add_argument("--note", default="")
    directive = sub.add_parser("record-directive")
    directive.add_argument("--job-id", required=True)
    directive.add_argument("--directive-file", type=Path, required=True)
    directive.add_argument("--actor", required=True)
    link = sub.add_parser("add-link")
    link.add_argument("--job-id", required=True)
    link.add_argument("--type", required=True)
    link.add_argument("--value", required=True)
    link.add_argument("--metadata-file", type=Path)
    approval = sub.add_parser("record-approval")
    approval.add_argument("--job-id", required=True)
    approval.add_argument("--operation", required=True)
    approval.add_argument("--decision", choices=["approved", "rejected"], required=True)
    approval.add_argument("--scope-file", type=Path, required=True)
    approval.add_argument("--actor", required=True)
    cost = sub.add_parser("set-cost")
    cost.add_argument("--job-id", required=True)
    cost.add_argument("--estimated", type=float)
    cost.add_argument("--actual", type=float)
    cost.add_argument("--currency", default="CNY")
    return parser


def main() -> int:
    configure_console()
    args = build_parser().parse_args()
    db_path = resolve_db_path(args.db)
    if args.command == "init":
        payload = init_db(db_path)
    elif args.command == "create":
        payload = create_job(db_path, args.request_file, args.source_context_file, args.parent_job_id)
    elif args.command == "adopt-existing":
        payload = adopt_existing(
            db_path,
            args.request_file,
            args.source_context_file,
            args.chatcut_project_id,
            args.baseline_timeline_id,
            args.actor,
            args.historical_cost_state,
        )
    elif args.command == "read":
        payload = read_job(db_path, args.job_id)
    elif args.command == "list":
        payload = list_jobs(db_path, args.status, args.limit)
    elif args.command == "transition":
        payload = transition(db_path, args.job_id, args.status, args.expected_status, args.note)
    elif args.command == "set-stage":
        payload = set_stage(db_path, args.job_id, args.stage, args.note)
    elif args.command == "record-directive":
        payload = record_directive(db_path, args.job_id, args.directive_file, args.actor)
    elif args.command == "add-link":
        payload = add_link(db_path, args.job_id, args.type, args.value, args.metadata_file)
    elif args.command == "record-approval":
        payload = record_approval(db_path, args.job_id, args.operation, args.decision, args.scope_file, args.actor)
    else:
        payload = set_cost(db_path, args.job_id, args.estimated, args.actual, args.currency)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
