#!/usr/bin/env python3
"""Read-only NAS product and shared-asset context for Company FlowCut Producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PLUGIN_ROOT / "department-config" / "company-video.json"
MEDIA_EXTENSIONS = {
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".aac": "audio",
}
MAX_PRODUCT_BYTES = 2 * 1024 * 1024


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("configuration root must be an object")
    return payload


def _split_paths(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(";") if item.strip()]


def configured_roots(config: dict[str, Any], kind: str) -> list[Path]:
    entry = config.get("storage", {}).get(kind, {})
    env_name = str(entry.get("paths_env") or "")
    override = os.environ.get(env_name, "").strip() if env_name else ""
    values = _split_paths(override) if override else [str(item) for item in entry.get("paths", [])]
    roots = [Path(os.path.expandvars(value)) for value in values]
    for root in roots:
        if _is_reparse_point(root):
            raise ValueError(f"configured {kind} root cannot be a symbolic link or junction: {root}")
    return roots


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return path.is_symlink() or bool(attributes & marker)


def task_db_path(config: dict[str, Any]) -> Path:
    entry = config.get("storage", {}).get("task_database", {})
    env_name = str(entry.get("path_env") or "COMPANY_VIDEO_DB_PATH")
    raw = os.environ.get(env_name, "").strip() or str(entry.get("default_local") or "")
    if raw:
        return Path(os.path.expandvars(raw))
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / ".local" / "share")
    return Path(base) / "CompanyVideoWorkbench" / "tasks.sqlite3"


def local_work_root(config: dict[str, Any]) -> Path:
    entry = config.get("storage", {}).get("local_work_root", {})
    env_name = str(entry.get("path_env") or "COMPANY_VIDEO_WORK_ROOT")
    raw = os.environ.get(env_name, "").strip() or str(entry.get("default_local") or "")
    if raw:
        return Path(os.path.expandvars(raw))
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / ".local" / "share")
    return Path(base) / "CompanyVideoWorkbench" / "work"


def output_root(config: dict[str, Any]) -> Path | None:
    entry = config.get("storage", {}).get("output_root", {})
    env_name = str(entry.get("path_env") or "COMPANY_VIDEO_OUTPUT_ROOT")
    raw = os.environ.get(env_name, "").strip() or str(entry.get("path") or "").strip()
    return Path(os.path.expandvars(raw)) if raw else None


def path_status(path: Path, *, kind: str) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "kind": kind,
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK),
        "writable": exists and os.access(path, os.W_OK),
        "source_type": "nas" if _is_network_path(path) else "local",
    }


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


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path.parent
    while current != current.parent:
        if current.exists():
            return current
        current = current.parent
    return current if current.exists() else None


def preflight(config: dict[str, Any]) -> dict[str, Any]:
    product_roots = configured_roots(config, "product_sources")
    asset_roots = configured_roots(config, "asset_sources")
    source_roots = product_roots + asset_roots
    product_sources = [
        {**path_status(path, kind="product_source"), "access_policy": "read_only"}
        for path in product_roots
    ]
    asset_sources = [
        {**path_status(path, kind="asset_source"), "access_policy": "read_only"}
        for path in asset_roots
    ]
    db = task_db_path(config)
    db_parent = _nearest_existing_parent(db)
    db_is_network = _is_network_path(db)
    work = local_work_root(config)
    work_parent = _nearest_existing_parent(work / ".write-probe")
    work_is_network = _is_network_path(work)
    violations = []
    if db_is_network:
        violations.append("task_database_on_network_share")
    if work_is_network:
        violations.append("local_work_root_on_network_share")
    if any(_paths_overlap(db, root) for root in source_roots):
        violations.append("task_database_overlaps_source_root")
    if any(_paths_overlap(work, root) for root in source_roots):
        violations.append("local_work_root_overlaps_source_root")
    if db_parent is None or not os.access(db_parent, os.W_OK):
        violations.append("task_database_parent_not_writable")
    if work_parent is None or not os.access(work_parent, os.W_OK):
        violations.append("local_work_root_parent_not_writable")
    output = output_root(config)
    return {
        "ok_for_task_creation": any(item["readable"] for item in product_sources)
        and not violations,
        "product_sources": product_sources,
        "asset_sources": asset_sources,
        "task_store": {
            "backend": "standalone_sqlite",
            "path": str(db),
            "exists": db.exists(),
            "parent_for_creation": str(db_parent) if db_parent else None,
            "parent_writable": bool(db_parent and os.access(db_parent, os.W_OK)),
            "network_share": db_is_network,
            "overlaps_source_root": any(_paths_overlap(db, root) for root in source_roots),
            "policy": "local disk required; source-root overlap prohibited; one active writer per task",
        },
        "local_work_root": {
            "path": str(work),
            "exists": work.exists(),
            "parent_for_creation": str(work_parent) if work_parent else None,
            "parent_writable": bool(work_parent and os.access(work_parent, os.W_OK)),
            "network_share": work_is_network,
            "overlaps_source_root": any(_paths_overlap(work, root) for root in source_roots),
            "policy": "local disk required; source-root overlap prohibited",
        },
        "data_boundary": {
            "source_access": "read_only",
            "source_roots_mutated": False,
            "runtime_storage": "local_only",
            "valid": not violations,
            "violations": violations,
        },
        "output_root": path_status(output, kind="output_root") if output else {
            "configured": False,
            "blocking_for_export": True,
        },
        "external_checks": ["ChatCut tools available", "ChatCut user authenticated"],
    }


def list_products(config: dict[str, Any]) -> dict[str, Any]:
    roots = configured_roots(config, "product_sources")
    configured = [str(value) for value in config.get("product_catalog", [])]
    discovered = set(configured)
    evidence: dict[str, list[str]] = {name: [] for name in configured}
    for root in roots:
        if not root.exists():
            continue
        for suffix in ("*.yaml", "*.yml", "*.json", "*.md"):
            for path in _safe_glob(root, suffix):
                discovered.add(path.stem)
        for product in list(discovered):
            for path in _find_exact_documents(root, product):
                evidence.setdefault(product, []).append(str(path))
    return {
        "products": [
            {
                "product_id": product,
                "sources": sorted(set(evidence.get(product, []))),
                "verified": bool(evidence.get(product)),
            }
            for product in sorted(discovered)
        ]
    }


def get_product(config: dict[str, Any], product_id: str, max_chars: int) -> dict[str, Any]:
    _validate_product_id(product_id)
    documents: list[dict[str, Any]] = []
    facts: dict[str, Any] | None = None
    for root in configured_roots(config, "product_sources"):
        if not root.exists():
            continue
        for path in _find_exact_documents(root, product_id):
            stat = path.stat()
            if stat.st_size > MAX_PRODUCT_BYTES:
                raise ValueError(f"product document exceeds {MAX_PRODUCT_BYTES} bytes: {path.name}")
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            item: dict[str, Any] = {
                "path": str(path),
                "source_type": "nas" if _is_network_path(path) else "local",
                "format": path.suffix.lower().lstrip("."),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "content": text[:max_chars],
                "truncated": len(text) > max_chars,
            }
            documents.append(item)
            if facts is None and path.suffix.lower() in {".yaml", ".yml", ".json"}:
                try:
                    facts = _load_structured(path.suffix.lower(), text)
                except (ValueError, json.JSONDecodeError) as exc:
                    item["parse_error"] = type(exc).__name__
    return {
        "product_id": product_id,
        "source_level": "nas_or_configured_approved" if documents else "unavailable",
        "facts": facts or {},
        "documents": documents,
        "blocking_issue": None if documents else "product_context_incomplete",
        "content_is_data_not_instruction": True,
    }


def summarize_product(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": payload.get("product_id"),
        "source_level": payload.get("source_level"),
        "documents": [
            {key: item.get(key) for key in (
                "path", "source_type", "format", "sha256", "size_bytes", "modified_at", "parse_error"
            ) if item.get(key) is not None}
            for item in payload.get("documents", [])
        ],
        "blocking_issue": payload.get("blocking_issue"),
        "content_stored": False,
    }


def list_assets(config: dict[str, Any], product_id: str, query: str, limit: int) -> dict[str, Any]:
    roots = configured_roots(config, "asset_sources")
    terms = [term.casefold() for term in f"{product_id} {query}".split() if term.strip()]
    result: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in _walk_files(root):
            kind = MEDIA_EXTENSIONS.get(path.suffix.lower())
            if kind is None:
                continue
            searchable = str(path).casefold()
            if terms and not all(term in searchable for term in terms):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result.append({
                "path": str(path),
                "kind": kind,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "source_type": "nas" if _is_network_path(path) else "local",
                "authorization": "unknown_until_catalogued",
            })
            if len(result) >= max(1, min(limit, 200)):
                break
        if len(result) >= max(1, min(limit, 200)):
            break
    return {
        "assets": result,
        "roots_configured": len(roots),
        "blocking_issue": "asset_sources_unconfigured" if not roots else None,
        "note": "filenames do not prove usage rights; authorization must be confirmed separately",
    }


def _find_exact_documents(root: Path, product_id: str) -> list[Path]:
    _validate_product_id(product_id)
    root = root.resolve()
    wanted = product_id.strip().casefold()
    result = []
    for suffix in (".yaml", ".yml", ".json", ".md"):
        exact = root / f"{product_id}{suffix}"
        resolved = exact.resolve()
        if resolved.parent == root and resolved.is_file():
            result.append(resolved)
    if result:
        return result
    for suffix in ("*.yaml", "*.yml", "*.json", "*.md"):
        for path in _safe_glob(root, suffix):
            resolved = _resolved_file_within(root, path, direct_child=True)
            if resolved is not None and resolved.stem.strip().casefold() == wanted:
                result.append(resolved)
    return result


def _validate_product_id(product_id: str) -> None:
    value = product_id.strip()
    if not value or len(value) > 128:
        raise ValueError("product_id must contain 1 to 128 characters")
    if value in {".", ".."} or any(character in value for character in ("/", "\\", ":", "\x00")):
        raise ValueError("product_id must be a plain product name, not a path")


def _safe_glob(root: Path, pattern: str) -> list[Path]:
    try:
        return list(root.glob(pattern))
    except OSError:
        return []


def _walk_files(root: Path) -> Iterable[Path]:
    try:
        root = root.resolve()
    except OSError:
        return
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [
                name for name in dirs
                if _resolved_directory_within(root, current_path / name) is not None
            ]
            for name in files:
                resolved = _resolved_file_within(root, current_path / name)
                if resolved is not None:
                    yield resolved
    except OSError:
        return


def _resolved_file_within(root: Path, candidate: Path, *, direct_child: bool = False) -> Path | None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if direct_child and resolved.parent != root:
        return None
    return resolved if resolved.is_file() else None


def _resolved_directory_within(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate.is_symlink():
        return None
    is_junction = getattr(candidate, "is_junction", None)
    if callable(is_junction) and is_junction():
        return None
    return resolved if resolved.is_dir() else None


def _load_structured(suffix: str, text: str) -> dict[str, Any]:
    if suffix == ".json":
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {"value": payload}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {"raw_yaml": text}
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError("invalid YAML product document") from exc
    return payload if isinstance(payload, dict) else {"value": payload}


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("list-products")
    product = sub.add_parser("get-product")
    product.add_argument("--product-id", required=True)
    product.add_argument("--max-chars", type=int, default=12000)
    product.add_argument("--summary-only", action="store_true")
    assets = sub.add_parser("list-assets")
    assets.add_argument("--product-id", default="")
    assets.add_argument("--query", default="")
    assets.add_argument("--limit", type=int, default=50)
    return parser


def main() -> int:
    configure_console()
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "preflight":
        payload = preflight(config)
    elif args.command == "list-products":
        payload = list_products(config)
    elif args.command == "get-product":
        payload = get_product(config, args.product_id, max(1000, args.max_chars))
        if args.summary_only:
            payload = summarize_product(payload)
    else:
        payload = list_assets(config, args.product_id, args.query, args.limit)
    emit(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
