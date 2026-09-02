#!/usr/bin/env python3
"""Validate repository JSON, required files, test cases, and secret hygiene."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
REQUIRED = [
    PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
    PLUGIN_ROOT / "skills" / "company-video-producer" / "SKILL.md",
    PLUGIN_ROOT / "department-config" / "company-video.json",
    PLUGIN_ROOT / "templates" / "video-job.schema.json",
    PLUGIN_ROOT / "templates" / "approval-scope.example.json",
    PLUGIN_ROOT / "templates" / "directive.example.json",
    PLUGIN_ROOT / "templates" / "test-cases" / "two-day-video-flow.json",
    PLUGIN_ROOT / "scripts" / "company_context.py",
    PLUGIN_ROOT / "scripts" / "material_planner.py",
    PLUGIN_ROOT / "scripts" / "template_service.py",
    PLUGIN_ROOT / "scripts" / "task_store.py",
    PLUGIN_ROOT / "templates" / "material-catalog.schema.json",
    PLUGIN_ROOT / "templates" / "material-catalog.example.json",
    PLUGIN_ROOT / "templates" / "shot-script.schema.json",
    PLUGIN_ROOT / "templates" / "shot-script.example.json",
    PLUGIN_ROOT / "templates" / "remix-template.schema.json",
    PLUGIN_ROOT / "templates" / "remix-template.example.json",
    PLUGIN_ROOT / "templates" / "batch-remix-request.schema.json",
    PLUGIN_ROOT / "templates" / "batch-remix-request.example.json",
    PLUGIN_ROOT / "skills" / "company-video-producer" / "references" / "template-remix.md",
    PLUGIN_ROOT / "docs" / "system-architecture.md",
    PLUGIN_ROOT / "docs" / "data-source-contract.md",
    PLUGIN_ROOT / "docs" / "local-install.md",
    PLUGIN_ROOT / "docs" / "test-plan.md",
    PLUGIN_ROOT / "docs" / "validation-report.md",
    PLUGIN_ROOT / "docs" / "github-handoff.md",
]
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|secret|api[_-]?key)\s*[:=]\s*['\"][^<'\"]{8,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{16,}"),
]
FORBIDDEN_DEPENDENCIES = [
    "video" + "-agent-factory",
    "company" + "_video_factory",
    "127.0.0.1" + ":8790",
    "COMPANY_VIDEO_" + "API_BASE",
    "COMPANY_VIDEO_" + "API_TOKEN",
    "OVERSEAS_" + "DB_",
    "manifest_" + "fallback",
    "D:" + "\\work\\",
]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    errors: list[str] = []
    for path in REQUIRED:
        if not path.is_file():
            errors.append(f"missing required file: {path.relative_to(PLUGIN_ROOT)}")
    for path in REPO_ROOT.rglob("*.json"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON {path.relative_to(REPO_ROOT)}: {exc}")
    config_path = PLUGIN_ROOT / "department-config" / "company-video.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        storage = config.get("storage", {})
        policies = config.get("policies", {})
        for source_name in ("product_sources", "asset_sources", "template_sources"):
            if storage.get(source_name, {}).get("access") != "read_only":
                errors.append(f"{source_name} must declare read_only access")
        for local_name in ("task_database", "local_work_root"):
            local_config = storage.get(local_name, {})
            if local_config.get("network_share_policy") != "prohibited":
                errors.append(f"{local_name} must prohibit network shares")
            if local_config.get("source_overlap_policy") != "prohibited":
                errors.append(f"{local_name} must prohibit source-root overlap")
        if policies.get("runtime_storage") != "local_only":
            errors.append("runtime_storage policy must be local_only")
        if policies.get("nas_sources") != "read_only":
            errors.append("nas_sources policy must be read_only")
        if policies.get("material_classification") != "metadata_only_never_move_source":
            errors.append("material classification must never move source files")
        if policies.get("script_asset_matching") != "deterministic_hard_constraints_before_chatcut":
            errors.append("script asset matching must use deterministic hard constraints")
        if policies.get("chatcut_import") != "selection_manifest_only":
            errors.append("ChatCut import must be limited to the selection manifest")
        if policies.get("template_scripts") != "approved_read_only_templates_only":
            errors.append("template scripts must be approved and read only")
        if policies.get("bulk_remix") != "deterministic_template_and_selection_manifests":
            errors.append("bulk remix must generate deterministic manifests")
        if policies.get("chatcut_bulk_execution") != "named_versions_preserve_existing_no_export_by_default":
            errors.append("ChatCut bulk execution must preserve named versions and default to no export")
    cases_path = PLUGIN_ROOT / "templates" / "test-cases" / "two-day-video-flow.json"
    if cases_path.is_file():
        payload = json.loads(cases_path.read_text(encoding="utf-8"))
        ids = [case.get("id") for case in payload.get("cases", [])]
        if len(ids) != 8 or len(set(ids)) != 8:
            errors.append("two-day regression pack must contain 8 unique cases")
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or path.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".webp", ".pyc"
        }:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"possible embedded secret: {path.relative_to(REPO_ROOT)}")
                break
        for marker in FORBIDDEN_DEPENDENCIES:
            if marker.casefold() in text.casefold():
                errors.append(f"forbidden legacy dependency '{marker}': {path.relative_to(REPO_ROOT)}")
                break
    for legacy in (PLUGIN_ROOT / "scripts" / "job_manifest.py", PLUGIN_ROOT / "tests" / "test_job_manifest.py"):
        if legacy.exists():
            errors.append(f"legacy fallback file still present: {legacy.relative_to(PLUGIN_ROOT)}")
    result = {"ok": not errors, "errors": errors, "required_files": len(REQUIRED)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
