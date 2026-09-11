#!/usr/bin/env python3
"""Consume one confirmed upstream editing case; never select or classify clients."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTENT_TYPES = ("short_form", "long_form")
REQUIRED_ROLES = {"final_srt", "review_srt", "ai_baseline", "report"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected JSON object: {path}")
    return value


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def nonempty(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"Missing {label}")
    return value


def inside(root, relative):
    nonempty(relative, "relative path")
    p, w = PurePosixPath(relative), PureWindowsPath(relative)
    require(not p.is_absolute() and not w.is_absolute() and not w.drive
            and ".." not in p.parts and ".." not in w.parts and "\\" not in relative,
            "Use portable relative paths without traversal or drive letters")
    target = (root / relative).resolve()
    require(target.is_relative_to(root.resolve()), "Path escapes its selected root")
    return target


def ref_path(ref, roots, *, mutable=False):
    require(isinstance(ref, dict), "Path reference must contain root and path")
    require(ref.get("root") in roots, "Missing device root binding")
    target = inside(roots[ref["root"]], ref.get("path"))
    if mutable:
        require(not target.is_relative_to(SKILL_ROOT.parent),
                "Mutable workspace data must not be stored in installed skills")
        require(ref["root"] != "skill", "Skill root is read-only")
    return target


def index_rows(rows, key):
    require(isinstance(rows, list), f"{key} index must be an array")
    result = {}
    for row in rows:
        require(isinstance(row, dict), f"Invalid {key} index row")
        name = nonempty(row.get(key), key)
        require(name not in result, f"Duplicate {key}: {name}")
        result[name] = row
    return result


def load_roots(workspace, bindings=None):
    root = Path(workspace).expanduser().resolve()
    supplied = read_object(bindings) if bindings else {}
    require(not {"workspace", "skill"} & supplied.keys(), "Reserved root binding")
    roots = {"workspace": root, "skill": SKILL_ROOT}
    for key, value in supplied.items():
        p = Path(nonempty(value, "binding path")).expanduser()
        require(p.is_absolute(), "Device root bindings must be absolute")
        roots[nonempty(key, "binding name")] = p.resolve()
    return roots, supplied


def upstream_checker():
    # Reuse identity/confirmation/source checks, not the catalog or layer selector.
    path = SKILL_ROOT.parent / "auto-edit/scripts/case_context.py"
    require(path.is_file(), "The auto-edit case checker is required for case handoff")
    spec = importlib.util.spec_from_file_location("subtitle_upstream_case", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def effective_profile(profile, content_type):
    result = copy.deepcopy(profile)
    preferences = result.get("subtitle_preferences", {})
    require(isinstance(preferences, dict), "subtitle_preferences must be an object")
    overrides = result.get("subtitle_preferences_by_content_type", {})
    require(isinstance(overrides, dict) and all(k in CONTENT_TYPES for k in overrides),
            "Invalid subtitle_preferences_by_content_type")
    for value in overrides.values():
        require(isinstance(value, dict), "Content type preferences must be an object")
    result["subtitle_preferences"] = {**preferences, **overrides.get(content_type, {})}
    return result


def resolve_context(workspace, context_path, bindings=None):
    roots, supplied = load_roots(workspace, bindings)
    manifest_path = Path(context_path).expanduser().resolve()
    require(not manifest_path.is_relative_to(SKILL_ROOT.parent),
            "Subtitle job context belongs in the private project, not installed skills")
    local = read_object(manifest_path)
    require(type(local.get("schema_version")) is int and local["schema_version"] == 1,
            "Unsupported subtitle context schema")
    upstream_ref = local.get("upstream_case")
    case_path = ref_path(upstream_ref, roots)
    require(digest(case_path) == upstream_ref.get("sha256"),
            "Upstream case changed; review and rebind this subtitle context")
    case = read_object(case_path)
    checker = upstream_checker()
    checker.identity(case)
    checker.confirmed(case)  # Verifies the existing answer; never prompts or classifies.
    require(case["case_id"] == local.get("case_id"), "Subtitle context belongs to a different case")
    require("text" in case["scope"]["stages"], "Upstream case does not include the text stage")
    settings = case.get("subtitle")
    require(isinstance(settings, dict), "Upstream case must supply subtitle settings; do not select a client again")
    job_id = nonempty(settings.get("job_id"), "upstream subtitle job_id")
    require(not any(c in job_id for c in '<>:"/\\|?*') and job_id.strip(" .-") == job_id,
            "job_id must be a portable filename component")
    require(local.get("job_id") == job_id, "Subtitle job ID conflicts with upstream handoff")
    # Identity and routing are inherited. Legacy duplicate fields may only agree.
    kind = settings.get("subtitle_content_type")
    require(kind is None or kind in CONTENT_TYPES, "Invalid upstream subtitle_content_type")
    series_id = settings.get("series_id")
    if series_id is not None:
        nonempty(series_id, "upstream series_id")
    inherited = {k: case[k] for k in ("client_id", "type_id", "episode_id", "version")}
    inherited.update(series_id=series_id, subtitle_content_type=kind)
    for key, value in inherited.items():
        if key in local:
            require(local[key] == value, f"Local {key} conflicts with upstream handoff")
    for forbidden in ("profile", "delivery", "learning_registry", "series", "clients"):
        require(forbidden not in local, f"{forbidden} must come from upstream, not a second subtitle index")
    profile_ref = settings.get("profile")
    profile_path = None
    profile_hash = None
    if profile_ref is None:
        profile = {"series_id": series_id}
    else:
        require(series_id is not None, "Upstream series_id required for a supplied profile")
        profile_path = ref_path(profile_ref, roots)
        profile_hash = digest(profile_path)
        require(profile_hash == profile_ref.get("sha256"), "Upstream profile pin changed")
        profile = read_object(profile_path)
        require(profile.get("series_id") == series_id, "Profile/series identity mismatch")
        for key in ("client_id", "type_id"):
            if key in profile:
                require(profile[key] == case[key], f"Profile/{key} mismatch")
    profile = effective_profile(profile, kind)
    # Verify the upstream whitelist, without consulting the client catalog.
    checker.verify_sources(case, roots["workspace"], supplied)
    available = index_rows(case["sources"], "id")
    source_ids = local.get("source_ids")
    require(isinstance(source_ids, list) and bool(source_ids)
            and all(isinstance(s, str) for s in source_ids)
            and len(set(source_ids)) == len(source_ids), "Declare unique upstream source_ids")
    verified = {}
    for sid in source_ids:
        require(sid in available, f"Source is not in the upstream whitelist: {sid}")
        source = available[sid]
        verified[sid] = {**source, "resolved_path": str(ref_path(source, roots))}
    edit_versions = {s["version"] for s in verified.values()
                     if s["role"] in ("audio", "media", "transcript", "timeline")}
    require(len(edit_versions) == 1, "Selected source edit versions do not match")
    require(any(s["role"] in ("audio", "media") for s in verified.values()),
            "Matching source audio/video is required")
    decisions_path = ref_path(local.get("decisions"), roots, mutable=True)
    decisions = read_object(decisions_path)
    expected = {**inherited, "job_id": job_id, "case_id": case["case_id"]}
    for key, value in expected.items():
        if key in decisions:
            require(decisions[key] == value, f"Decisions {key} mismatch")
    require(digest(decisions_path) == local["decisions"].get("sha256"),
            "Decisions changed; review and register the new hash without discarding locks")
    evidence_dir = ref_path(local.get("evidence_dir"), roots, mutable=True)
    require(evidence_dir != decisions_path and evidence_dir != manifest_path,
            "Evidence directory conflicts with a job file")
    legacy_path = None
    if "legacy_job" in local:
        legacy_path = ref_path(local["legacy_job"], roots)
        require(digest(legacy_path) == local["legacy_job"].get("sha256"), "Legacy job version changed")
        legacy = read_object(legacy_path)
        for key, value in expected.items():
            if key in legacy:
                require(legacy[key] == value, f"Legacy job {key} mismatch")
    registry = ref_path(settings.get("learning_registry", {
        "root": "workspace", "path": ".subtitles/learning-registry.json"}), roots, mutable=True)
    if registry.exists():
        read_object(registry)
    delivery = settings.get("delivery")
    require(isinstance(delivery, dict), "Upstream must supply the confirmed project delivery destination")
    roles = delivery.get("required_roles", [])
    require(isinstance(roles, list) and all(isinstance(r, str) and r.strip() for r in roles),
            "required_roles must be an array of role names")
    delivery_dir = ref_path(delivery.get("directory"), roots, mutable=True)
    job = {**local, **inherited}
    return {
        "job": job, "manifest_path": manifest_path, "case_path": case_path,
        "profile_path": profile_path, "profile": profile, "profile_sha256": profile_hash,
        "decisions_path": decisions_path, "evidence_dir": evidence_dir, "legacy_path": legacy_path,
        "learning_registry": registry, "delivery_dir": delivery_dir,
        "required_roles": REQUIRED_ROLES | set(roles), "sources": verified,
        "content_rules": (SKILL_ROOT / "references/content-types" / f"{kind.replace('_', '-')}.md") if kind else None,
        "upstream_sha256": upstream_ref["sha256"],
        "warnings": [] if kind else ["Subtitle format not supplied upstream; use common rules without guessing."],
    }


def check_delivery(context):
    job, directory = context["job"], context["delivery_dir"]
    rows = index_rows(job.get("deliverables"), "role")
    require(context["required_roles"] <= rows.keys(), "Missing required delivery roles")
    checked, paths = {}, set()
    protected = {Path(s["resolved_path"]) for s in context["sources"].values()}
    protected |= {context["manifest_path"], context["case_path"], context["decisions_path"],
                  context["profile_path"], context["legacy_path"]}
    for role, row in rows.items():
        path = inside(directory, row.get("path"))
        require(path not in paths and path not in protected, "Delivery path duplicates or overwrites evidence")
        require(row.get("job_id") == job["job_id"], "Cross-job delivery rejected")
        require(path.is_file() and path.stat().st_size > 0, f"Missing/empty delivery: {role}")
        require(digest(path) == row.get("sha256"), f"Delivery changed: {role}")
        paths.add(path)
        checked[role] = str(path)
    return checked


def summary(context):
    job = context["job"]
    return {"status": "upstream-handoff-verified", "content_quality_verified": False,
            **{k: job[k] for k in ("case_id", "job_id", "client_id", "series_id", "episode_id", "version", "subtitle_content_type")},
            "type_id": job.get("type_id"),
            **{k: str(context[k]) if context[k] is not None else None for k in ("manifest_path", "case_path", "profile_path", "decisions_path",
                                             "evidence_dir", "learning_registry", "delivery_dir", "content_rules")},
            "required_roles": sorted(context["required_roles"]),
            "subtitle_preferences": context["profile"].get("subtitle_preferences", {}),
            "source_ids": list(context["sources"]),
            "warnings": context["warnings"],
            "pins": {"upstream_case_sha256": context["upstream_sha256"],
                     "manifest_sha256": digest(context["manifest_path"]),
                     "profile_sha256": context["profile_sha256"],
                     "decisions_sha256": job["decisions"]["sha256"]}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("check", "delivery"))
    p.add_argument("context", type=Path)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--bindings", type=Path)
    args = p.parse_args()
    try:
        context = resolve_context(args.workspace, args.context, args.bindings)
        result = summary(context)
        if args.command == "delivery":
            result["deliverables"] = check_delivery(context)
            result["status"] = "delivery-files-verified"
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
