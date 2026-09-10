#!/usr/bin/env python3
"""Create, discover, and validate workspace-local rough-cut profiles."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


PROFILE_CANDIDATES = (
    Path(".codex/auto-rough-cut.json"),
    Path("自動粗剪設定.json"),
)
TRACK_RE = re.compile(r"^[VA][1-9][0-9]*$")
AUDIO_LAYOUTS = {
    "auto",
    "full_mix_single_protagonist",
    "separate_speakers",
    "mixed_multitrack",
}
AUDIO_ROLES = {"protagonist", "director", "staff", "music", "mixed", "unknown"}
DIRECTOR_POLICIES = {"semantic_invalid_only", "always_review", "profile_defined"}


class ClientProfileError(ValueError):
    """Raised when a profile is missing required safety or routing fields."""


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClientProfileError(f"{field} must be an object")
    return value


def _known_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ClientProfileError(
            f"{field} contains unknown fields: {', '.join(unknown)}"
        )


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ClientProfileError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ClientProfileError(f"{field} cannot be empty")
    return normalized


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ClientProfileError(f"{field} must be a list")
    result = [_string(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise ClientProfileError(f"{field} cannot contain duplicates")
    return result


def _track_list(value: Any, field: str) -> list[str]:
    tracks = [item.upper() for item in _string_list(value, field)]
    invalid = [track for track in tracks if not TRACK_RE.fullmatch(track)]
    if invalid:
        raise ClientProfileError(
            f"{field} contains invalid Premiere tracks: {', '.join(invalid)}"
        )
    if len(tracks) != len(set(tracks)):
        raise ClientProfileError(f"{field} cannot contain duplicates")
    return tracks


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized profile or raise a precise validation error."""

    profile = _object(profile, "profile")
    _known_keys(
        profile,
        {
            "schema_version",
            "profile_id",
            "client",
            "series",
            "premiere",
            "audio",
            "classification",
            "vocabulary",
            "notes",
        },
        "profile",
    )
    if profile.get("schema_version") != 1:
        raise ClientProfileError("schema_version must be 1")

    normalized: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": _string(profile.get("profile_id"), "profile_id"),
    }

    client = _object(profile.get("client"), "client")
    _known_keys(client, {"id", "name"}, "client")
    normalized["client"] = {
        "id": _string(client.get("id"), "client.id"),
        "name": _string(client.get("name"), "client.name"),
    }

    series_value = profile.get("series")
    if series_value is not None:
        series = _object(series_value, "series")
        _known_keys(series, {"id", "name"}, "series")
        normalized["series"] = {
            "id": _string(series.get("id"), "series.id"),
            "name": _string(series.get("name"), "series.name"),
        }

    premiere = _object(profile.get("premiere"), "premiere")
    _known_keys(
        premiere,
        {
            "target_tracks",
            "background_music_tracks",
            "red_label",
            "yellow_label",
            "handle_frames",
            "output_suffix",
        },
        "premiere",
    )
    target_tracks = _track_list(premiere.get("target_tracks"), "premiere.target_tracks")
    if not target_tracks:
        raise ClientProfileError("premiere.target_tracks cannot be empty")
    music_tracks = _track_list(
        premiere.get("background_music_tracks", []),
        "premiere.background_music_tracks",
    )
    overlap = sorted(set(target_tracks).intersection(music_tracks))
    if overlap:
        raise ClientProfileError(
            "target tracks and background-music tracks overlap: " + ", ".join(overlap)
        )
    handle_frames = premiere.get("handle_frames")
    if isinstance(handle_frames, bool) or not isinstance(handle_frames, int):
        raise ClientProfileError("premiere.handle_frames must be an integer")
    if not 0 <= handle_frames <= 30:
        raise ClientProfileError("premiere.handle_frames must be between 0 and 30")
    normalized["premiere"] = {
        "target_tracks": target_tracks,
        "background_music_tracks": music_tracks,
        "red_label": _string(premiere.get("red_label"), "premiere.red_label"),
        "yellow_label": _string(
            premiere.get("yellow_label"), "premiere.yellow_label"
        ),
        "handle_frames": handle_frames,
        "output_suffix": _string(
            premiere.get("output_suffix"), "premiere.output_suffix"
        ),
    }

    audio = _object(profile.get("audio"), "audio")
    _known_keys(audio, {"layout", "tracks", "silence_seconds"}, "audio")
    layout = _string(audio.get("layout"), "audio.layout")
    if layout not in AUDIO_LAYOUTS:
        raise ClientProfileError(
            "audio.layout must be one of: " + ", ".join(sorted(AUDIO_LAYOUTS))
        )
    silence_seconds = audio.get("silence_seconds")
    if (
        isinstance(silence_seconds, bool)
        or not isinstance(silence_seconds, (int, float))
        or not 0.2 <= float(silence_seconds) <= 30.0
    ):
        raise ClientProfileError("audio.silence_seconds must be between 0.2 and 30")
    track_rules_value = audio.get("tracks", [])
    if not isinstance(track_rules_value, list):
        raise ClientProfileError("audio.tracks must be a list")
    track_rules: list[dict[str, Any]] = []
    for index, item in enumerate(track_rules_value):
        rule = _object(item, f"audio.tracks[{index}]")
        _known_keys(
            rule,
            {"pattern", "role", "speaker", "timing_authority"},
            f"audio.tracks[{index}]",
        )
        role = _string(rule.get("role"), f"audio.tracks[{index}].role")
        if role not in AUDIO_ROLES:
            raise ClientProfileError(
                f"audio.tracks[{index}].role must be one of: "
                + ", ".join(sorted(AUDIO_ROLES))
            )
        timing_authority = rule.get("timing_authority", False)
        if not isinstance(timing_authority, bool):
            raise ClientProfileError(
                f"audio.tracks[{index}].timing_authority must be boolean"
            )
        normalized_rule: dict[str, Any] = {
            "pattern": _string(rule.get("pattern"), f"audio.tracks[{index}].pattern"),
            "role": role,
            "timing_authority": timing_authority,
        }
        if "speaker" in rule:
            normalized_rule["speaker"] = _string(
                rule["speaker"], f"audio.tracks[{index}].speaker"
            )
        track_rules.append(normalized_rule)
    normalized["audio"] = {
        "layout": layout,
        "tracks": track_rules,
        "silence_seconds": float(silence_seconds),
    }

    classification = _object(profile.get("classification"), "classification")
    _known_keys(
        classification,
        {"director_policy", "red_signals", "yellow_signals", "preserve_signals"},
        "classification",
    )
    director_policy = _string(
        classification.get("director_policy"), "classification.director_policy"
    )
    if director_policy not in DIRECTOR_POLICIES:
        raise ClientProfileError(
            "classification.director_policy must be one of: "
            + ", ".join(sorted(DIRECTOR_POLICIES))
        )
    normalized["classification"] = {
        "director_policy": director_policy,
        "red_signals": _string_list(
            classification.get("red_signals", []), "classification.red_signals"
        ),
        "yellow_signals": _string_list(
            classification.get("yellow_signals", []), "classification.yellow_signals"
        ),
        "preserve_signals": _string_list(
            classification.get("preserve_signals", []),
            "classification.preserve_signals",
        ),
    }

    vocabulary = _object(profile.get("vocabulary", {}), "vocabulary")
    _known_keys(vocabulary, {"confirmed_terms"}, "vocabulary")
    normalized["vocabulary"] = {
        "confirmed_terms": _string_list(
            vocabulary.get("confirmed_terms", []), "vocabulary.confirmed_terms"
        )
    }
    normalized["notes"] = _string_list(profile.get("notes", []), "notes")
    return normalized


def load_profile(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientProfileError(f"cannot read profile {path}: {exc}") from exc
    return validate_profile(data)


def resolve_profile(start: Path, workspace_root: Path) -> Path | None:
    start = start.resolve()
    if start.is_file():
        start = start.parent
    workspace_root = workspace_root.resolve()
    try:
        start.relative_to(workspace_root)
    except ValueError as exc:
        raise ClientProfileError(
            f"start path {start} is outside workspace root {workspace_root}"
        ) from exc

    current = start
    while True:
        matches = [current / candidate for candidate in PROFILE_CANDIDATES if (current / candidate).is_file()]
        if len(matches) > 1:
            raise ClientProfileError(
                "multiple rough-cut profiles exist at the same level: "
                + ", ".join(str(path) for path in matches)
            )
        if matches:
            return matches[0]
        if current == workspace_root:
            return None
        current = current.parent


def template_profile(
    *, profile_id: str, client_id: str, client_name: str, series_id: str | None, series_name: str | None
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": profile_id,
        "client": {"id": client_id, "name": client_name},
        "premiere": {
            "target_tracks": ["V1", "A1", "A2"],
            "background_music_tracks": [],
            "red_label": "Rose",
            "yellow_label": "Mango",
            "handle_frames": 3,
            "output_suffix": "_自動粗剪",
        },
        "audio": {
            "layout": "auto",
            "tracks": [],
            "silence_seconds": 2.0,
        },
        "classification": {
            "director_policy": "semantic_invalid_only",
            "red_signals": [
                "明確重來或不要這段",
                "倒數與現場調度",
                "口誤後已有完整重講",
                "被新版本取代的重複句",
            ],
            "yellow_signals": ["說話功能或替代關係不確定"],
            "preserve_signals": ["主角有效內容", "有效提問或補充"],
        },
        "vocabulary": {"confirmed_terms": []},
        "notes": [],
    }
    if series_id is not None or series_name is not None:
        if not series_id or not series_name:
            raise ClientProfileError(
                "series-id and series-name must be supplied together"
            )
        profile["series"] = {"id": series_id, "name": series_name}
    return validate_profile(profile)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ClientProfileError(f"profile already exists: {path}")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, discover, and validate workspace rough-cut profiles."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate one profile")
    validate.add_argument("profile", type=Path)

    resolve = subparsers.add_parser(
        "resolve", help="find the nearest profile without crossing the workspace root"
    )
    resolve.add_argument("--start", required=True, type=Path)
    resolve.add_argument("--workspace-root", required=True, type=Path)

    initialize = subparsers.add_parser("init", help="create a safe profile template")
    initialize.add_argument("--path", required=True, type=Path)
    initialize.add_argument("--profile-id", required=True)
    initialize.add_argument("--client-id", required=True)
    initialize.add_argument("--client-name", required=True)
    initialize.add_argument("--series-id")
    initialize.add_argument("--series-name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            profile = load_profile(args.profile)
            print(
                json.dumps(
                    {"valid": True, "path": str(args.profile.resolve()), "profile": profile},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "resolve":
            path = resolve_profile(args.start, args.workspace_root)
            result: dict[str, Any] = {"found": path is not None}
            if path is not None:
                result["path"] = str(path)
                result["profile"] = load_profile(path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "init":
            profile = template_profile(
                profile_id=args.profile_id,
                client_id=args.client_id,
                client_name=args.client_name,
                series_id=args.series_id,
                series_name=args.series_name,
            )
            _atomic_write_json(args.path, profile)
            print(
                json.dumps(
                    {"created": str(args.path), "profile": profile},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        raise ClientProfileError(f"unsupported command: {args.command}")
    except (OSError, ClientProfileError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
