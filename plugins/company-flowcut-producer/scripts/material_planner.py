#!/usr/bin/env python3
"""Build a reviewed material catalog and deterministically match it to a shot script."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import company_context as context  # noqa: E402


DEFAULT_CONFIG = context.DEFAULT_CONFIG
CATALOG_VERSION = "1.0"
SCRIPT_VERSION = "1.0"
MAX_ASSETS = 500
MAX_SHOTS = 100
MEDIA_TYPES = {"image", "video", "audio"}
ORIENTATIONS = {"portrait", "landscape", "square", "unknown", "any"}
FIT_MODES = {"contain", "cover", "cover_blur_background"}
ROLES = {"hook", "problem", "product", "feature", "proof", "scene", "cta", "transition", "audio"}
CATEGORIES = {
    "product_hero",
    "product_detail",
    "feature_proof",
    "usage_scene",
    "problem_context",
    "lifestyle",
    "presenter",
    "reference_only",
    "audio_music",
    "audio_voice",
    "audio_sfx",
    "transition",
    "uncategorized",
}
APPROVED_REVIEW = {"approved", "not_applicable"}
CATEGORY_ROLES = {
    "product_hero": {"hook", "product", "cta"},
    "product_detail": {"product", "feature", "proof"},
    "feature_proof": {"feature", "proof"},
    "usage_scene": {"hook", "problem", "scene", "cta"},
    "problem_context": {"hook", "problem"},
    "lifestyle": {"hook", "scene", "cta"},
    "presenter": {"hook", "product", "feature", "cta"},
    "reference_only": set(),
    "audio_music": {"audio"},
    "audio_voice": {"audio"},
    "audio_sfx": {"audio", "transition"},
    "transition": {"transition"},
    "uncategorized": set(),
}
CATEGORY_RULES = [
    ("reference_only", ("对标", "竞品", "benchmark", "reference", "competitor")),
    ("presenter", ("人像", "人物", "口播", "presenter", "avatar", "portrait")),
    ("usage_scene", ("场景", "使用", "旅行", "出行", "车内", "户外", "scene", "usage", "travel")),
    ("problem_context", ("痛点", "问题", "等待", "problem", "pain")),
    ("product_hero", ("主图", "白底", "hero", "packshot", "product-main")),
    ("feature_proof", ("功能", "架构", "效率", "安全", "防水", "防漏", "证明", "feature", "proof")),
    ("product_detail", ("细节", "特写", "结构", "detail", "closeup", "close-up")),
    ("transition", ("转场", "transition")),
]


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def read_json_object(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_asset_id(sha256: str | None, path: str) -> str:
    token = sha256 or hashlib.sha256(path.encode("utf-8")).hexdigest()
    return f"AST-{token[:12].upper()}"


def _suggest_categories(path: str, media_type: str) -> list[str]:
    searchable = path.casefold()
    if media_type == "audio":
        if any(token in searchable for token in ("配音", "旁白", "voice", "narration")):
            return ["audio_voice"]
        if any(token in searchable for token in ("音效", "sfx", "sound-effect")):
            return ["audio_sfx"]
        return ["audio_music"]
    result = [category for category, tokens in CATEGORY_RULES if any(token in searchable for token in tokens)]
    return result or ["uncategorized"]


def _roles_for(categories: list[str]) -> list[str]:
    roles: set[str] = set()
    for category in categories:
        roles.update(CATEGORY_ROLES.get(category, set()))
    return sorted(roles)


def seed_catalog(prepared_product: dict[str, Any], catalog_id: str) -> dict[str, Any]:
    product = prepared_product.get("product")
    media_candidates = prepared_product.get("media_candidates")
    if not isinstance(product, dict) or not isinstance(media_candidates, dict):
        raise ValueError("prepared product must contain product and media_candidates objects")
    product_id = str(product.get("product_id") or "").strip()
    if not product_id or product_id != str(media_candidates.get("product_id") or "").strip():
        raise ValueError("prepared product has missing or inconsistent product_id")
    media = media_candidates.get("media")
    if not isinstance(media, list) or len(media) > MAX_ASSETS:
        raise ValueError(f"media_candidates.media must be an array with at most {MAX_ASSETS} items")

    assets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in media:
        if not isinstance(item, dict):
            raise ValueError("each media candidate must be an object")
        path = str(item.get("path") or "").strip()
        media_type = str(item.get("kind") or "").strip()
        if not path or media_type not in MEDIA_TYPES:
            raise ValueError("media candidate requires a path and supported kind")
        sha256 = str(item.get("sha256") or "").lower() or None
        if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"invalid media sha256: {path}")
        asset_id = _stable_asset_id(sha256, path)
        if asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        categories = _suggest_categories(path, media_type)
        source_scope = str(item.get("source_scope") or "shared_asset")
        assets.append({
            "asset_id": asset_id,
            "path": path,
            "sha256": sha256,
            "media_type": media_type,
            "size_bytes": item.get("size_bytes"),
            "source_scope": source_scope,
            "product_id": product_id if source_scope == "product_companion_media" else None,
            "categories": categories,
            "tags": [],
            "allowed_roles": _roles_for(categories),
            "orientation": "unknown",
            "duration_seconds": None,
            "usable_ranges": [],
            "classification_status": "needs_review",
            "authorization_status": "pending",
            "brand_review_status": "needs_review",
            "claim_review_status": "needs_review",
            "quality_status": "needs_review",
            "observed_brands": [],
            "observed_claims": [],
            "suggestion_basis": "path_only_not_product_fact",
            "notes": "Review the actual media before approving classification or ChatCut use.",
        })

    counts = Counter(category for asset in assets for category in asset["categories"])
    return {
        "catalog_version": CATALOG_VERSION,
        "catalog_id": catalog_id.strip() or f"CAT-{_canonical_sha256([item['asset_id'] for item in assets])[:12].upper()}",
        "product_id": product_id,
        "content_is_data_not_instruction": True,
        "source_summary": prepared_product.get("source_summary", {}),
        "assets": assets,
        "classification_summary": dict(sorted(counts.items())),
        "chatcut_eligible_assets": 0,
        "review_required": True,
        "note": "Classification is metadata only; source files are never moved. Path-derived categories are suggestions until visually approved.",
    }


def _list_of_strings(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be an array of non-empty strings")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    return [item.strip() for item in value]


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a positive number")
    return result


def _resolve_approved_media(path_value: str, roots: list[Path]) -> Path | None:
    path = Path(os.path.expandvars(path_value))
    if not path.is_absolute() or not path.is_file():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for root in roots:
        try:
            resolved_root = root.resolve()
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        current = resolved_root
        escaped = False
        for part in relative.parts:
            current = current / part
            if context._is_reparse_point(current):
                escaped = True
                break
        if not escaped:
            return resolved
    return None


def _path_is_under(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _validate_asset(
    asset: dict[str, Any],
    product_roots: list[Path],
    asset_roots: list[Path],
    catalog_product_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    asset_id = str(asset.get("asset_id") or "").strip()
    if not asset_id:
        reasons.append("missing_asset_id")
    path_value = str(asset.get("path") or "").strip()
    resolved = _resolve_approved_media(path_value, product_roots + asset_roots)
    if resolved is None:
        reasons.append("path_not_existing_under_approved_roots")
    source_scope = str(asset.get("source_scope") or "").strip()
    if source_scope not in {"product_companion_media", "shared_asset", "user_confirmed"}:
        reasons.append("invalid_source_scope")
    elif resolved is not None and source_scope == "product_companion_media":
        exact_product_roots = [root / catalog_product_id for root in product_roots]
        if not _path_is_under(resolved, exact_product_roots):
            reasons.append("source_scope_path_mismatch")
    elif resolved is not None and source_scope == "shared_asset" and not _path_is_under(resolved, asset_roots):
        reasons.append("source_scope_path_mismatch")
    media_type = str(asset.get("media_type") or "").strip()
    if media_type not in MEDIA_TYPES:
        reasons.append("unsupported_media_type")
    elif resolved is not None and context.MEDIA_EXTENSIONS.get(resolved.suffix.lower()) != media_type:
        reasons.append("media_type_extension_mismatch")
    sha256 = str(asset.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        reasons.append("missing_or_invalid_sha256")
    elif resolved is not None and _file_sha256(resolved) != sha256:
        reasons.append("sha256_mismatch")
    try:
        categories = set(_list_of_strings(asset.get("categories"), f"asset {asset_id} categories", allow_empty=False))
        if not categories.issubset(CATEGORIES):
            reasons.append("unknown_category")
    except ValueError:
        categories = set()
        reasons.append("invalid_categories")
    try:
        tags = {_normalize_tag(item) for item in _list_of_strings(asset.get("tags"), f"asset {asset_id} tags")}
    except ValueError:
        tags = set()
        reasons.append("invalid_tags")
    try:
        allowed_roles = set(_list_of_strings(asset.get("allowed_roles"), f"asset {asset_id} allowed_roles"))
        if not allowed_roles.issubset(ROLES):
            reasons.append("unknown_allowed_role")
    except ValueError:
        allowed_roles = set()
        reasons.append("invalid_allowed_roles")
    try:
        observed_brands = _list_of_strings(asset.get("observed_brands", []), f"asset {asset_id} observed_brands")
        observed_claims = _list_of_strings(asset.get("observed_claims", []), f"asset {asset_id} observed_claims")
    except ValueError:
        observed_brands = []
        observed_claims = []
        reasons.append("invalid_brand_or_claim_observations")
    orientation = str(asset.get("orientation") or "unknown")
    if orientation not in ORIENTATIONS - {"any"}:
        reasons.append("invalid_orientation")
    if str(asset.get("classification_status") or "") != "approved":
        reasons.append("classification_not_approved")
    if str(asset.get("authorization_status") or "") != "approved":
        reasons.append("authorization_not_approved")
    if str(asset.get("brand_review_status") or "") not in APPROVED_REVIEW:
        reasons.append("brand_review_incomplete")
    if str(asset.get("claim_review_status") or "") not in APPROVED_REVIEW:
        reasons.append("claim_review_incomplete")
    if str(asset.get("quality_status") or "") != "approved":
        reasons.append("quality_not_approved")
    if "reference_only" in categories:
        reasons.append("reference_only_asset")
    duration = asset.get("duration_seconds")
    if duration is not None:
        try:
            duration = _positive_number(duration, f"asset {asset_id} duration_seconds")
        except ValueError:
            reasons.append("invalid_duration")
            duration = None
    usable_ranges = asset.get("usable_ranges", [])
    normalized_ranges: list[dict[str, float]] = []
    if not isinstance(usable_ranges, list):
        reasons.append("invalid_usable_ranges")
    else:
        for range_item in usable_ranges:
            if not isinstance(range_item, dict):
                reasons.append("invalid_usable_range")
                continue
            try:
                start = float(range_item.get("start_seconds"))
                end = float(range_item.get("end_seconds"))
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    raise ValueError
                normalized_ranges.append({"start_seconds": start, "end_seconds": end})
            except (TypeError, ValueError):
                reasons.append("invalid_usable_range")
    if reasons:
        return None, sorted(set(reasons))
    normalized = dict(asset)
    normalized.update({
        "asset_id": asset_id,
        "path": str(resolved),
        "media_type": media_type,
        "source_scope": source_scope,
        "sha256": sha256,
        "categories": categories,
        "tags": tags,
        "allowed_roles": allowed_roles,
        "orientation": orientation,
        "duration_seconds": duration,
        "usable_ranges": normalized_ranges,
        "observed_brands": observed_brands,
        "observed_claims": observed_claims,
    })
    return normalized, []


def _normalize_tag(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().casefold())


def _available_source_range(asset: dict[str, Any], shot_duration: float) -> tuple[float | None, float | None] | None:
    if asset["media_type"] == "image":
        return (None, None)
    for item in asset["usable_ranges"]:
        if item["end_seconds"] - item["start_seconds"] + 1e-9 >= shot_duration:
            return (item["start_seconds"], item["start_seconds"] + shot_duration)
    duration = asset.get("duration_seconds")
    if duration is not None and duration + 1e-9 >= shot_duration:
        return (0.0, shot_duration)
    return None


def _shot_candidates(
    shot: dict[str, Any],
    assets: list[dict[str, Any]],
    product_id: str,
    usage: Counter[str],
    max_asset_uses: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    candidates: list[dict[str, Any]] = []
    diagnostics: Counter[str] = Counter()
    required_categories = set(shot["required_categories"])
    required_tags = set(shot["required_tags"])
    preferred_tags = set(shot["preferred_tags"])
    for asset in assets:
        reasons: list[str] = []
        asset_product = str(asset.get("product_id") or "").strip() or None
        if asset_product != product_id and not (shot["allow_generic"] and asset_product is None):
            reasons.append("product_scope_mismatch")
        if asset["media_type"] not in shot["allowed_media_types"]:
            reasons.append("media_type_mismatch")
        if not required_categories.issubset(asset["categories"]):
            reasons.append("required_category_missing")
        if not required_tags.issubset(asset["tags"]):
            reasons.append("required_tag_missing")
        if shot["role"] not in asset["allowed_roles"]:
            reasons.append("role_not_allowed")
        required_orientation = shot["required_orientation"]
        if required_orientation != "any" and asset["orientation"] != required_orientation:
            reasons.append("orientation_mismatch")
        source_range = _available_source_range(asset, shot["duration_seconds"])
        if source_range is None:
            reasons.append("insufficient_usable_duration")
        if usage[asset["asset_id"]] >= max_asset_uses:
            reasons.append("reuse_limit_reached")
        if reasons:
            diagnostics.update(reasons)
            continue
        preferred_matches = sorted(preferred_tags.intersection(asset["tags"]))
        score = 100 + 12 * len(preferred_matches)
        match_reasons = ["all_hard_constraints_satisfied"]
        if asset_product == product_id:
            score += 10
            match_reasons.append("exact_product_scope")
        if preferred_matches:
            match_reasons.append("preferred_tags:" + ",".join(preferred_matches))
        if shot["preferred_orientation"] != "any" and asset["orientation"] == shot["preferred_orientation"]:
            score += 6
            match_reasons.append("preferred_orientation")
        score -= usage[asset["asset_id"]] * 15
        candidates.append({
            "asset": asset,
            "score": score,
            "match_reasons": match_reasons,
            "source_range": source_range,
        })
    candidates.sort(key=lambda item: (-item["score"], item["asset"]["asset_id"]))
    return candidates, diagnostics


def _normalize_shot(shot: dict[str, Any]) -> dict[str, Any]:
    shot_id = str(shot.get("shot_id") or "").strip()
    if not shot_id:
        raise ValueError("each shot requires shot_id")
    order = shot.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise ValueError(f"shot {shot_id} order must be a positive integer")
    line = str(shot.get("line") or "").strip()
    if not line:
        raise ValueError(f"shot {shot_id} line must not be empty")
    role = str(shot.get("role") or "").strip()
    if role not in ROLES:
        raise ValueError(f"shot {shot_id} has unsupported role: {role}")
    required_categories = _list_of_strings(shot.get("required_categories", []), f"shot {shot_id} required_categories")
    if not set(required_categories).issubset(CATEGORIES):
        raise ValueError(f"shot {shot_id} contains unknown required category")
    required_tags = [_normalize_tag(item) for item in _list_of_strings(shot.get("required_tags", []), f"shot {shot_id} required_tags")]
    if not required_categories and not required_tags:
        raise ValueError(f"shot {shot_id} must declare a required category or tag")
    preferred_tags = [_normalize_tag(item) for item in _list_of_strings(shot.get("preferred_tags", []), f"shot {shot_id} preferred_tags")]
    allowed_media_types = _list_of_strings(
        shot.get("allowed_media_types", ["image", "video"]), f"shot {shot_id} allowed_media_types", allow_empty=False
    )
    if not set(allowed_media_types).issubset(MEDIA_TYPES):
        raise ValueError(f"shot {shot_id} contains unsupported media type")
    required_orientation = str(shot.get("required_orientation") or "any")
    preferred_orientation = str(shot.get("preferred_orientation") or "any")
    if required_orientation not in ORIENTATIONS or preferred_orientation not in ORIENTATIONS:
        raise ValueError(f"shot {shot_id} has invalid orientation")
    fit_mode = str(shot.get("fit_mode") or "contain")
    if fit_mode not in FIT_MODES:
        raise ValueError(f"shot {shot_id} has invalid fit_mode")
    allow_generic = shot.get("allow_generic", False)
    if not isinstance(allow_generic, bool):
        raise ValueError(f"shot {shot_id} allow_generic must be a boolean")
    fact_refs = _list_of_strings(shot.get("fact_refs", []), f"shot {shot_id} fact_refs")
    if role in {"product", "feature", "proof"} and not fact_refs:
        raise ValueError(f"shot {shot_id} role {role} requires at least one fact_ref")
    return {
        "shot_id": shot_id,
        "order": order,
        "line": line,
        "role": role,
        "duration_seconds": _positive_number(shot.get("duration_seconds"), f"shot {shot_id} duration_seconds"),
        "required_categories": required_categories,
        "required_tags": required_tags,
        "preferred_tags": preferred_tags,
        "allowed_media_types": allowed_media_types,
        "required_orientation": required_orientation,
        "preferred_orientation": preferred_orientation,
        "fit_mode": fit_mode,
        "allow_generic": allow_generic,
        "fact_refs": fact_refs,
    }


def match_script(config: dict[str, Any], catalog: dict[str, Any], script: dict[str, Any]) -> dict[str, Any]:
    if str(catalog.get("catalog_version") or "") != CATALOG_VERSION:
        raise ValueError(f"unsupported catalog_version; expected {CATALOG_VERSION}")
    if str(script.get("script_version") or "") != SCRIPT_VERSION:
        raise ValueError(f"unsupported script_version; expected {SCRIPT_VERSION}")
    catalog_id = str(catalog.get("catalog_id") or "").strip()
    script_id = str(script.get("script_id") or "").strip()
    if not catalog_id or not script_id:
        raise ValueError("catalog_id and script_id are required")
    product_id = str(script.get("product_id") or "").strip()
    if not product_id or product_id != str(catalog.get("product_id") or "").strip():
        raise ValueError("catalog and script product_id must match exactly")
    raw_assets = catalog.get("assets")
    raw_shots = script.get("shots")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise ValueError(f"catalog assets must be an array with at most {MAX_ASSETS} items")
    if not isinstance(raw_shots, list) or not raw_shots or len(raw_shots) > MAX_SHOTS:
        raise ValueError(f"script shots must contain 1 to {MAX_SHOTS} items")
    max_asset_uses = script.get("max_asset_uses", 1)
    if isinstance(max_asset_uses, bool) or not isinstance(max_asset_uses, int) or not 1 <= max_asset_uses <= 20:
        raise ValueError("max_asset_uses must be an integer from 1 to 20")
    target_duration = _positive_number(script.get("target_duration_seconds"), "target_duration_seconds")
    aspect_ratio = str(script.get("aspect_ratio") or "").strip()
    if aspect_ratio not in {"9:16", "16:9", "1:1", "4:5"}:
        raise ValueError("unsupported aspect_ratio")

    shots = [_normalize_shot(item) if isinstance(item, dict) else _normalize_shot({}) for item in raw_shots]
    if len({shot["shot_id"] for shot in shots}) != len(shots):
        raise ValueError("shot_id values must be unique")
    if len({shot["order"] for shot in shots}) != len(shots):
        raise ValueError("shot order values must be unique")
    shots.sort(key=lambda item: item["order"])

    product_roots = context.configured_roots(config, "product_sources")
    asset_roots = context.configured_roots(config, "asset_sources")
    valid_assets: list[dict[str, Any]] = []
    rejected_assets: list[dict[str, Any]] = []
    seen_asset_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_hashes: set[str] = set()
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            rejected_assets.append({"asset_id": None, "reasons": ["asset_not_object"]})
            continue
        asset_id = str(raw_asset.get("asset_id") or "").strip() or None
        if asset_id and asset_id in seen_asset_ids:
            rejected_assets.append({"asset_id": asset_id, "reasons": ["duplicate_asset_id"]})
            continue
        if asset_id:
            seen_asset_ids.add(asset_id)
        normalized, reasons = _validate_asset(raw_asset, product_roots, asset_roots, product_id)
        if normalized is not None and normalized["path"] in seen_paths:
            normalized = None
            reasons = ["duplicate_asset_path"]
        elif normalized is not None and normalized["sha256"] in seen_hashes:
            normalized = None
            reasons = ["duplicate_asset_content"]
        if normalized is None:
            rejected_assets.append({"asset_id": asset_id, "path": raw_asset.get("path"), "reasons": reasons})
        else:
            valid_assets.append(normalized)
            seen_paths.add(normalized["path"])
            seen_hashes.add(normalized["sha256"])

    product = context.get_product(config, product_id, 100000)
    approved_text = "\n".join(str(item.get("content") or "") for item in product.get("documents", []))
    reviewed_assets: list[dict[str, Any]] = []
    for asset in valid_assets:
        observation_reasons = []
        brand_status = str(asset.get("brand_review_status") or "")
        claim_status = str(asset.get("claim_review_status") or "")
        if brand_status == "approved" and not asset["observed_brands"]:
            observation_reasons.append("approved_brand_review_requires_observation")
        if brand_status == "not_applicable" and asset["observed_brands"]:
            observation_reasons.append("brand_not_applicable_conflicts_with_observation")
        if claim_status == "approved" and not asset["observed_claims"]:
            observation_reasons.append("approved_claim_review_requires_observation")
        if claim_status == "not_applicable" and asset["observed_claims"]:
            observation_reasons.append("claim_not_applicable_conflicts_with_observation")
        if any(not context._observation_is_approved(value, approved_text) for value in asset["observed_brands"]):
            observation_reasons.append("observed_brand_not_in_approved_documents")
        if any(not context._observation_is_approved(value, approved_text) for value in asset["observed_claims"]):
            observation_reasons.append("observed_claim_not_in_approved_documents")
        if observation_reasons:
            rejected_assets.append({
                "asset_id": asset["asset_id"],
                "path": asset["path"],
                "reasons": sorted(set(observation_reasons)),
            })
        else:
            reviewed_assets.append(asset)
    valid_assets = reviewed_assets
    blockers: list[dict[str, Any]] = []
    if product.get("blocking_issue"):
        blockers.append({"type": "product_context_incomplete"})
    unapproved_fact_refs = []
    for shot in shots:
        for fact_ref in shot["fact_refs"]:
            if not context._observation_is_approved(fact_ref, approved_text):
                unapproved_fact_refs.append({"shot_id": shot["shot_id"], "fact_ref": fact_ref})
    if unapproved_fact_refs:
        blockers.append({"type": "unapproved_script_fact_refs", "items": unapproved_fact_refs})
    total_duration = sum(shot["duration_seconds"] for shot in shots)
    if abs(total_duration - target_duration) > 0.01:
        blockers.append({
            "type": "script_duration_mismatch",
            "target_duration_seconds": target_duration,
            "shot_duration_seconds": total_duration,
        })

    usage: Counter[str] = Counter()
    selections: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    timeline_start = 0.0
    for shot in shots:
        candidates, diagnostics = _shot_candidates(shot, valid_assets, product_id, usage, max_asset_uses)
        if not candidates:
            unresolved.append({
                "shot_id": shot["shot_id"],
                "order": shot["order"],
                "line": shot["line"],
                "timeline_start_seconds": timeline_start,
                "duration_seconds": shot["duration_seconds"],
                "reason": "no_asset_satisfies_hard_constraints",
                "rejection_reason_counts": dict(sorted(diagnostics.items())),
            })
            timeline_start += shot["duration_seconds"]
            continue
        chosen = candidates[0]
        asset = chosen["asset"]
        source_start, source_end = chosen["source_range"]
        usage[asset["asset_id"]] += 1
        selections.append({
            "shot_id": shot["shot_id"],
            "order": shot["order"],
            "line": shot["line"],
            "role": shot["role"],
            "timeline_start_seconds": timeline_start,
            "duration_seconds": shot["duration_seconds"],
            "asset_id": asset["asset_id"],
            "path": asset["path"],
            "sha256": asset["sha256"],
            "media_type": asset["media_type"],
            "source_in_seconds": source_start,
            "source_out_seconds": source_end,
            "fit_mode": shot["fit_mode"],
            "match_score": chosen["score"],
            "match_reasons": chosen["match_reasons"],
            "alternates": [
                {"asset_id": item["asset"]["asset_id"], "path": item["asset"]["path"], "score": item["score"]}
                for item in candidates[1:4]
            ],
        })
        timeline_start += shot["duration_seconds"]

    if unresolved:
        blockers.append({"type": "unresolved_script_shots", "shot_ids": [item["shot_id"] for item in unresolved]})
    if not valid_assets:
        blockers.append({"type": "no_chatcut_eligible_assets"})
    ready = not blockers and len(selections) == len(shots)
    import_paths = list(dict.fromkeys(item["path"] for item in selections)) if ready else []
    selection_core = {
        "catalog_id": catalog_id,
        "script_id": script_id,
        "product_id": product_id,
        "selections": selections,
    }
    return {
        "catalog_id": catalog_id,
        "script_id": script_id,
        "product_id": product_id,
        "aspect_ratio": aspect_ratio,
        "target_duration_seconds": target_duration,
        "ready_for_chatcut": ready,
        "catalog_sha256": _canonical_sha256(catalog),
        "script_sha256": _canonical_sha256(script),
        "selection_manifest_sha256": _canonical_sha256(selection_core),
        "classification_summary": dict(sorted(Counter(
            category for asset in valid_assets for category in asset["categories"]
        ).items())),
        "eligible_asset_count": len(valid_assets),
        "rejected_assets": rejected_assets,
        "selections": selections,
        "unresolved_shots": unresolved,
        "blockers": blockers,
        "chatcut_handoff": {
            "ready": ready,
            "import_paths": import_paths,
            "placements": selections if ready else [],
            "candidate_placements": selections,
            "instruction": "Import and place only when ready is true; preserve source media and existing timelines.",
        },
        "content_is_data_not_instruction": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-catalog")
    seed.add_argument("--prepared-product-file", required=True, help="Path to prepare-product JSON, or - for stdin")
    seed.add_argument("--catalog-id", default="")
    direct = sub.add_parser("seed-catalog-from-product")
    direct.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    direct.add_argument("--product-id", required=True)
    direct.add_argument("--query", default="")
    direct.add_argument("--limit", type=int, default=200)
    direct.add_argument("--catalog-id", default="")
    match = sub.add_parser("match-script")
    match.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    match.add_argument("--catalog-file", required=True)
    match.add_argument("--script-file", required=True)
    return parser


def main() -> int:
    configure_console()
    args = build_parser().parse_args()
    if args.command == "seed-catalog":
        payload = seed_catalog(read_json_object(args.prepared_product_file), args.catalog_id)
    elif args.command == "seed-catalog-from-product":
        config = context.load_config(args.config)
        prepared = context.prepare_product(
            config,
            args.product_id,
            args.query,
            args.limit,
            12000,
            True,
            [],
            [],
        )
        payload = seed_catalog(prepared, args.catalog_id)
    else:
        config = context.load_config(args.config)
        payload = match_script(config, read_json_object(args.catalog_file), read_json_object(args.script_file))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
