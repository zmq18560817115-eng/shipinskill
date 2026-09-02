from __future__ import annotations

import hashlib
import importlib.util
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


planner = load_module("material_planner", PLUGIN_ROOT / "scripts" / "material_planner.py")


class MaterialPlannerTests(unittest.TestCase):
    def _asset(
        self,
        path: Path,
        asset_id: str,
        categories: list[str],
        tags: list[str],
        roles: list[str],
        *,
        product_id: str | None = "示例产品",
    ) -> dict:
        return {
            "asset_id": asset_id,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "media_type": "image",
            "size_bytes": path.stat().st_size,
            "source_scope": "product_companion_media",
            "product_id": product_id,
            "categories": categories,
            "tags": tags,
            "allowed_roles": roles,
            "orientation": "portrait",
            "duration_seconds": None,
            "usable_ranges": [],
            "classification_status": "approved",
            "authorization_status": "approved",
            "brand_review_status": "approved",
            "claim_review_status": "not_applicable",
            "quality_status": "approved",
            "observed_brands": ["批准品牌"],
            "observed_claims": [],
            "suggestion_basis": "visually_reviewed",
            "notes": "test fixture",
        }

    def _fixture(self, root: Path) -> tuple[dict, dict, dict]:
        (root / "示例产品.md").write_text("# 示例产品\n批准品牌；便携设计；USB-C 充电。", encoding="utf-8")
        media = root / "示例产品"
        media.mkdir()
        hero = media / "主图.jpg"
        feature = media / "充电细节.jpg"
        reference = media / "竞品参考.jpg"
        hero.write_bytes(b"hero")
        feature.write_bytes(b"feature")
        reference.write_bytes(b"reference")
        config = {"storage": {"product_sources": {"paths": [str(root)]}, "asset_sources": {"paths": []}}}
        catalog = {
            "catalog_version": "1.0",
            "catalog_id": "CAT-TEST",
            "product_id": "示例产品",
            "assets": [
                self._asset(hero, "A-HERO", ["product_hero"], ["product", "portable"], ["hook", "product"]),
                self._asset(feature, "A-FEATURE", ["feature_proof"], ["usb-c", "charging"], ["feature", "proof"]),
                self._asset(reference, "A-REFERENCE", ["reference_only"], ["product"], []),
            ],
        }
        script = {
            "script_version": "1.0",
            "script_id": "SCRIPT-TEST",
            "product_id": "示例产品",
            "aspect_ratio": "9:16",
            "target_duration_seconds": 6,
            "max_asset_uses": 1,
            "shots": [
                {
                    "shot_id": "S01",
                    "order": 1,
                    "line": "便携出行",
                    "role": "hook",
                    "duration_seconds": 3,
                    "required_categories": ["product_hero"],
                    "required_tags": ["product"],
                    "preferred_tags": ["portable"],
                    "allowed_media_types": ["image", "video"],
                    "required_orientation": "any",
                    "preferred_orientation": "portrait",
                    "fit_mode": "contain",
                    "allow_generic": False,
                    "fact_refs": ["便携设计"],
                },
                {
                    "shot_id": "S02",
                    "order": 2,
                    "line": "USB-C 充电",
                    "role": "feature",
                    "duration_seconds": 3,
                    "required_categories": ["feature_proof"],
                    "required_tags": ["usb-c"],
                    "preferred_tags": ["charging"],
                    "allowed_media_types": ["image", "video"],
                    "required_orientation": "any",
                    "preferred_orientation": "portrait",
                    "fit_mode": "cover_blur_background",
                    "allow_generic": False,
                    "fact_refs": ["USB-C 充电"],
                },
            ],
        }
        return config, catalog, script

    def test_seed_catalog_suggests_categories_but_requires_review(self):
        prepared = {
            "product": {"product_id": "示例产品"},
            "source_summary": {"product_id": "示例产品"},
            "media_candidates": {
                "product_id": "示例产品",
                "media": [
                    {
                        "path": "C:\\approved-media\\示例产品\\主图.jpg",
                        "kind": "image",
                        "sha256": "a" * 64,
                        "size_bytes": 10,
                        "source_scope": "product_companion_media",
                    }
                ],
            },
        }
        catalog = planner.seed_catalog(prepared, "CAT-SEED")
        self.assertEqual(catalog["assets"][0]["categories"], ["product_hero"])
        self.assertEqual(catalog["assets"][0]["classification_status"], "needs_review")
        self.assertEqual(catalog["chatcut_eligible_assets"], 0)
        self.assertTrue(catalog["review_required"])

    def test_match_script_selects_only_exact_reviewed_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            result = planner.match_script(config, catalog, script)
            self.assertTrue(result["ready_for_chatcut"])
            self.assertEqual([item["asset_id"] for item in result["selections"]], ["A-HERO", "A-FEATURE"])
            self.assertEqual([item["timeline_start_seconds"] for item in result["selections"]], [0.0, 3.0])
            self.assertEqual(len(result["chatcut_handoff"]["import_paths"]), 2)
            self.assertEqual(result["rejected_assets"][0]["asset_id"], "A-REFERENCE")
            self.assertIn("reference_only_asset", result["rejected_assets"][0]["reasons"])

    def test_unresolved_shot_blocks_chatcut_import(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            script["shots"][1]["required_tags"] = ["sterilization"]
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["chatcut_handoff"]["import_paths"], [])
            self.assertEqual(result["unresolved_shots"][0]["shot_id"], "S02")
            self.assertIn("required_tag_missing", result["unresolved_shots"][0]["rejection_reason_counts"])

    def test_unapproved_script_fact_blocks_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            script["shots"][0]["fact_refs"] = ["行业第一"]
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            self.assertEqual(result["chatcut_handoff"]["placements"], [])
            self.assertIn("unapproved_script_fact_refs", [item["type"] for item in result["blockers"]])

    def test_hash_mismatch_rejects_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            catalog["assets"][0]["sha256"] = "0" * 64
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            rejected = {item["asset_id"]: item["reasons"] for item in result["rejected_assets"]}
            self.assertIn("sha256_mismatch", rejected["A-HERO"])

    def test_unapproved_catalog_brand_rejects_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            catalog["assets"][0]["observed_brands"] = ["未批准品牌"]
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            rejected = {item["asset_id"]: item["reasons"] for item in result["rejected_assets"]}
            self.assertIn("observed_brand_not_in_approved_documents", rejected["A-HERO"])

    def test_unreviewed_asset_cannot_enter_chatcut_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            catalog["assets"][0]["classification_status"] = "needs_review"
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            rejected = {item["asset_id"]: item["reasons"] for item in result["rejected_assets"]}
            self.assertIn("classification_not_approved", rejected["A-HERO"])

    def test_duplicate_asset_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config, catalog, script = self._fixture(Path(temp))
            duplicate = dict(catalog["assets"][0])
            duplicate["asset_id"] = "A-HERO-DUPLICATE"
            catalog["assets"].append(duplicate)
            result = planner.match_script(config, catalog, script)
            self.assertTrue(result["ready_for_chatcut"])
            rejected = {item["asset_id"]: item["reasons"] for item in result["rejected_assets"]}
            self.assertIn("duplicate_asset_path", rejected["A-HERO-DUPLICATE"])

    def test_product_companion_scope_requires_exact_product_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, catalog, script = self._fixture(root)
            misplaced = root / "misplaced.jpg"
            misplaced.write_bytes(b"misplaced")
            catalog["assets"][0]["path"] = str(misplaced)
            catalog["assets"][0]["sha256"] = hashlib.sha256(misplaced.read_bytes()).hexdigest()
            result = planner.match_script(config, catalog, script)
            self.assertFalse(result["ready_for_chatcut"])
            rejected = {item["asset_id"]: item["reasons"] for item in result["rejected_assets"]}
            self.assertIn("source_scope_path_mismatch", rejected["A-HERO"])


if __name__ == "__main__":
    unittest.main()
