from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


service = load_module("template_service", PLUGIN_ROOT / "scripts" / "template_service.py")


class TemplateServiceTests(unittest.TestCase):
    def _asset(
        self,
        path: Path,
        asset_id: str,
        categories: list[str],
        tags: list[str],
        roles: list[str],
    ) -> dict:
        return {
            "asset_id": asset_id,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "media_type": "image",
            "size_bytes": path.stat().st_size,
            "source_scope": "product_companion_media",
            "product_id": "示例产品",
            "categories": categories,
            "tags": tags,
            "allowed_roles": roles,
            "orientation": "portrait",
            "duration_seconds": None,
            "usable_ranges": [],
            "classification_status": "approved",
            "authorization_status": "approved",
            "brand_review_status": "not_applicable",
            "claim_review_status": "not_applicable",
            "quality_status": "approved",
            "observed_brands": [],
            "observed_claims": [],
            "suggestion_basis": "visually_reviewed",
            "notes": "test fixture",
        }

    def _fixture(self, root: Path) -> tuple[dict, dict, dict, Path]:
        products = root / "products"
        templates = root / "templates"
        products.mkdir()
        templates.mkdir()
        (products / "示例产品.md").write_text(
            "# 示例产品\n便携设计；批准功能；真实使用场景。", encoding="utf-8"
        )
        media = products / "示例产品"
        media.mkdir()
        assets = []
        asset_specs = [
            ("主图-1.jpg", "A-HERO-1", ["product_hero"], ["product", "portable"], ["hook", "product"]),
            ("主图-2.jpg", "A-HERO-2", ["product_hero"], ["product", "portable"], ["hook", "product"]),
            ("功能-1.jpg", "A-FEATURE-1", ["feature_proof"], ["approved-feature"], ["feature", "proof"]),
            ("功能-2.jpg", "A-FEATURE-2", ["feature_proof"], ["approved-feature"], ["feature", "proof"]),
            ("场景-1.jpg", "A-SCENE-1", ["usage_scene"], ["approved-scene", "lifestyle"], ["scene", "cta"]),
            ("场景-2.jpg", "A-SCENE-2", ["usage_scene"], ["approved-scene", "lifestyle"], ["scene", "cta"]),
        ]
        for filename, asset_id, categories, tags, roles in asset_specs:
            path = media / filename
            path.write_bytes(asset_id.encode("utf-8"))
            assets.append(self._asset(path, asset_id, categories, tags, roles))
        template = {
            "template_version": "1.0",
            "template_id": "QUICK-15",
            "display_name": "15 秒产品快剪",
            "revision": 3,
            "status": "approved",
            "product_scope": ["*"],
            "match": {
                "platforms": ["抖音"],
                "aspect_ratios": ["9:16"],
                "languages": ["zh-CN"],
                "duration_seconds": 15,
                "tags": ["quick-remix", "product"],
            },
            "max_outputs": 20,
            "max_asset_uses": 1,
            "shots": [
                {
                    "slot_id": "S01", "line_slot": "hook", "role": "hook", "duration_seconds": 5,
                    "required_categories": ["product_hero"], "required_tags": ["product"],
                    "preferred_tags": ["portable"], "allowed_media_types": ["image", "video"],
                    "required_orientation": "any", "preferred_orientation": "portrait",
                    "fit_mode": "contain", "allow_generic": False,
                },
                {
                    "slot_id": "S02", "line_slot": "feature", "role": "feature", "duration_seconds": 5,
                    "required_categories": ["feature_proof"], "required_tags": ["approved-feature"],
                    "preferred_tags": [], "allowed_media_types": ["image", "video"],
                    "required_orientation": "any", "preferred_orientation": "portrait",
                    "fit_mode": "cover_blur_background", "allow_generic": False,
                },
                {
                    "slot_id": "S03", "line_slot": "cta", "role": "cta", "duration_seconds": 5,
                    "required_categories": ["usage_scene"], "required_tags": ["approved-scene"],
                    "preferred_tags": ["lifestyle"], "allowed_media_types": ["image", "video"],
                    "required_orientation": "any", "preferred_orientation": "portrait",
                    "fit_mode": "cover", "allow_generic": False,
                },
            ],
            "variants": [
                {"variant_id": "ABC", "name": "产品功能场景", "shot_order": ["S01", "S02", "S03"]},
                {"variant_id": "ACB", "name": "产品场景功能", "shot_order": ["S01", "S03", "S02"]},
            ],
            "notes": "approved test template",
        }
        template_path = templates / "quick-15.json"
        template_path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        config = {
            "storage": {
                "product_sources": {"paths": [str(products)]},
                "asset_sources": {"paths": []},
                "template_sources": {"paths": [str(templates)]},
            }
        }
        catalog = {
            "catalog_version": "1.0",
            "catalog_id": "CAT-BATCH-TEST",
            "product_id": "示例产品",
            "assets": assets,
        }
        request = {
            "request_version": "1.0",
            "request_id": "REQ-BATCH-TEST",
            "product_id": "示例产品",
            "platform": "抖音",
            "aspect_ratio": "9:16",
            "duration_seconds": 15,
            "language": "zh-CN",
            "output_count": 2,
            "template_id": None,
            "required_template_tags": ["quick-remix", "product"],
            "approved_copy": {
                "hook": {"text": "便携出行", "fact_refs": ["便携设计"]},
                "feature": {"text": "批准功能展示", "fact_refs": ["批准功能"]},
                "cta": {"text": "真实场景收尾", "fact_refs": []},
            },
            "chatcut_layout": "named_timelines",
            "preserve_existing_timelines": True,
            "timeline_name_prefix": "示例产品-快剪",
            "max_incremental_credits": 0,
            "export_policy": "not_requested",
        }
        return config, catalog, request, template_path

    def test_approved_template_builds_deterministic_multi_output_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, request, template_path = self._fixture(Path(temp))
            first = service.plan_batch(config, request, catalog)
            second = service.plan_batch(config, request, catalog)
            self.assertTrue(first["ready_for_chatcut"])
            self.assertEqual(first["batch_id"], second["batch_id"])
            self.assertEqual(len(first["jobs"]), 2)
            self.assertEqual(first["jobs"][0]["variant_id"], "ABC")
            self.assertEqual(first["jobs"][1]["variant_id"], "ACB")
            self.assertEqual(first["jobs"][0]["chatcut_handoff"]["placements"][0]["asset_id"], "A-HERO-1")
            self.assertEqual(first["jobs"][1]["chatcut_handoff"]["placements"][0]["asset_id"], "A-HERO-2")
            self.assertEqual(len(set(job["selection_manifest_sha256"] for job in first["jobs"])), 2)
            self.assertEqual(len(first["chatcut_batch_handoff"]["shared_import_paths"]), 6)
            self.assertTrue(first["chatcut_batch_handoff"]["preserve_existing_timelines"])
            self.assertEqual(first["estimated_incremental_credits"], 0)
            self.assertFalse(first["paid_generation_authorized"])
            self.assertEqual(first["template_selection"]["sha256"], hashlib.sha256(template_path.read_bytes()).hexdigest())

    def test_published_examples_match_runtime_contract(self):
        template_path = PLUGIN_ROOT / "templates" / "remix-template.example.json"
        request_path = PLUGIN_ROOT / "templates" / "batch-remix-request.example.json"
        template = service._normalize_template(
            json.loads(template_path.read_text(encoding="utf-8")), template_path
        )
        request = service._normalize_request(json.loads(request_path.read_text(encoding="utf-8")))
        self.assertEqual(template["status"], "draft")
        self.assertEqual(template["template_id"], "PRODUCT-QUICK-REMIX-15S")
        self.assertEqual(request["output_count"], 6)
        self.assertTrue(request["preserve_existing_timelines"])

    def test_draft_template_is_never_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, request, template_path = self._fixture(Path(temp))
            payload = json.loads(template_path.read_text(encoding="utf-8"))
            payload["status"] = "draft"
            template_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = service.plan_batch(config, request, catalog)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["template_resolution"]["blocking_issue"], "no_approved_templates")
            self.assertEqual(result["chatcut_batch_handoff"]["outputs"], [])

    def test_missing_approved_copy_slot_blocks_every_output(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, request, _ = self._fixture(Path(temp))
            del request["approved_copy"]["feature"]
            result = service.plan_batch(config, request, catalog)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["jobs"], [])
            self.assertEqual(len(result["blockers"]), 2)
            self.assertTrue(all(item["type"] == "template_render_failed" for item in result["blockers"]))

    def test_request_constraints_must_match_template(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, request, _ = self._fixture(Path(temp))
            request["duration_seconds"] = 30
            result = service.plan_batch(config, request, catalog)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["template_resolution"]["blocking_issue"], "no_template_matches_request")
            self.assertEqual(result["template_resolution"]["rejection_reason_counts"]["duration_mismatch"], 1)

    def test_unconfigured_template_root_blocks_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, request, _ = self._fixture(Path(temp))
            config["storage"]["template_sources"]["paths"] = []
            result = service.plan_batch(config, request, catalog)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["template_resolution"]["blocking_issue"], "template_sources_unconfigured")


if __name__ == "__main__":
    unittest.main()
