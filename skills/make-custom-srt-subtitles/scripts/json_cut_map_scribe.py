#!/usr/bin/env python3
"""Map existing Scribe v2 character timestamps onto a corrected master SRT.

This is intentionally local-only.  It never invents timing for corrected text and
never calls ElevenLabs; unresolved characters keep null timestamps so downstream
tools can reject only the cuts or cards that lack enough evidence.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


TIMESTAMP_LINE = re.compile(r"^\s*\d{2,}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+")
SKIPPED_TYPES = {"audio_event", "event", "spacing"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Locally map existing ElevenLabs Scribe v2 character JSON to a corrected SRT."
        )
    )
    parser.add_argument("--scribe", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel-index", type=int)
    parser.add_argument("--max-substitution-chars", type=int, default=4)
    return parser.parse_args(argv)


def compact(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def corrected_text(path: Path) -> str:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    parts: list[str] = []
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines()]
        if len(lines) < 3 or not TIMESTAMP_LINE.match(lines[1]):
            raise ValueError(f"Invalid SRT block: {block[:80]!r}")
        text = compact("".join(line.strip() for line in lines[2:] if line.strip()))
        if not text:
            raise ValueError("Empty SRT cue is not supported")
        parts.append(text)
    if not parts:
        raise ValueError("Corrected SRT contains no cues")
    return "".join(parts)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_scribe_payload(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        records: list[Any] = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSONL record at line {line_number}: {error.msg}"
                ) from error
        if not records:
            raise ValueError("Scribe JSONL contains no records")
        if len(records) == 1 and isinstance(records[0], dict):
            payload = records[0]
            container = "jsonl_single_response"
        elif all(
            isinstance(item, dict)
            and "text" in item
            and "start" in item
            and "end" in item
            for item in records
        ):
            payload = {"words": records}
            container = "jsonl_character_events"
        else:
            raise ValueError(
                "JSONL must contain one Scribe response object or one timed character event per line"
            )
    else:
        container = "json"
        if isinstance(payload, list):
            if all(
                isinstance(item, dict)
                and "text" in item
                and "start" in item
                and "end" in item
                for item in payload
            ):
                payload = {"words": payload}
                container = "json_character_event_array"
            else:
                raise ValueError("Scribe JSON array must contain timed character events")
    if not isinstance(payload, dict):
        raise ValueError("Scribe JSON root must be an object")
    return payload, container


def selected_words(
    payload: dict[str, Any], channel_index: int | None
) -> tuple[list[Any], int | None]:
    transcripts = payload.get("transcripts")
    if isinstance(transcripts, list):
        candidates: list[tuple[int, dict[str, Any]]] = []
        for ordinal, transcript in enumerate(transcripts):
            if not isinstance(transcript, dict):
                raise ValueError("Each Scribe transcript must be an object")
            explicit = transcript.get("channel_index")
            resolved = int(explicit) if isinstance(explicit, int) else ordinal
            candidates.append((resolved, transcript))
        if not candidates:
            raise ValueError("Scribe transcripts array is empty")
        if channel_index is None:
            if len(candidates) != 1:
                raise ValueError(
                    "Multichannel Scribe JSON requires --channel-index to select one source track"
                )
            selected_channel, selected = candidates[0]
        else:
            matches = [item for item in candidates if item[0] == channel_index]
            if len(matches) != 1:
                raise ValueError(f"Scribe channel_index not found or ambiguous: {channel_index}")
            selected_channel, selected = matches[0]
        words = selected.get("words")
        if not isinstance(words, list):
            raise ValueError("Selected Scribe transcript requires a words array")
        return words, selected_channel

    words = payload.get("words")
    if not isinstance(words, list):
        raise ValueError("Scribe JSON requires a words array or transcripts array")
    if channel_index is None:
        return words, None
    filtered = [
        item
        for item in words
        if isinstance(item, dict) and item.get("channel_index") == channel_index
    ]
    if not filtered:
        raise ValueError(
            "Top-level Scribe words do not contain the requested --channel-index"
        )
    return filtered, channel_index


def source_characters(
    payload: dict[str, Any], channel_index: int | None
) -> tuple[list[dict[str, Any]], int | None]:
    words, selected_channel = selected_words(payload, channel_index)
    characters: list[dict[str, Any]] = []
    previous_start = -1.0
    previous_end = -1.0
    for source_index, item in enumerate(words):
        if not isinstance(item, dict):
            raise ValueError("Each Scribe word event must be an object")
        event_type = str(item.get("type", "word")).lower()
        text = str(item.get("text", ""))
        if event_type in SKIPPED_TYPES or not compact(text):
            continue
        visible = compact(text)
        if len(visible) != 1:
            raise ValueError(
                "Scribe timing must be character-granular; refusing to split a timed "
                f"multi-character event: {text!r}"
            )
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"Visible Scribe character lacks timestamps: {visible!r}")
        start_value, end_value = float(start), float(end)
        if end_value < start_value:
            raise ValueError(f"Scribe character has inverted timestamps: {visible!r}")
        if start_value + 0.001 < previous_start or end_value + 0.001 < previous_end:
            raise ValueError("Scribe character timestamps are not monotonic")
        record: dict[str, Any] = {
            "text": visible,
            "start": start_value,
            "end": end_value,
            "source_event_index": source_index,
            "source_character_index": len(characters),
        }
        for key in ("speaker_id", "channel_index", "logprob"):
            if key in item:
                record[key] = item[key]
        if "channel_index" not in record and selected_channel is not None:
            record["channel_index"] = selected_channel
        characters.append(record)
        previous_start, previous_end = start_value, end_value
    if not characters:
        raise ValueError("Selected Scribe source contains no timed visible characters")
    return characters, selected_channel


def map_characters(
    source: list[dict[str, Any]], target: str, max_substitution_chars: int
) -> tuple[list[dict[str, Any]], dict[str, int | float]]:
    if max_substitution_chars < 0:
        raise ValueError("--max-substitution-chars must be zero or greater")
    source_text = "".join(item["text"] for item in source)
    mapped: list[dict[str, Any]] = [
        {"text": character, "start": None, "end": None, "mapping_status": "unmapped"}
        for character in target
    ]
    exact = substituted = deleted = 0
    matcher = difflib.SequenceMatcher(a=source_text, b=target, autojunk=False)
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        source_length = source_end - source_start
        target_length = target_end - target_start
        if tag == "equal":
            for offset in range(target_length):
                source_item = source[source_start + offset]
                mapped[target_start + offset] = {
                    **source_item,
                    "text": target[target_start + offset],
                    "mapping_status": "exact",
                }
            exact += target_length
        elif (
            tag == "replace"
            and source_length == target_length
            and 0 < target_length <= max_substitution_chars
        ):
            for offset in range(target_length):
                source_item = source[source_start + offset]
                mapped[target_start + offset] = {
                    **source_item,
                    "text": target[target_start + offset],
                    "source_text": source_item["text"],
                    "mapping_status": "substitution",
                }
            substituted += target_length
        if tag in {"delete", "replace"}:
            deleted += source_length

    unmapped = sum(item["mapping_status"] == "unmapped" for item in mapped)
    mapped_count = exact + substituted
    summary: dict[str, int | float] = {
        "source_characters": len(source),
        "corrected_characters": len(target),
        "exact_characters": exact,
        "substituted_characters": substituted,
        "unmapped_characters": unmapped,
        "deleted_source_characters": deleted,
        "mapped_coverage": round(mapped_count / len(target), 6) if target else 1.0,
    }
    return mapped, summary


def write_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def run(args: argparse.Namespace) -> dict[str, Any]:
    scribe_path = args.scribe.expanduser()
    srt_path = args.srt.expanduser()
    output_path = args.output.expanduser()
    for path in (scribe_path, srt_path):
        if not path.is_file():
            raise ValueError(f"Input does not exist: {path}")
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output_path}")
    payload, input_container = load_scribe_payload(scribe_path)
    target = corrected_text(srt_path)
    source, selected_channel = source_characters(payload, args.channel_index)
    characters, summary = map_characters(source, target, args.max_substitution_chars)
    output = {
        "schema_version": 1,
        "alignment_source": "scribe_v2_corrected_mapping",
        "additional_api_calls": 0,
        "additional_api_cost_usd": 0,
        "source_scribe_sha256": sha256(scribe_path),
        "source_container": input_container,
        "corrected_srt_sha256": sha256(srt_path),
        "selected_channel_index": selected_channel,
        "max_substitution_chars": args.max_substitution_chars,
        # Preserve the compact timed source stream so downstream cut alignment
        # can distinguish omitted punctuation from omitted spoken content.
        # A source-index gap alone is ambiguous and previously caused normal
        # sentence punctuation to reject valid Premiere edit boundaries.
        "source_characters": source,
        "characters": characters,
        "words": [],
        "summary": summary,
    }
    write_exclusive(output_path, json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return {"output": str(output_path.resolve()), **summary, "additional_api_calls": 0}


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run(parse_args(argv)), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
