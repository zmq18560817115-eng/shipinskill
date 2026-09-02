#!/usr/bin/env python3
"""Resolve approved company remix templates and build deterministic ChatCut batch handoffs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import company_context as context  # noqa: E402
import material_planner as planner  # noqa: E402


DEFAULT_CONFIG = context.DEFAULT_CONFIG
TEMPLATE_VERSION = "1.0"
REQUEST_VERSION = "1.0"
BATCH_VERSION = "1.0"
MAX_TEMPLATES = 500
MAX_OUTPUTS = 100
TEMPLATE_STATUSES = {"draft", "approved", "disabled"}
CHATCUT_LAYOUTS = {"named_timelines", "separate_projects"}
EXPORT_POLICIES = {"not_requested", "review_before_export"}


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


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be an array of non-empty strings")
    result = [item.strip() for item in value]
    if not allow_empty and not result:
        raise ValueError(f"{field} must not be empty")
    if len(set(item.casefold() for item in result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a positive number")
    return result


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _normalize_tags(values: list[str]) -> list[str]:
    return [re.sub(r"\s+", "-", item.casefold()) for item in values]


def _approved_path(path: Path, roots: list[Path]) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix.casefold() != ".json":
        return None
    for root in roots:
        try:
            resolved_root = root.resolve()
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        current = resolved_root
        if context._is_reparse_point(current):
            continue
        escaped = False
        for part in relative.parts:
            current = current / part
            if context._is_reparse_point(current):
                escaped = True
                break
        if not escaped:
            return resolved
    return None


def _normalize_template(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    if str(payload.get("template_version") or "") != TEMPLATE_VERSION:
        raise ValueError(f"unsupported template_version in {path}; expected {TEMPLATE_VERSION}")
    template_id = _required_string(payload.get("template_id"), "template_id")
    display_name = _required_string(payload.get("display_name"), f"template {template_id} display_name")
    revision = _bounded_int(payload.get("revision"), f"template {template_id} revision", 1, 1_000_000)
    status = _required_string(payload.get("status"), f"template {template_id} status")
    if status not in TEMPLATE_STATUSES:
        raise ValueError(f"template {template_id} has unsupported status")
    product_scope = _string_list(
        payload.get("product_scope"), f"template {template_id} product_scope", allow_empty=False
    )
    if "*" in product_scope and len(product_scope) != 1:
        raise ValueError(f"template {template_id} product_scope wildcard must be used alone")
    match = payload.get("match")
    if not isinstance(match, dict):
        raise ValueError(f"template {template_id} match must be an object")
    platforms = _string_list(match.get("platforms"), f"template {template_id} match.platforms", allow_empty=False)
    aspect_ratios = _string_list(
        match.get("aspect_ratios"), f"template {template_id} match.aspect_ratios", allow_empty=False
    )
    if not set(aspect_ratios).issubset({"9:16", "16:9", "1:1", "4:5"}):
        raise ValueError(f"template {template_id} contains an unsupported aspect ratio")
    languages = _string_list(match.get("languages"), f"template {template_id} match.languages", allow_empty=False)
    duration_seconds = _positive_number(
        match.get("duration_seconds"), f"template {template_id} match.duration_seconds"
    )
    tags = _normalize_tags(_string_list(match.get("tags", []), f"template {template_id} match.tags"))
    max_outputs = _bounded_int(payload.get("max_outputs", 20), f"template {template_id} max_outputs", 1, MAX_OUTPUTS)
    max_asset_uses = _bounded_int(
        payload.get("max_asset_uses", 1), f"template {template_id} max_asset_uses", 1, 20
    )

    raw_shots = payload.get("shots")
    if not isinstance(raw_shots, list) or not 1 <= len(raw_shots) <= planner.MAX_SHOTS:
        raise ValueError(f"template {template_id} shots must contain 1 to {planner.MAX_SHOTS} items")
    shots: list[dict[str, Any]] = []
    for raw_shot in raw_shots:
        if not isinstance(raw_shot, dict):
            raise ValueError(f"template {template_id} shot must be an object")
        slot_id = _required_string(raw_shot.get("slot_id"), f"template {template_id} shot slot_id")
        line_slot = _required_string(raw_shot.get("line_slot"), f"template {template_id} shot {slot_id} line_slot")
        role = _required_string(raw_shot.get("role"), f"template {template_id} shot {slot_id} role")
        if role not in planner.ROLES:
            raise ValueError(f"template {template_id} shot {slot_id} has unsupported role")
        required_categories = _string_list(
            raw_shot.get("required_categories", []), f"template {template_id} shot {slot_id} required_categories"
        )
        if not set(required_categories).issubset(planner.CATEGORIES):
            raise ValueError(f"template {template_id} shot {slot_id} contains an unknown category")
        required_tags = _normalize_tags(_string_list(
            raw_shot.get("required_tags", []), f"template {template_id} shot {slot_id} required_tags"
        ))
        if not required_categories and not required_tags:
            raise ValueError(f"template {template_id} shot {slot_id} needs a required category or tag")
        preferred_tags = _normalize_tags(_string_list(
            raw_shot.get("preferred_tags", []), f"template {template_id} shot {slot_id} preferred_tags"
        ))
        allowed_media_types = _string_list(
            raw_shot.get("allowed_media_types", ["image", "video"]),
            f"template {template_id} shot {slot_id} allowed_media_types",
            allow_empty=False,
        )
        if not set(allowed_media_types).issubset(planner.MEDIA_TYPES):
            raise ValueError(f"template {template_id} shot {slot_id} contains an unsupported media type")
        required_orientation = str(raw_shot.get("required_orientation") or "any")
        preferred_orientation = str(raw_shot.get("preferred_orientation") or "any")
        if required_orientation not in planner.ORIENTATIONS or preferred_orientation not in planner.ORIENTATIONS:
            raise ValueError(f"template {template_id} shot {slot_id} has an invalid orientation")
        fit_mode = str(raw_shot.get("fit_mode") or "contain")
        if fit_mode not in planner.FIT_MODES:
            raise ValueError(f"template {template_id} shot {slot_id} has an invalid fit mode")
        allow_generic = raw_shot.get("allow_generic", False)
        if not isinstance(allow_generic, bool):
            raise ValueError(f"template {template_id} shot {slot_id} allow_generic must be a boolean")
        shots.append({
            "slot_id": slot_id,
            "line_slot": line_slot,
            "role": role,
            "duration_seconds": _positive_number(
                raw_shot.get("duration_seconds"), f"template {template_id} shot {slot_id} duration_seconds"
            ),
            "required_categories": required_categories,
            "required_tags": required_tags,
            "preferred_tags": preferred_tags,
            "allowed_media_types": allowed_media_types,
            "required_orientation": required_orientation,
            "preferred_orientation": preferred_orientation,
            "fit_mode": fit_mode,
            "allow_generic": allow_generic,
        })
    shot_ids = [shot["slot_id"] for shot in shots]
    if len(set(shot_ids)) != len(shot_ids):
        raise ValueError(f"template {template_id} shot slot_id values must be unique")
    total_duration = sum(shot["duration_seconds"] for shot in shots)
    if abs(total_duration - duration_seconds) > 0.01:
        raise ValueError(f"template {template_id} shot duration does not match match.duration_seconds")

    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        raise ValueError(f"template {template_id} variants must not be empty")
    variants: list[dict[str, Any]] = []
    for raw_variant in raw_variants:
        if not isinstance(raw_variant, dict):
            raise ValueError(f"template {template_id} variant must be an object")
        variant_id = _required_string(raw_variant.get("variant_id"), f"template {template_id} variant_id")
        name = _required_string(raw_variant.get("name"), f"template {template_id} variant {variant_id} name")
        shot_order = _string_list(
            raw_variant.get("shot_order"), f"template {template_id} variant {variant_id} shot_order", allow_empty=False
        )
        if len(shot_order) != len(shot_ids) or set(shot_order) != set(shot_ids):
            raise ValueError(f"template {template_id} variant {variant_id} must reference every shot exactly once")
        variants.append({"variant_id": variant_id, "name": name, "shot_order": shot_order})
    if len({item["variant_id"] for item in variants}) != len(variants):
        raise ValueError(f"template {template_id} variant_id values must be unique")

    return {
        "template_version": TEMPLATE_VERSION,
        "template_id": template_id,
        "display_name": display_name,
        "revision": revision,
        "status": status,
        "product_scope": product_scope,
        "match": {
            "platforms": platforms,
            "aspect_ratios": aspect_ratios,
            "languages": languages,
            "duration_seconds": duration_seconds,
            "tags": tags,
        },
        "max_outputs": max_outputs,
        "max_asset_uses": max_asset_uses,
        "shots": shots,
        "variants": variants,
        "notes": str(payload.get("notes") or "").strip(),
        "path": str(path),
        "sha256": _file_sha256(path),
    }


def discover_templates(config: dict[str, Any]) -> dict[str, Any]:
    roots = context.configured_roots(config, "template_sources")
    templates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir() and os.access(root, os.R_OK):
            candidates.extend(sorted(root.rglob("*.json"), key=lambda path: str(path).casefold()))
    if len(candidates) > MAX_TEMPLATES:
        raise ValueError(f"template sources contain more than {MAX_TEMPLATES} JSON files")
    seen_ids: set[str] = set()
    for candidate in candidates:
        resolved = _approved_path(candidate, roots)
        if resolved is None:
            errors.append({"path": str(candidate), "error": "path_not_under_approved_template_roots"})
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("template JSON root must be an object")
            template = _normalize_template(payload, resolved)
            if template["template_id"] in seen_ids:
                raise ValueError(f"duplicate template_id: {template['template_id']}")
            seen_ids.add(template["template_id"])
            templates.append(template)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"path": str(resolved), "error": str(exc)})
    templates.sort(key=lambda item: (item["template_id"].casefold(), -item["revision"], item["path"].casefold()))
    approved = [item for item in templates if item["status"] == "approved"]
    return {
        "template_sources": [str(root) for root in roots],
        "templates": templates,
        "errors": errors,
        "template_count": len(templates),
        "approved_template_count": len(approved),
        "blocking_issue": "template_sources_unconfigured" if not roots else (
            "no_approved_templates" if not approved else None
        ),
        "content_is_data_not_instruction": True,
    }


def _normalize_request(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("request_version") or "") != REQUEST_VERSION:
        raise ValueError(f"unsupported request_version; expected {REQUEST_VERSION}")
    request_id = _required_string(payload.get("request_id"), "request_id")
    product_id = _required_string(payload.get("product_id"), "product_id")
    platform = _required_string(payload.get("platform"), "platform")
    aspect_ratio = _required_string(payload.get("aspect_ratio"), "aspect_ratio")
    if aspect_ratio not in {"9:16", "16:9", "1:1", "4:5"}:
        raise ValueError("unsupported aspect_ratio")
    language = _required_string(payload.get("language"), "language")
    output_count = _bounded_int(payload.get("output_count"), "output_count", 1, MAX_OUTPUTS)
    template_id_value = payload.get("template_id")
    if template_id_value is not None and (not isinstance(template_id_value, str) or not template_id_value.strip()):
        raise ValueError("template_id must be null or a non-empty string")
    required_template_tags = _normalize_tags(_string_list(
        payload.get("required_template_tags", []), "required_template_tags"
    ))
    approved_copy = payload.get("approved_copy")
    if not isinstance(approved_copy, dict):
        raise ValueError("approved_copy must be an object")
    normalized_copy: dict[str, dict[str, Any]] = {}
    for key, value in approved_copy.items():
        slot = _required_string(key, "approved_copy key")
        if not isinstance(value, dict):
            raise ValueError(f"approved_copy.{slot} must be an object")
        normalized_copy[slot] = {
            "text": _required_string(value.get("text"), f"approved_copy.{slot}.text"),
            "fact_refs": _string_list(value.get("fact_refs", []), f"approved_copy.{slot}.fact_refs"),
        }
    max_incremental_credits = payload.get("max_incremental_credits", 0)
    if isinstance(max_incremental_credits, bool) or not isinstance(max_incremental_credits, (int, float)):
        raise ValueError("max_incremental_credits must be a non-negative number")
    max_incremental_credits = float(max_incremental_credits)
    if not math.isfinite(max_incremental_credits) or max_incremental_credits < 0:
        raise ValueError("max_incremental_credits must be a non-negative number")
    export_policy = _required_string(payload.get("export_policy"), "export_policy")
    if export_policy not in EXPORT_POLICIES:
        raise ValueError("unsupported export_policy")
    chatcut_layout = _required_string(payload.get("chatcut_layout"), "chatcut_layout")
    if chatcut_layout not in CHATCUT_LAYOUTS:
        raise ValueError("unsupported chatcut_layout")
    if payload.get("preserve_existing_timelines") is not True:
        raise ValueError("preserve_existing_timelines must be true for automated batch planning")
    timeline_name_prefix = str(payload.get("timeline_name_prefix") or request_id).strip()
    if not timeline_name_prefix:
        raise ValueError("timeline_name_prefix must not be empty")
    return {
        "request_version": REQUEST_VERSION,
        "request_id": request_id,
        "product_id": product_id,
        "platform": platform,
        "aspect_ratio": aspect_ratio,
        "duration_seconds": _positive_number(payload.get("duration_seconds"), "duration_seconds"),
        "language": language,
        "output_count": output_count,
        "template_id": template_id_value.strip() if isinstance(template_id_value, str) else None,
        "required_template_tags": required_template_tags,
        "approved_copy": normalized_copy,
        "chatcut_layout": chatcut_layout,
        "preserve_existing_timelines": True,
        "timeline_name_prefix": timeline_name_prefix,
        "max_incremental_credits": max_incremental_credits,
        "export_policy": export_policy,
    }


def _matches(value: str, allowed: list[str]) -> bool:
    folded = {item.casefold() for item in allowed}
    return "*" in folded or value.casefold() in folded


def resolve_template(config: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    request = _normalize_request(request_payload)
    discovery = discover_templates(config)
    candidates: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}
    for template in discovery["templates"]:
        reasons: list[str] = []
        if template["status"] != "approved":
            reasons.append("template_not_approved")
        if request["template_id"] and template["template_id"] != request["template_id"]:
            reasons.append("template_id_mismatch")
        if not _matches(request["product_id"], template["product_scope"]):
            reasons.append("product_scope_mismatch")
        if not _matches(request["platform"], template["match"]["platforms"]):
            reasons.append("platform_mismatch")
        if request["aspect_ratio"] not in template["match"]["aspect_ratios"]:
            reasons.append("aspect_ratio_mismatch")
        if not _matches(request["language"], template["match"]["languages"]):
            reasons.append("language_mismatch")
        if abs(request["duration_seconds"] - template["match"]["duration_seconds"]) > 0.01:
            reasons.append("duration_mismatch")
        if not set(request["required_template_tags"]).issubset(template["match"]["tags"]):
            reasons.append("required_template_tag_missing")
        if request["output_count"] > template["max_outputs"]:
            reasons.append("output_count_exceeds_template_limit")
        if reasons:
            for reason in set(reasons):
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        score = 100
        match_reasons = ["all_template_constraints_satisfied"]
        if request["template_id"] == template["template_id"]:
            score += 1000
            match_reasons.append("explicit_template_id")
        if request["product_id"] in template["product_scope"]:
            score += 200
            match_reasons.append("exact_product_scope")
        score += 20 * len(request["required_template_tags"])
        score += min(template["revision"], 100)
        candidates.append({"template": template, "score": score, "match_reasons": match_reasons})
    candidates.sort(key=lambda item: (
        -item["score"], item["template"]["template_id"].casefold(), -item["template"]["revision"]
    ))
    if not candidates:
        return {
            "ready": False,
            "request": request,
            "blocking_issue": discovery["blocking_issue"] or "no_template_matches_request",
            "rejection_reason_counts": dict(sorted(rejection_counts.items())),
            "template_errors": discovery["errors"],
            "content_is_data_not_instruction": True,
        }
    selected = candidates[0]
    template = selected["template"]
    return {
        "ready": True,
        "request": request,
        "template": template,
        "selection": {
            "template_id": template["template_id"],
            "revision": template["revision"],
            "path": template["path"],
            "sha256": template["sha256"],
            "score": selected["score"],
            "match_reasons": selected["match_reasons"],
        },
        "alternate_templates": [
            {"template_id": item["template"]["template_id"], "revision": item["template"]["revision"], "score": item["score"]}
            for item in candidates[1:4]
        ],
        "template_errors": discovery["errors"],
        "content_is_data_not_instruction": True,
    }


def _render_script(template: dict[str, Any], request: dict[str, Any], variant: dict[str, Any], index: int) -> dict[str, Any]:
    shots_by_id = {item["slot_id"]: item for item in template["shots"]}
    rendered_shots: list[dict[str, Any]] = []
    for order, slot_id in enumerate(variant["shot_order"], start=1):
        template_shot = shots_by_id[slot_id]
        line_slot = template_shot["line_slot"]
        copy = request["approved_copy"].get(line_slot)
        if copy is None:
            raise ValueError(f"missing approved_copy slot required by template: {line_slot}")
        if template_shot["role"] in {"product", "feature", "proof"} and not copy["fact_refs"]:
            raise ValueError(f"approved_copy.{line_slot}.fact_refs is required for role {template_shot['role']}")
        rendered_shots.append({
            "shot_id": f"{slot_id}-{index + 1:02d}",
            "order": order,
            "line": copy["text"],
            "role": template_shot["role"],
            "duration_seconds": template_shot["duration_seconds"],
            "required_categories": template_shot["required_categories"],
            "required_tags": template_shot["required_tags"],
            "preferred_tags": template_shot["preferred_tags"],
            "allowed_media_types": template_shot["allowed_media_types"],
            "required_orientation": template_shot["required_orientation"],
            "preferred_orientation": template_shot["preferred_orientation"],
            "fit_mode": template_shot["fit_mode"],
            "allow_generic": template_shot["allow_generic"],
            "fact_refs": copy["fact_refs"],
        })
    return {
        "script_version": planner.SCRIPT_VERSION,
        "script_id": f"{request['request_id']}-{variant['variant_id']}-{index + 1:02d}",
        "product_id": request["product_id"],
        "aspect_ratio": request["aspect_ratio"],
        "target_duration_seconds": request["duration_seconds"],
        "max_asset_uses": template["max_asset_uses"],
        "selection_seed": index,
        "shots": rendered_shots,
    }


def plan_batch(config: dict[str, Any], request_payload: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve_template(config, request_payload)
    if not resolved["ready"]:
        return {
            "batch_version": BATCH_VERSION,
            "ready_for_chatcut": False,
            "request_sha256": _canonical_sha256(request_payload),
            "template_resolution": resolved,
            "jobs": [],
            "blockers": [{"type": resolved["blocking_issue"]}],
            "chatcut_batch_handoff": {"ready": False, "outputs": [], "candidate_outputs": []},
            "content_is_data_not_instruction": True,
        }
    request = resolved["request"]
    template = resolved["template"]
    jobs: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for index in range(request["output_count"]):
        variant = template["variants"][index % len(template["variants"])]
        try:
            script = _render_script(template, request, variant, index)
            matched = planner.match_script(config, catalog, script)
        except ValueError as exc:
            blockers.append({"type": "template_render_failed", "output_index": index + 1, "message": str(exc)})
            continue
        timeline_name = f"{request['timeline_name_prefix']}｜{template['template_id']}｜{variant['variant_id']}｜{index + 1:02d}"
        jobs.append({
            "output_index": index + 1,
            "variant_id": variant["variant_id"],
            "variant_name": variant["name"],
            "timeline_name": timeline_name[:160],
            "shot_script": script,
            "shot_script_sha256": matched["script_sha256"],
            "selection_manifest_sha256": matched["selection_manifest_sha256"],
            "ready_for_chatcut": matched["ready_for_chatcut"],
            "blockers": matched["blockers"],
            "chatcut_handoff": matched["chatcut_handoff"],
        })
        if not matched["ready_for_chatcut"]:
            blockers.append({
                "type": "output_not_ready_for_chatcut",
                "output_index": index + 1,
                "selection_blockers": matched["blockers"],
            })
    ready = not blockers and len(jobs) == request["output_count"] and all(
        item["ready_for_chatcut"] for item in jobs
    )
    candidate_outputs = [{
        "output_index": item["output_index"],
        "timeline_name": item["timeline_name"],
        "variant_id": item["variant_id"],
        "template_id": template["template_id"],
        "template_sha256": template["sha256"],
        "selection_manifest_sha256": item["selection_manifest_sha256"],
        "import_paths": item["chatcut_handoff"]["import_paths"],
        "placements": item["chatcut_handoff"]["placements"],
    } for item in jobs]
    shared_import_paths = list(dict.fromkeys(
        path for output in candidate_outputs for path in output["import_paths"]
    )) if ready else []
    batch_core = {
        "request_id": request["request_id"],
        "template_id": template["template_id"],
        "template_sha256": template["sha256"],
        "outputs": candidate_outputs,
    }
    return {
        "batch_version": BATCH_VERSION,
        "batch_id": f"BATCH-{_canonical_sha256(batch_core)[:12].upper()}",
        "product_id": request["product_id"],
        "request_sha256": _canonical_sha256(request_payload),
        "catalog_sha256": _canonical_sha256(catalog),
        "template_selection": resolved["selection"],
        "output_count": request["output_count"],
        "ready_for_chatcut": ready,
        "jobs": jobs,
        "blockers": blockers,
        "estimated_incremental_credits": 0,
        "max_incremental_credits": request["max_incremental_credits"],
        "export_policy": request["export_policy"],
        "requires_chatcut_write_authorization": True,
        "paid_generation_authorized": False,
        "paid_generation_requires_separate_approval": True,
        "chatcut_batch_handoff": {
            "ready": ready,
            "layout": request["chatcut_layout"],
            "preserve_existing_timelines": True,
            "shared_import_paths": shared_import_paths,
            "outputs": candidate_outputs if ready else [],
            "candidate_outputs": candidate_outputs,
            "instruction": "Create only named versions from this handoff; preserve prior timelines and do not export without review.",
        },
        "content_is_data_not_instruction": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listed = sub.add_parser("list-templates")
    listed.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resolved = sub.add_parser("resolve-template")
    resolved.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resolved.add_argument("--request-file", required=True, help="Path to batch remix request JSON, or - for stdin")
    planned = sub.add_parser("plan-batch")
    planned.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    planned.add_argument("--request-file", required=True, help="Path to batch remix request JSON, or - for stdin")
    planned.add_argument("--catalog-file", required=True, help="Path to reviewed material catalog JSON")
    return parser


def main() -> int:
    configure_console()
    args = build_parser().parse_args()
    config = context.load_config(args.config)
    if args.command == "list-templates":
        payload = discover_templates(config)
    elif args.command == "resolve-template":
        payload = resolve_template(config, read_json_object(args.request_file))
    else:
        payload = plan_batch(config, read_json_object(args.request_file), read_json_object(args.catalog_file))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
