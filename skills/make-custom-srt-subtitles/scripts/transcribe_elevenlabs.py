#!/usr/bin/env python3
"""Create immutable ElevenLabs Scribe v2 evidence for one subtitle job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MODEL_ID = "scribe_v2"
DEFAULT_LANGUAGE_CODE = "zho"
PROFILE_DIRECTORY = Path(__file__).parent.parent / "references" / "series-profiles"
UNSUPPORTED_KEYTERM_CHARACTERS = re.compile(r"[<>{}\[\]\\]")
WINDOWS_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe one audio or video source with ElevenLabs Scribe v2."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--mode", choices=("single", "diarized", "multichannel"), required=True
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--context", type=Path, help="Subtitle context referencing the confirmed upstream case")
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--profile", help="Series id, alias, file stem, or profile JSON path")
    parser.add_argument("--job-id")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--language-code", default=DEFAULT_LANGUAGE_CODE)
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--keyterm", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_env_file() -> Path:
    if os.name == "nt":
        base = os.getenv("APPDATA")
        if base:
            return Path(base) / "Chuangfei" / "Subtitles" / "elevenlabs.env"
        return Path.home() / "AppData" / "Roaming" / "Chuangfei" / "Subtitles" / "elevenlabs.env"
    base = os.getenv("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "chuangfei" / "subtitles" / "elevenlabs.env"
    return Path.home() / ".config" / "chuangfei" / "subtitles" / "elevenlabs.env"


def load_api_key(explicit_env_file: Path | None) -> tuple[str, str]:
    process_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if process_key:
        return process_key, "process_environment"

    env_path = explicit_env_file.expanduser() if explicit_env_file else default_env_file()
    if explicit_env_file and not env_path.is_file():
        raise ValueError(f"Explicit env file does not exist: {env_path}")
    if not env_path.is_file():
        raise ValueError(
            "ELEVENLABS_API_KEY is not set and the private ElevenLabs env file is missing"
        )
    try:
        from dotenv import dotenv_values
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "python-dotenv is not installed; install the pinned portable requirements"
        ) from error
    key = str(dotenv_values(env_path).get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise ValueError(f"ELEVENLABS_API_KEY is missing from: {env_path}")
    source = "explicit_env_file" if explicit_env_file else "default_private_env_file"
    return key, source


def read_profile(profile_value: str | None) -> dict[str, Any] | None:
    if not profile_value:
        return None
    candidate = Path(profile_value).expanduser()
    if candidate.is_file():
        paths = [candidate]
    elif candidate.exists():
        raise ValueError(f"Profile is not a file: {candidate}")
    else:
        paths = sorted(PROFILE_DIRECTORY.glob("*.json"))
    matches: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Series profile must be a JSON object: {path}")
        if candidate.is_file():
            return data
        names = {
            path.stem,
            str(data.get("series_id") or ""),
            str(data.get("series_name") or ""),
            *(str(item) for item in data.get("aliases", []) if item),
        }
        if profile_value in names:
            matches.append(data)
    if not matches:
        raise ValueError(f"No series profile matches: {profile_value}")
    if len(matches) > 1:
        raise ValueError(f"Multiple series profiles match: {profile_value}")
    return matches[0]


def profile_keyterms(profile: dict[str, Any] | None) -> list[str]:
    if profile is None:
        return []
    # A series dictionary can contain past guests/products. Only an explicit
    # stable roster should bias a new recording when this field is present.
    if "transcription_keyterms" in profile:
        roster = profile["transcription_keyterms"]
        if not isinstance(roster, list) or not all(isinstance(term, str) for term in roster):
            raise ValueError("transcription_keyterms must be an array of strings")
        return roster
    values: list[str] = [str(item) for item in profile.get("confirmed_terms", [])]
    misrecognitions = profile.get("common_misrecognitions", {})
    if isinstance(misrecognitions, dict):
        values.extend(str(item) for item in misrecognitions.values())
    return values


def normalize_keyterms(values: list[str]) -> list[str]:
    unique: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value or value in unique:
            continue
        if len(value) >= 50:
            raise ValueError(f"Keyterm must be shorter than 50 characters: {value!r}")
        if len(value.split()) > 5:
            raise ValueError(f"Keyterm must contain at most five words: {value!r}")
        if UNSUPPORTED_KEYTERM_CHARACTERS.search(value):
            raise ValueError(f"Keyterm contains an unsupported character: {value!r}")
        unique.append(value)
    if len(unique) > 1000:
        raise ValueError("ElevenLabs accepts at most 1000 keyterms")
    return unique


def build_parameters(
    mode: str,
    language_code: str,
    num_speakers: int | None,
    keyterms: list[str],
) -> dict[str, Any]:
    if num_speakers is not None and not 1 <= num_speakers <= 32:
        raise ValueError("--num-speakers must be between 1 and 32")
    if mode != "diarized" and num_speakers is not None:
        raise ValueError("--num-speakers is only valid with --mode diarized")
    parameters: dict[str, Any] = {
        "model_id": MODEL_ID,
        "language_code": language_code,
        "no_verbatim": False,
        "timestamps_granularity": "character",
        "tag_audio_events": True,
        "temperature": 0,
    }
    if mode == "single":
        parameters.update({"diarize": False, "use_multi_channel": False})
    elif mode == "diarized":
        parameters.update({"diarize": True, "use_multi_channel": False})
        if num_speakers is not None:
            parameters["num_speakers"] = num_speakers
    elif mode == "multichannel":
        parameters.update(
            {
                "diarize": False,
                "use_multi_channel": True,
                "multichannel_output_style": "separate",
            }
        )
    else:
        raise ValueError(f"Unsupported mode: {mode}")
    if keyterms:
        parameters["keyterms"] = keyterms
    return parameters


def safe_job_id(value: str) -> str:
    cleaned = WINDOWS_INVALID_FILENAME_CHARACTERS.sub("-", value.strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-")
    if not cleaned:
        raise ValueError("job id is empty after filename sanitization")
    return cleaned


def serialize_response(response: object) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        value = response.model_dump(mode="json")
    elif hasattr(response, "dict"):
        value = response.dict()
    else:
        raise TypeError(f"Cannot serialize ElevenLabs response: {type(response)!r}")
    if not isinstance(value, dict):
        raise TypeError("Serialized ElevenLabs response must be a JSON object")
    return value


def create_client(api_key: str) -> object:
    try:
        from elevenlabs.client import ElevenLabs
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "elevenlabs is not installed; install the pinned portable requirements"
        ) from error
    return ElevenLabs(api_key=api_key)


def write_json_exclusive(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def prepare_request(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Source media does not exist: {source}")
    context = None
    if args.workspace:
        from subtitle_context import resolve_context
        if not args.context:
            raise ValueError("--workspace requires --context")
        context = resolve_context(args.workspace, args.context, args.bindings)
        if args.profile:
            raise ValueError("Use the upstream profile with --workspace; do not pass --profile")
        profile = context["profile"]
        matching = [s for s in context["sources"].values()
                    if Path(s["resolved_path"]) == source and s["role"] in ("media", "audio")]
        if len(matching) != 1:
            raise ValueError("Transcription source is not this job's registered media/audio")
        if args.job_id and args.job_id != context["job"]["job_id"]:
            raise ValueError("--job-id conflicts with the upstream subtitle job")
    else:
        if args.context or args.bindings:
            raise ValueError("--context and --bindings require --workspace")
        if not args.output_dir:
            raise ValueError("--output-dir is required without --workspace")
        profile = read_profile(args.profile)
    keyterms = normalize_keyterms(profile_keyterms(profile) + list(args.keyterm))
    parameters = build_parameters(
        args.mode, args.language_code, args.num_speakers, keyterms
    )
    created_at = datetime.now(timezone.utc)
    job_id = safe_job_id(
        (context["job"]["job_id"] if context else args.job_id) or f"{source.stem}-{created_at.strftime('%Y%m%d-%H%M%S')}"
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else context["evidence_dir"]
    if context and (output_dir != context["evidence_dir"] or job_id != context["job"]["job_id"]):
        raise ValueError("Output directory/job ID conflicts with the upstream subtitle job")
    raw_path = output_dir / f"{job_id}_elevenlabs_raw.json"
    metadata_path = output_dir / f"{job_id}_elevenlabs_request.json"
    warnings = ["This request can incur ElevenLabs transcription charges."]
    if keyterms:
        warnings.append("Keyterms can add an ElevenLabs transcription surcharge.")
    if args.mode == "multichannel":
        warnings.append("Every multichannel channel is billed for the full media duration.")
    return {
        "source": source,
        "source_sha256": sha256_file(source),
        "profile": profile,
        "subtitle_context": context,
        "parameters": parameters,
        "job_id": job_id,
        "created_at": created_at,
        "output_dir": output_dir,
        "raw_path": raw_path,
        "metadata_path": metadata_path,
        "warnings": warnings,
    }


def public_request_summary(request: dict[str, Any]) -> dict[str, Any]:
    profile = request["profile"]
    return {
        "dry_run": True,
        "job_id": request["job_id"],
        "mode": request["parameters"]["diarize"] and "diarized"
        or (request["parameters"]["use_multi_channel"] and "multichannel")
        or "single",
        "source": {
            "path": str(request["source"]),
            "sha256": request["source_sha256"],
        },
        "series_id": profile.get("series_id") if profile else None,
        "parameters": request["parameters"],
        "raw_path": str(request["raw_path"]),
        "metadata_path": str(request["metadata_path"]),
        "warnings": request["warnings"],
        "subtitle_context": handoff_metadata(request),
    }


def handoff_metadata(request):
    context = request.get("subtitle_context")
    if context is None:
        return None
    from subtitle_context import summary
    return summary(context)


def run(
    args: argparse.Namespace,
    client_factory: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    request = prepare_request(args)
    if args.dry_run:
        return public_request_summary(request)
    if not args.confirm_cost:
        raise ValueError("Actual API calls require --confirm-cost")
    raw_path = request["raw_path"]
    metadata_path = request["metadata_path"]
    if raw_path.exists() or metadata_path.exists():
        raise FileExistsError("Immutable ElevenLabs evidence already exists; refusing to overwrite")
    request["output_dir"].mkdir(parents=True, exist_ok=True)
    api_key, key_source = load_api_key(args.env_file)
    factory = client_factory or create_client
    client = factory(api_key)
    started = time.monotonic()
    try:
        with request["source"].open("rb") as handle:
            response = client.speech_to_text.convert(
                file=handle, **request["parameters"]
            )
    except Exception as error:
        raise RuntimeError(
            f"ElevenLabs request failed ({type(error).__name__}); no API response was saved"
        ) from None
    elapsed = time.monotonic() - started
    raw = serialize_response(response)
    metadata = {
        "schema_version": 1,
        "job_id": request["job_id"],
        "created_at": request["created_at"].isoformat(),
        "mode": args.mode,
        "model_id": MODEL_ID,
        "parameters": request["parameters"],
        "series_id": request["profile"].get("series_id")
        if request["profile"]
        else None,
        "source": {
            "path": str(request["source"]),
            "sha256": request["source_sha256"],
        },
        "api_key_source": key_source,
        "response_sha256": sha256_json(raw),
        "processing_seconds": round(elapsed, 3),
        "subtitle_context": handoff_metadata(request),
    }
    write_json_exclusive(raw_path, raw)
    write_json_exclusive(metadata_path, metadata)
    return {
        "dry_run": False,
        "job_id": request["job_id"],
        "mode": args.mode,
        "raw_path": str(raw_path),
        "metadata_path": str(metadata_path),
        "response_sha256": metadata["response_sha256"],
        "processing_seconds": metadata["processing_seconds"],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
