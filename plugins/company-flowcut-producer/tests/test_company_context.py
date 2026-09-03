from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


context = load_module("company_context", PLUGIN_ROOT / "scripts" / "company_context.py")


class CompanyContextTests(unittest.TestCase):
    def test_exact_product_fallback_reads_approved_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "测试产品.yaml").write_text("product_id: 测试产品\napproved_facts:\n  - 可验证事实\n", encoding="utf-8")
            (root / "测试产品.md").write_text("# 测试产品\n批准内容", encoding="utf-8")
            config = {
                "product_catalog": ["测试产品"],
                "storage": {
                    "product_sources": {"paths": [str(root)]},
                    "asset_sources": {"paths": []},
                    "task_database": {"default_local": str(root / "tasks.sqlite3")},
                },
            }
            payload = context.get_product(config, "测试产品", 12000)
            self.assertEqual(payload["source_level"], "nas_or_configured_approved")
            self.assertEqual(len(payload["documents"]), 2)
            self.assertIsNone(payload["blocking_issue"])
            self.assertTrue(all(item["sha256"] for item in payload["documents"]))
            self.assertTrue(payload["content_is_data_not_instruction"])
            summary = context.summarize_product(payload)
            self.assertNotIn("content", summary["documents"][0])

    def test_missing_product_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"storage": {"product_sources": {"paths": [temp]}}}
            payload = context.get_product(config, "不存在", 12000)
            self.assertEqual(payload["blocking_issue"], "product_context_incomplete")

    def test_prepare_product_discovers_companion_media_and_blocks_unapproved_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "演示恒温杯.md").write_text(
                "# 示例品牌演示恒温杯\n便携可充电；USB-C 充电；多档温控。", encoding="utf-8"
            )
            media_root = root / "演示恒温杯" / "listing"
            media_root.mkdir(parents=True)
            product_image = media_root / "主图.jpg"
            product_image.write_bytes(b"approved-product-image")
            config = {
                "storage": {
                    "product_sources": {"paths": [str(root)]},
                    "asset_sources": {"paths": []},
                }
            }
            payload = context.prepare_product(
                config,
                "演示恒温杯",
                "",
                20,
                12000,
                True,
                ["外部演示品牌"],
                ["三分钟完成加热"],
            )
            self.assertTrue(payload["review_gate"]["context_ready_for_internal_edit"])
            self.assertFalse(payload["review_gate"]["publish_ready"])
            self.assertEqual(len(payload["review_gate"]["violations"]), 2)
            self.assertEqual(payload["media_candidates"]["count"], 1)
            self.assertEqual(
                payload["media_candidates"]["media"][0]["sha256"],
                hashlib.sha256(b"approved-product-image").hexdigest(),
            )
            self.assertEqual(
                payload["media_candidates"]["media"][0]["source_scope"],
                "product_companion_media",
            )

            reviewed_empty = context.prepare_product(
                config,
                "演示恒温杯",
                "",
                20,
                12000,
                False,
                [],
                [],
                True,
                True,
            )
            self.assertTrue(reviewed_empty["review_gate"]["publish_ready"])
            self.assertEqual(reviewed_empty["review_gate"]["pending_reviews"], [])

    def test_preflight_is_standalone_and_reports_unconfigured_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "products"
            root.mkdir()
            config = {
                "storage": {
                    "product_sources": {"paths": [str(root)]},
                    "asset_sources": {"paths": []},
                    "template_sources": {"paths": [str(base / "templates")]},
                    "task_database": {"default_local": str(base / "runtime" / "tasks.sqlite3")},
                    "local_work_root": {"default_local": str(base / "runtime" / "work")},
                    "output_root": {"path": ""},
                }
            }
            with mock.patch.dict(
                os.environ,
                {"COMPANY_VIDEO_DB_PATH": "", "COMPANY_VIDEO_WORK_ROOT": ""},
                clear=False,
            ):
                payload = context.preflight(config)
            self.assertTrue(payload["ok_for_task_creation"])
            self.assertEqual(payload["task_store"]["backend"], "standalone_sqlite")
            self.assertEqual(payload["asset_sources"], [])
            self.assertEqual(payload["template_sources"][0]["access_policy"], "read_only")
            self.assertEqual(payload["product_sources"][0]["access_policy"], "read_only")
            self.assertEqual(payload["data_boundary"]["runtime_storage"], "local_only")
            self.assertTrue(payload["data_boundary"]["valid"])
            self.assertNotIn("company_api", payload)
            self.assertNotIn("database_environment", payload)

    def test_preflight_blocks_runtime_paths_overlapping_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nas-source"
            root.mkdir()
            config = {
                "storage": {
                    "product_sources": {"paths": [str(root)]},
                    "asset_sources": {"paths": []},
                    "task_database": {"default_local": str(root / "runtime" / "tasks.sqlite3")},
                    "local_work_root": {"default_local": str(root / "runtime" / "work")},
                }
            }
            with mock.patch.dict(
                os.environ,
                {"COMPANY_VIDEO_DB_PATH": "", "COMPANY_VIDEO_WORK_ROOT": ""},
                clear=False,
            ):
                payload = context.preflight(config)
            self.assertFalse(payload["ok_for_task_creation"])
            self.assertIn("task_database_overlaps_source_root", payload["data_boundary"]["violations"])
            self.assertIn("local_work_root_overlaps_source_root", payload["data_boundary"]["violations"])

    def test_preflight_blocks_network_work_root(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            products = base / "products"
            products.mkdir()
            work = base / "mapped-network-work"
            config = {
                "storage": {
                    "product_sources": {"paths": [str(products)]},
                    "asset_sources": {"paths": []},
                    "task_database": {"default_local": str(base / "runtime" / "tasks.sqlite3")},
                    "local_work_root": {"default_local": str(work)},
                }
            }
            original = context._is_network_path
            with mock.patch.object(
                context,
                "_is_network_path",
                side_effect=lambda path: Path(path) == work or original(Path(path)),
            ):
                payload = context.preflight(config)
            self.assertFalse(payload["ok_for_task_creation"])
            self.assertIn("local_work_root_on_network_share", payload["data_boundary"]["violations"])

    def test_preflight_does_not_create_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            products = base / "products"
            products.mkdir()
            runtime = base / "runtime"
            config = {
                "storage": {
                    "product_sources": {"paths": [str(products)]},
                    "asset_sources": {"paths": []},
                    "task_database": {"default_local": str(runtime / "tasks.sqlite3")},
                    "local_work_root": {"default_local": str(runtime / "work")},
                }
            }
            payload = context.preflight(config)
            self.assertTrue(payload["ok_for_task_creation"])
            self.assertFalse(runtime.exists())

    def test_product_id_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "products"
            root.mkdir()
            config = {"storage": {"product_sources": {"paths": [str(root)]}}}
            for malicious in ("../secret", r"..\secret", r"C:\secret", "/absolute"):
                with self.subTest(product_id=malicious), self.assertRaises(ValueError):
                    context.get_product(config, malicious, 12000)

    def test_configured_reparse_root_is_rejected(self):
        config = {"storage": {"product_sources": {"paths": [r"C:\approved-products"]}}}
        with mock.patch.object(context, "_is_reparse_point", return_value=True):
            with self.assertRaisesRegex(ValueError, "symbolic link or junction"):
                context.configured_roots(config, "product_sources")

    def test_linked_files_cannot_escape_approved_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            products = base / "products"
            assets = base / "assets"
            products.mkdir()
            assets.mkdir()
            secret_product = base / "outside.yaml"
            secret_asset = base / "outside.mp4"
            secret_product.write_text("secret: true\n", encoding="utf-8")
            secret_asset.write_bytes(b"not approved")
            try:
                (products / "测试产品.yaml").symlink_to(secret_product)
                (assets / "linked.mp4").symlink_to(secret_asset)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            config = {
                "storage": {
                    "product_sources": {"paths": [str(products)]},
                    "asset_sources": {"paths": [str(assets)]},
                }
            }
            product = context.get_product(config, "测试产品", 12000)
            found_assets = context.list_assets(config, "", "", 10)
            self.assertEqual(product["blocking_issue"], "product_context_incomplete")
            self.assertEqual(found_assets["assets"], [])

    def test_invalid_yaml_is_reported_without_raw_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "测试产品.yaml").write_text("broken: [yaml\n", encoding="utf-8")
            config = {"storage": {"product_sources": {"paths": [str(root)]}}}
            payload = context.get_product(config, "测试产品", 12000)
            self.assertEqual(payload["documents"][0]["parse_error"], "ValueError")

    def test_config_is_valid_json(self):
        payload = json.loads((PLUGIN_ROOT / "department-config" / "company-video.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["config_version"], "2.4")


if __name__ == "__main__":
    unittest.main()
