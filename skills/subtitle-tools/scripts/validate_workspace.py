#!/usr/bin/env python3
"""Validate a shared subtitle-tools workspace and its client/job isolation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing file: {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON: {path}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"JSON root must be an object: {path}")
        return None
    return value


def resolve_inside(root: Path, relative: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        errors.append(f"{label} must be a non-empty relative path")
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        errors.append(f"{label} escapes workspace: {relative}")
        return None
    return candidate


def unique_text(
    value: object,
    label: str,
    seen: dict[str, str],
    owner: str,
    errors: list[str],
) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} for {owner} must be non-empty text")
        return
    previous = seen.get(value)
    if previous is not None and previous != owner:
        errors.append(f"duplicate {label} {value!r}: {previous} and {owner}")
        return
    seen[value] = owner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()

    root = Path(args.workspace).resolve()
    errors: list[str] = []
    config_path = root / ".subtitle-tools-workspace.json"
    config = read_json(config_path, errors)
    if config is None:
        return report(errors, [], 0, 0, 0)

    if config.get("schema_version") != 3:
        errors.append("schema_version must be 3")
    if config.get("workspace_kind") != "shared-subtitle-tools":
        errors.append("workspace_kind must be shared-subtitle-tools")
    if config.get("default_series") is not None:
        errors.append("shared workspace default_series must be null")
    for forbidden in ("series_id", "series_name", "series_profile_file"):
        if forbidden in config:
            errors.append(f"shared workspace must not define top-level {forbidden}")

    compatibility_path = root / ".codex" / "subtitle-workspace.json"
    if compatibility_path.exists():
        compatibility = read_json(compatibility_path, errors)
        if compatibility is not None and compatibility != config:
            errors.append(".codex/subtitle-workspace.json differs from canonical workspace config")

    content_rows = config.get("content_type_profiles")
    if not isinstance(content_rows, list):
        errors.append("content_type_profiles must be a list")
        content_rows = []

    content_types: dict[str, dict[str, Any]] = {}
    content_aliases: dict[str, str] = {}
    for index, row in enumerate(content_rows):
        owner = f"content_type_profiles[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{owner} must be an object")
            continue
        content_id = row.get("content_type")
        unique_text(content_id, "content_type", content_aliases, owner, errors)
        if not isinstance(content_id, str) or not content_id:
            continue
        if content_id in content_types:
            errors.append(f"duplicate content_type: {content_id}")
            continue
        content_types[content_id] = row
        unique_text(row.get("display_name"), "content display name", content_aliases, content_id, errors)
        for alias in row.get("aliases", []):
            unique_text(alias, "content alias", content_aliases, content_id, errors)
        unique_text(row.get("jobs_folder"), "jobs folder", {}, content_id, errors)

    if set(content_types) != {"short_form", "long_form"}:
        errors.append("content types must be exactly short_form and long_form")

    client_rows = config.get("client_profiles")
    if not isinstance(client_rows, list):
        errors.append("client_profiles must be a list")
        client_rows = []

    clients: list[tuple[dict[str, Any], Path]] = []
    client_ids: dict[str, str] = {}
    client_names: dict[str, str] = {}
    client_aliases: dict[str, str] = {}
    expected_jobs_roots: set[Path] = set()

    for index, row in enumerate(client_rows):
        owner = f"client_profiles[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{owner} must be an object")
            continue
        series_id = row.get("series_id")
        series_name = row.get("series_name")
        unique_text(series_id, "series_id", client_ids, owner, errors)
        unique_text(series_name, "series_name", client_names, owner, errors)
        if isinstance(series_id, str):
            unique_text(series_id, "client alias", client_aliases, series_id, errors)
        if isinstance(series_name, str):
            unique_text(series_name, "client alias", client_aliases, str(series_id), errors)
        for alias in row.get("series_aliases", []):
            unique_text(alias, "client alias", client_aliases, str(series_id), errors)

        profile_path = resolve_inside(root, row.get("series_profile_file"), f"{owner}.series_profile_file", errors)
        jobs_root = resolve_inside(root, row.get("jobs_root"), f"{owner}.jobs_root", errors)
        if profile_path is None or jobs_root is None:
            continue
        if not str(profile_path.relative_to(root)).startswith("clients/"):
            errors.append(f"client profile must be under clients/: {profile_path}")
        if not str(jobs_root.relative_to(root)).startswith("jobs/"):
            errors.append(f"jobs_root must be under jobs/: {jobs_root}")
        expected_jobs_roots.add(jobs_root)

        profile = read_json(profile_path, errors)
        if profile is not None:
            if profile.get("series_id") != series_id:
                errors.append(f"profile series_id mismatch: {profile_path}")
            if profile.get("series_name") != series_name:
                errors.append(f"profile series_name mismatch: {profile_path}")
        clients.append((row, jobs_root))

    registry_path = resolve_inside(root, config.get("learning_registry_file"), "learning_registry_file", errors)
    if registry_path is not None:
        read_json(registry_path, errors)

    jobs_root_path = root / "jobs"
    if jobs_root_path.exists():
        unexpected = {
            item.resolve()
            for item in jobs_root_path.iterdir()
            if not item.name.startswith(".") and item.resolve() not in expected_jobs_roots
        }
        for item in sorted(unexpected):
            errors.append(f"job entry is outside a registered client folder: {item}")

    job_count = 0
    for client, jobs_root in clients:
        series_id = client.get("series_id")
        for content_id, content in content_types.items():
            folder_name = content.get("jobs_folder")
            if not isinstance(folder_name, str):
                continue
            content_root = jobs_root / folder_name
            if not content_root.is_dir():
                errors.append(f"missing jobs folder: {content_root}")
                continue
            for job_dir in sorted(content_root.iterdir()):
                if job_dir.name.startswith("."):
                    continue
                if not job_dir.is_dir():
                    errors.append(f"job entry must be a directory: {job_dir}")
                    continue
                job_count += 1
                if (job_dir / "series-profile.json").exists():
                    errors.append(f"rename mutable-looking job profile to series-profile-snapshot.json: {job_dir}")
                job_files = list(job_dir.glob("*_job.json"))
                if len(job_files) != 1:
                    errors.append(f"job must contain exactly one *_job.json: {job_dir}")
                    continue
                job = read_json(job_files[0], errors)
                if job is None:
                    continue
                if job.get("series_id") != series_id:
                    errors.append(f"job/client series_id mismatch: {job_files[0]}")
                if job.get("content_type") != content_id:
                    errors.append(f"job/content_type mismatch: {job_files[0]}")

    return report(errors, [], len(clients), len(content_types), job_count)


def report(
    errors: list[str],
    warnings: list[str],
    client_count: int,
    content_count: int,
    job_count: int,
) -> int:
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"FAILED: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print(
        "OK: "
        f"{client_count} client(s), {content_count} content type(s), "
        f"{job_count} isolated job(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
