from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_store", PLUGIN_ROOT / "scripts" / "task_store.py")
task_store = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(task_store)


def write_source_context(root: Path, product_id: str = "示例产品") -> Path:
    path = root / "source-context.json"
    path.write_text(json.dumps({
        "product_id": product_id,
        "source_level": "nas_or_configured_approved",
        "documents": [{
            "path": "X:/approved/example.md",
            "sha256": "a" * 64,
            "modified_at": "2026-09-02T00:00:00+00:00",
        }],
        "blocking_issue": None,
        "content_stored": False,
    }, ensure_ascii=False), encoding="utf-8")
    return path


class TaskStoreTests(unittest.TestCase):
    def test_create_transition_link_approval_cost_directive_and_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            source = write_source_context(root)
            scope = root / "scope.json"
            scope.write_text(json.dumps({
                "operation": "paid_generation",
                "content": "one test voice segment",
                "quantity": 1,
                "target": "test timeline",
                "estimated_credits": 20,
            }), encoding="utf-8")
            directive = root / "directive.json"
            directive.write_text(json.dumps({
                "variants": ["A", "B", "C"],
                "max_incremental_credits": 0,
                "export_policy": "not_requested",
            }), encoding="utf-8")
            created = task_store.create_job(db, PLUGIN_ROOT / "templates" / "video-job.example.json", source)
            running = task_store.transition(db, created["job_id"], "running", "queued", "test")
            staged = task_store.set_stage(db, created["job_id"], "editing", "start variants")
            linked = task_store.add_link(db, created["job_id"], "chatcut_project_id", "project-test")
            duplicate = task_store.add_link(db, created["job_id"], "chatcut_project_id", "project-test")
            approved = task_store.record_approval(db, created["job_id"], "paid_generation", "approved", scope, "tester")
            directed = task_store.record_directive(db, created["job_id"], directive, "tester")
            costed = task_store.set_cost(db, created["job_id"], 20.0, 18.0, "CREDITS")
            self.assertEqual(running["status"], "running")
            self.assertEqual(staged["stage"], "editing")
            self.assertEqual(linked["links"][0]["value"], "project-test")
            self.assertEqual(duplicate["revision"], linked["revision"])
            self.assertEqual(approved["approvals"][0]["decision"], "approved")
            self.assertEqual(directed["directives"][0]["directive"]["max_incremental_credits"], 0)
            self.assertEqual(costed["actual_cost"], 18.0)

    def test_invalid_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            created = task_store.create_job(
                db, PLUGIN_ROOT / "templates" / "video-job.example.json", write_source_context(root)
            )
            with self.assertRaises(ValueError):
                task_store.transition(db, created["job_id"], "succeeded", "queued", "skip")

    def test_database_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            created = task_store.create_job(
                db, PLUGIN_ROOT / "templates" / "video-job.example.json", write_source_context(root)
            )
            found = task_store.read_job(db, created["job_id"])
            listing = task_store.list_jobs(db)
            self.assertEqual(found["request"]["product_id"], "示例产品")
            self.assertEqual(listing["jobs"][0]["job_id"], created["job_id"])
            self.assertEqual(listing["jobs"][0]["duration_seconds"], 45)
            self.assertEqual(listing["jobs"][0]["aspect_ratio"], "9:16")
            self.assertIsNone(listing["jobs"][0]["chatcut_project_id"])

    def test_legacy_database_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "legacy.sqlite3"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE jobs ("
                    "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, request_json TEXT NOT NULL, "
                    "source_context_json TEXT NOT NULL DEFAULT '{}', currency TEXT NOT NULL DEFAULT 'CNY', "
                    "estimated_cost REAL, actual_cost REAL, revision INTEGER NOT NULL DEFAULT 0, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.commit()
            finally:
                conn.close()
            result = task_store.init_db(db)
            conn = sqlite3.connect(db)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                migration = conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(result["schema_version"], task_store.CURRENT_SCHEMA_VERSION)
            self.assertEqual(result["migrations_applied"], 3)
            self.assertTrue({"parent_job_id", "stage", "directives_json"}.issubset(columns))
            self.assertEqual(migration[0], task_store.CURRENT_SCHEMA_VERSION)

    def test_legacy_database_requires_explicit_migration_before_list(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "legacy-list.sqlite3"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE jobs ("
                    "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, request_json TEXT NOT NULL, "
                    "source_context_json TEXT NOT NULL DEFAULT '{}', currency TEXT NOT NULL DEFAULT 'CNY', "
                    "estimated_cost REAL, actual_cost REAL, revision INTEGER NOT NULL DEFAULT 0, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.commit()
            finally:
                conn.close()
            before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
            before_mtime = db.stat().st_mtime_ns
            with self.assertRaisesRegex(ValueError, "run init explicitly"):
                task_store.list_jobs(db)
            conn = sqlite3.connect(db)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            finally:
                conn.close()
            self.assertFalse({"parent_job_id", "stage", "directives_json"}.issubset(columns))
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(db.stat().st_mtime_ns, before_mtime)

    def test_read_and_list_do_not_modify_database_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            created = task_store.create_job(
                db, PLUGIN_ROOT / "templates" / "video-job.example.json", write_source_context(root)
            )
            before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
            before_mtime = db.stat().st_mtime_ns
            task_store.read_job(db, created["job_id"])
            task_store.list_jobs(db)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(db.stat().st_mtime_ns, before_mtime)

    def test_chatcut_link_aliases_are_normalized_and_discoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            created = task_store.create_job(
                db, PLUGIN_ROOT / "templates" / "video-job.example.json", write_source_context(root)
            )
            linked = task_store.add_link(db, created["job_id"], "chatcut_project", "project-alias")
            listing = task_store.list_jobs(db)
            self.assertEqual(linked["links"][0]["link_type"], "chatcut_project_id")
            self.assertEqual(listing["jobs"][0]["chatcut_project_id"], "project-alias")

    def test_future_database_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "future.sqlite3"
            task_store.init_db(db)
            conn = sqlite3.connect(db)
            try:
                conn.execute("PRAGMA user_version = 99")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "newer than supported"):
                task_store.init_db(db)

    def test_missing_database_is_not_created_by_read(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "missing.sqlite3"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                task_store.list_jobs(db)
            self.assertFalse(db.exists())

    def test_invalid_request_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = json.loads((PLUGIN_ROOT / "templates" / "video-job.example.json").read_text(encoding="utf-8"))
            request.update({"aspect_ratio": "BAD", "duration_seconds": 999999, "output_count": 999999})
            request_path = root / "invalid.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaises(ValueError):
                task_store.create_job(root / "tasks.sqlite3", request_path, write_source_context(root))

    def test_source_context_must_not_store_document_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = write_source_context(root)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["documents"][0]["content"] = "sensitive source text"
            source.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata only"):
                task_store.create_job(root / "tasks.sqlite3", PLUGIN_ROOT / "templates" / "video-job.example.json", source)

    def test_network_database_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "local disk"):
            task_store.resolve_db_path(r"\\server\share\tasks.sqlite3")

    def test_database_path_inside_source_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source_root = Path(temp) / "products"
            source_root.mkdir()
            with mock.patch.dict(
                os.environ,
                {"COMPANY_VIDEO_PRODUCT_ROOTS": str(source_root), "COMPANY_VIDEO_ASSET_ROOTS": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "source root"):
                    task_store.resolve_db_path(str(source_root / "tasks.sqlite3"))

    def test_adopt_existing_and_create_derived_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "tasks.sqlite3"
            source = write_source_context(root)
            adopted = task_store.adopt_existing(
                db,
                PLUGIN_ROOT / "templates" / "video-job.example.json",
                source,
                "project-existing",
                "timeline-baseline",
                "tester",
                "unknown",
            )
            derived = task_store.create_job(
                db,
                PLUGIN_ROOT / "templates" / "video-job.example.json",
                source,
                adopted["job_id"],
            )
            self.assertEqual(adopted["status"], "awaiting_human")
            self.assertEqual(adopted["stage"], "editing")
            self.assertEqual(derived["parent_job_id"], adopted["job_id"])


if __name__ == "__main__":
    unittest.main()
