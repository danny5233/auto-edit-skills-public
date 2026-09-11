#!/usr/bin/env python3
"""Reassign corrected subtitle text at visible Premiere XML edit points."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TIMESTAMP = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$")
MAX_SNAP_FRAMES = 5


@dataclass
class Cue:
    start: float
    end: float
    char_start: int
    char_end: int


@dataclass(frozen=True)
class AlignedChar:
    text: str
    start: float | None
    end: float | None
    mapping_status: str
    source_character_index: int | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use timed characters from existing Scribe JSON mapping or Forced Alignment "
            "to redistribute SRT text at Premiere XML cuts."
        )
    )
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--sequence-name")
    parser.add_argument("--protected-term", action="append", default=[])
    parser.add_argument("--cards", type=Path)
    parser.add_argument("--cards-output", type=Path)
    parser.add_argument(
        "--post-snap-audio-check", choices=("pending", "passed"), default="pending"
    )
    return parser.parse_args(argv)


def parse_time(value: str) -> float:
    match = TIMESTAMP.match(value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, milliseconds = (int(item) for item in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def format_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def compact(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def parse_srt(path: Path) -> tuple[list[Cue], str, list[int]]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    cues: list[Cue] = []
    master_parts: list[str] = []
    cursor = 0
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines()]
        if len(lines) < 3 or " --> " not in lines[1]:
            raise ValueError(f"Invalid SRT block: {block[:80]!r}")
        start_text, end_text = lines[1].split(" --> ", 1)
        text = " ".join(item.strip() for item in lines[2:] if item.strip())
        if not text:
            raise ValueError("Empty SRT cue is not supported")
        length = len(compact(text))
        cues.append(Cue(parse_time(start_text), parse_time(end_text), cursor, cursor + length))
        master_parts.append(text)
        cursor += length
    master = "".join(master_parts)
    positions = [index for index, character in enumerate(master) if not character.isspace()]
    if len(positions) != cursor:
        raise AssertionError("SRT character mapping failed")
    return cues, master, positions


def alignment_characters(path: Path, expected: str) -> tuple[list[AlignedChar], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_characters = payload.get("characters")
    if not isinstance(raw_characters, list):
        raise ValueError("Alignment JSON requires a characters array")
    characters: list[AlignedChar] = []
    previous_start = -1.0
    previous_end = -1.0
    for item in raw_characters:
        if not isinstance(item, dict):
            raise ValueError("Each aligned character must be an object")
        text = str(item.get("text", ""))
        if not text or text.isspace():
            continue
        if len(text) != 1:
            raise ValueError(f"Expected one aligned character, got: {text!r}")
        start, end = item.get("start"), item.get("end")
        has_start = isinstance(start, (int, float))
        has_end = isinstance(end, (int, float))
        if has_start != has_end:
            raise ValueError(f"Aligned character has only one timestamp: {text!r}")
        status = str(item.get("mapping_status") or "forced_alignment")
        if not has_start:
            if start is not None or end is not None:
                raise ValueError(f"Aligned character timestamps must be numbers or null: {text!r}")
            source_character_index = item.get("source_character_index")
            characters.append(
                AlignedChar(
                    text,
                    None,
                    None,
                    status,
                    int(source_character_index)
                    if isinstance(source_character_index, int)
                    else None,
                )
            )
            continue
        start_value, end_value = float(start), float(end)
        if end_value < start_value:
            raise ValueError(f"Aligned character has inverted timestamps: {text!r}")
        if start_value + 0.001 < previous_start or end_value + 0.001 < previous_end:
            raise ValueError("Aligned character timestamps are not monotonic")
        source_character_index = item.get("source_character_index")
        characters.append(
            AlignedChar(
                text,
                start_value,
                end_value,
                status,
                int(source_character_index)
                if isinstance(source_character_index, int)
                else None,
            )
        )
        previous_start, previous_end = start_value, end_value
    actual = "".join(item.text for item in characters)
    if actual != expected:
        mismatch = next(
            (index for index, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1]),
            min(len(actual), len(expected)),
        )
        raise ValueError(
            "Corrected SRT and alignment text differ at compact character "
            f"{mismatch}; refusing unsafe redistribution"
        )
    return characters, payload


def boundary_at_cut(
    characters: list[AlignedChar],
    cut: float,
    source_characters: list[dict[str, Any]] | None = None,
) -> tuple[int | None, float | None, str | None, tuple[str, str] | None]:
    overlapping = next(
        (
            item
            for item in characters
            if item.start is not None
            and item.end is not None
            and item.start + 0.001 < cut < item.end - 0.001
        ),
        None,
    )
    if overlapping is not None:
        return None, None, f"cut_inside_spoken_character:{overlapping.text}", None
    left = [
        index
        for index, item in enumerate(characters)
        if item.end is not None and item.end <= cut + 0.001
    ]
    right = [
        index
        for index, item in enumerate(characters)
        if item.start is not None and item.start >= cut - 0.001
    ]
    if not left or not right:
        return None, None, "cut_outside_aligned_text", None
    left_index, right_index = max(left), min(right)
    if right_index != left_index + 1:
        return None, None, "unmapped_characters_at_cut", None
    previous, following = characters[left_index], characters[right_index]
    assert previous.end is not None and following.start is not None
    if (
        previous.source_character_index is not None
        and following.source_character_index is not None
        and following.source_character_index != previous.source_character_index + 1
    ):
        gap_start = previous.source_character_index + 1
        gap_end = following.source_character_index
        if not isinstance(source_characters, list) or gap_end > len(source_characters):
            return None, None, "deleted_source_characters_at_cut", None
        skipped = source_characters[gap_start:gap_end]
        punctuation = "，。！？、：；,.!?:;…—-()（）「」『』〈〉《》"
        if not skipped or any(
            not isinstance(item, dict)
            or any(character not in punctuation and not character.isspace() for character in str(item.get("text", "")))
            for item in skipped
        ):
            return None, None, "deleted_source_characters_at_cut", None
    speech_boundary = min((previous.end, following.start), key=lambda value: abs(value - cut))
    return (
        right_index,
        speech_boundary,
        None,
        (previous.mapping_status, following.mapping_status),
    )


def inside_character_review_candidate(
    characters: list[AlignedChar], cut: float, context_size: int = 8
) -> dict[str, Any] | None:
    """Describe both semantic split choices when a cut overlaps one timed character.

    Scribe character intervals can include trailing or leading timing smear.  An
    overlap therefore needs semantic review rather than being treated as proof
    that the editorial cut is in the middle of a spoken word.
    """
    for index, item in enumerate(characters):
        if (
            item.start is None
            or item.end is None
            or not (item.start + 0.001 < cut < item.end - 0.001)
        ):
            continue
        compact_text = "".join(character.text for character in characters)
        before_start = max(0, index - context_size)
        after_end = min(len(characters), index + context_size + 1)
        return {
            "requires_semantic_review": True,
            "overlapping_character": item.text,
            "overlapping_character_index": index,
            "character_start": format_time(item.start),
            "character_end": format_time(item.end),
            "context": compact_text[before_start:after_end],
            "split_before_character": {
                "boundary_index": index,
                "before_text": compact_text[before_start:index],
                "after_text": compact_text[index:after_end],
            },
            "split_after_character": {
                "boundary_index": index + 1,
                "before_text": compact_text[before_start : index + 1],
                "after_text": compact_text[index + 1 : after_end],
            },
            "decision_rule": (
                "accept only when the overlapping character is the final character "
                "of a complete left unit or the first character of a complete right "
                "unit; reject a true mid-word or mid-grammar split"
            ),
        }
    return None


def known_boundary_review_candidate(
    characters: list[AlignedChar], boundary: int, context_size: int = 8
) -> dict[str, Any]:
    compact_text = "".join(character.text for character in characters)
    start = max(0, boundary - context_size)
    end = min(len(characters), boundary + context_size)
    return {
        "requires_semantic_review": True,
        "review_kind": "known_character_boundary_not_applied",
        "boundary_index": boundary,
        "before_text": compact_text[start:boundary],
        "after_text": compact_text[boundary:end],
        "decision_rule": (
            "review speaker and dialogue function before rejecting a short cue; "
            "an independent answer or speaker switch may be shorter than the normal "
            "display-duration preference"
        ),
    }


def span_text(master: str, positions: list[int], start: int, end: int) -> str:
    if start >= end:
        return ""
    return master[positions[start] : positions[end - 1] + 1].strip()


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if local_name(child) == name), None)


def direct_text(element: ET.Element, name: str, default: str = "") -> str:
    child = direct(element, name)
    return (child.text or "").strip() if child is not None else default


def descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element.iter() if local_name(item) == name]


def sequence_fps(sequence: ET.Element) -> float:
    rate = direct(sequence, "rate")
    if rate is None:
        raise ValueError("Premiere XML sequence has no rate")
    timebase = int(direct_text(rate, "timebase"))
    if timebase <= 0:
        raise ValueError("Premiere XML timebase must be positive")
    ntsc = direct_text(rate, "ntsc").upper() == "TRUE"
    return timebase * 1000 / 1001 if ntsc else float(timebase)


def parse_visible_cuts(path: Path, sequence_name: str | None) -> tuple[float, list[float], str]:
    root = ET.parse(path).getroot()
    sequences = descendants(root, "sequence")
    if sequence_name:
        sequences = [item for item in sequences if direct_text(item, "name") == sequence_name]
    if len(sequences) != 1:
        raise ValueError(
            "Premiere XML must resolve to exactly one sequence; use --sequence-name when needed"
        )
    sequence = sequences[0]
    fps = sequence_fps(sequence)
    media = direct(sequence, "media")
    video = direct(media, "video") if media is not None else None
    if video is None:
        raise ValueError("Premiere XML sequence has no video section")
    clips: list[tuple[int, int, int, str]] = []
    transitions: list[tuple[int, int]] = []
    for track_index, track in enumerate(child for child in video if local_name(child) == "track"):
        if direct_text(track, "enabled", "TRUE").upper() == "FALSE":
            continue
        for item in track:
            tag = local_name(item)
            if tag == "transitionitem":
                transitions.append((int(direct_text(item, "start")), int(direct_text(item, "end"))))
            if tag != "clipitem" or direct_text(item, "enabled", "TRUE").upper() == "FALSE":
                continue
            start = int(direct_text(item, "start"))
            end = int(direct_text(item, "end"))
            if start < 0 or end <= start:
                continue
            identity = item.get("id") or direct_text(item, "name") or f"track{track_index}:{start}:{end}"
            clips.append((track_index, start, end, identity))
    if not clips:
        raise ValueError("Premiere XML contains no enabled video clips")

    # A composited Premiere frame can visibly change on more than one enabled
    # track.  Do not reduce the sequence to only the highest active clip: a
    # persistent logo or graphic on an upper track would otherwise hide every
    # A-roll/B-roll edit below it.  Instead, compare each enabled track across
    # each boundary and retain the boundary when any track changes.  Later
    # word/protected-span/audio checks still decide whether the subtitle may be
    # redistributed at that candidate cut.
    def active_on_track(track_index: int, frame: float) -> str | None:
        active = [
            item
            for item in clips
            if item[0] == track_index and item[1] <= frame < item[2]
        ]
        return active[-1][3] if active else None

    cuts: list[float] = []
    track_indexes = sorted({track_index for track_index, _, _, _ in clips})
    for frame in sorted({value for _, start, end, _ in clips for value in (start, end) if value > 0}):
        if any(start <= frame <= end for start, end in transitions):
            continue
        changed = any(
            active_on_track(track_index, frame - 0.5)
            != active_on_track(track_index, frame + 0.5)
            for track_index in track_indexes
        )
        if changed:
            cuts.append(frame / fps)
    return fps, cuts, direct_text(sequence, "name")


def protected_spans(
    text: str, alignment_payload: dict[str, Any], explicit_terms: list[str]
) -> tuple[list[tuple[int, int, str]], list[str]]:
    spans: list[tuple[int, int, str]] = []
    warnings: list[str] = []
    cursor = 0
    words = alignment_payload.get("words", [])
    if isinstance(words, list):
        for item in words:
            if not isinstance(item, dict):
                continue
            word = compact(str(item.get("text", "")))
            if len(word) <= 1:
                continue
            index = text.find(word, cursor)
            if index < 0:
                warnings.append(f"Could not map aligned word as protected span: {word}")
                continue
            spans.append((index, index + len(word), f"aligned_word:{word}"))
            cursor = index + len(word)
    for raw_term in explicit_terms:
        term = compact(raw_term)
        if not term:
            continue
        start = 0
        found = False
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            spans.append((index, index + len(term), f"protected_term:{term}"))
            start = index + 1
            found = True
        if not found:
            warnings.append(f"Protected term not found in corrected text: {term}")
    return sorted(set(spans)), warnings


def crossing_protection(spans: list[tuple[int, int, str]], boundary: int) -> str | None:
    return next((label for start, end, label in spans if start < boundary < end), None)


def nearest_temporal_pair(cues: list[Cue], cut: float, window: float) -> int | None:
    candidates: list[tuple[float, int]] = []
    for index in range(len(cues) - 1):
        distance = min(abs(cues[index].end - cut), abs(cues[index + 1].start - cut))
        if distance <= window:
            candidates.append((distance, index))
    return min(candidates)[1] if candidates else None


def apply_xml_boundary(cues: list[Cue], boundary: int, cut: float, window: float) -> tuple[bool, float, str]:
    pair_index = nearest_temporal_pair(cues, cut, window)
    if pair_index is not None:
        left, right = cues[pair_index], cues[pair_index + 1]
        if left.char_start < boundary < right.char_end:
            cues[pair_index] = Cue(left.start, cut, left.char_start, boundary)
            cues[pair_index + 1] = Cue(cut, right.end, boundary, right.char_end)
            return True, min(left.end, right.start, key=lambda value: abs(value - cut)), "redistributed_adjacent_cues"
    for index, cue in enumerate(cues):
        if cue.start < cut < cue.end and cue.char_start < boundary < cue.char_end:
            cues[index : index + 1] = [
                Cue(cue.start, cut, cue.char_start, boundary),
                Cue(cut, cue.end, boundary, cue.char_end),
            ]
            original = min((cue.start, cue.end), key=lambda value: abs(value - cut))
            return True, original, "split_existing_cue"
    return False, cut, "no_compatible_subtitle_segment"


def force_index_boundary(cues: list[Cue], boundary: int, moment: float) -> bool:
    for index in range(len(cues) - 1):
        if cues[index].char_end == boundary and cues[index + 1].char_start == boundary:
            return True
    for index, cue in enumerate(cues):
        if cue.char_start < boundary < cue.char_end and cue.start < moment < cue.end:
            cues[index : index + 1] = [
                Cue(cue.start, moment, cue.char_start, boundary),
                Cue(moment, cue.end, boundary, cue.char_end),
            ]
            return True
    return False


def render_srt(cues: list[Cue], master: str, positions: list[int]) -> str:
    blocks = []
    previous_end = -1.0
    for index, cue in enumerate(cues, 1):
        text = span_text(master, positions, cue.char_start, cue.char_end)
        if not text or cue.end <= cue.start or cue.start < previous_end - 0.001:
            raise ValueError("Redistribution produced an empty, inverted, or overlapping subtitle")
        blocks.append(f"{index}\n{format_time(cue.start)} --> {format_time(cue.end)}\n{text}")
        previous_end = cue.end
    return "\n\n".join(blocks) + "\n"


def find_card(text: str, selection: dict[str, Any]) -> tuple[int, int]:
    phrase = compact(str(selection.get("text", "")))
    if not phrase:
        raise ValueError("Each card selection requires non-empty text")
    matches: list[int] = []
    start = 0
    while True:
        index = text.find(phrase, start)
        if index < 0:
            break
        matches.append(index)
        start = index + 1
    if not matches:
        raise ValueError(f"Card phrase not found: {phrase}")
    occurrence = selection.get("occurrence")
    if occurrence is None:
        if len(matches) != 1:
            raise ValueError(f"Card phrase is ambiguous; provide occurrence: {phrase}")
        index = matches[0]
    else:
        ordinal = int(occurrence)
        if ordinal < 1 or ordinal > len(matches):
            raise ValueError(f"Card occurrence is out of range: {phrase} #{ordinal}")
        index = matches[ordinal - 1]
    return index, index + len(phrase)


def write_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def run(args: argparse.Namespace) -> dict[str, Any]:
    paths = [args.srt, args.alignment, args.xml]
    for path in paths:
        if not path.expanduser().is_file():
            raise ValueError(f"Input does not exist: {path}")
    for output in (args.output_srt, args.report, args.cards_output):
        if output and output.expanduser().exists():
            raise FileExistsError(f"Refusing to overwrite output: {output}")
    if args.cards and not args.cards_output:
        raise ValueError("--cards requires --cards-output")

    cues, master, positions = parse_srt(args.srt.expanduser())
    text = compact(master)
    characters, alignment_payload = alignment_characters(args.alignment.expanduser(), text)
    alignment_source = str(
        alignment_payload.get("alignment_source") or "elevenlabs_forced_alignment"
    )
    if alignment_source == "scribe_v2_corrected_mapping":
        timing_evidence = (
            "Existing ElevenLabs Scribe v2 character JSON mapped to corrected text "
            "plus visible Premiere XML cut"
        )
    else:
        timing_evidence = "ElevenLabs Forced Alignment characters plus visible Premiere XML cut"
    fps, cuts, sequence_name = parse_visible_cuts(args.xml.expanduser(), args.sequence_name)
    window = MAX_SNAP_FRAMES / fps
    spans, warnings = protected_spans(text, alignment_payload, list(args.protected_term))
    checks: list[dict[str, Any]] = []

    for cut in cuts:
        boundary, speech_boundary, reason, adjacent_statuses = boundary_at_cut(
            characters, cut, alignment_payload.get("source_characters")
        )
        semantic_review = inside_character_review_candidate(characters, cut)
        if reason is not None:
            pass
        elif boundary is None or speech_boundary is None or adjacent_statuses is None:
            reason = "alignment_boundary_resolution_failed"
        elif crossing_protection(spans, boundary):
            reason = f"cut_inside_protected_span:{crossing_protection(spans, boundary)}"
        else:
            if abs(speech_boundary - cut) > window + 0.001:
                reason = "nearest_speech_boundary_exceeds_five_frames"
            else:
                applied, original, operation = apply_xml_boundary(cues, boundary, cut, window)
                if applied:
                    checks.append(
                        {
                            "cut": format_time(cut),
                            "fps": round(fps, 6),
                            "max_snap_frames": MAX_SNAP_FRAMES,
                            "original_boundary": format_time(original),
                            "final_boundary": format_time(cut),
                            "mode": "word_aware",
                            "status": "resolved" if args.post_snap_audio_check == "passed" else "pending",
                            "evidence": timing_evidence,
                            "alignment_source": alignment_source,
                            "adjacent_mapping_statuses": list(adjacent_statuses),
                            "speech_boundary": format_time(speech_boundary),
                            "operation": operation,
                            "_boundary_index": boundary,
                            "post_snap_audio_check": args.post_snap_audio_check,
                            "hidden_event_conflict": False
                            if args.post_snap_audio_check == "passed"
                            else None,
                            "speaker_boundary_preserved": True
                            if args.post_snap_audio_check == "passed"
                            else None,
                        }
                    )
                    continue
                reason = operation
        if (
            semantic_review is None
            and reason == "no_compatible_subtitle_segment"
            and boundary is not None
        ):
            semantic_review = known_boundary_review_candidate(characters, boundary)
        rejected_check = {
                "cut": format_time(cut),
                "fps": round(fps, 6),
                "max_snap_frames": MAX_SNAP_FRAMES,
                "original_boundary": format_time(cut),
                "final_boundary": format_time(cut),
                "mode": "not_used",
                "status": "not_used",
                "evidence": reason,
                "reason": reason,
            }
        if semantic_review is not None:
            rejected_check["semantic_review_candidate"] = semantic_review
        checks.append(rejected_check)

    card_records: list[dict[str, Any]] = []
    card_srt = ""
    if args.cards:
        card_payload = json.loads(args.cards.expanduser().read_text(encoding="utf-8"))
        selections = card_payload.get("selections")
        if not isinstance(selections, list):
            raise ValueError("Card JSON requires a selections array")
        resolved_cards: list[tuple[float, float, str, Any, list[str]]] = []
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError("Each card selection must be an object")
            start_index, end_index = find_card(text, selection)
            selected_characters = characters[start_index:end_index]
            if any(item.start is None or item.end is None for item in selected_characters):
                raise ValueError(
                    "Cannot create exact card timing; phrase contains unmapped characters: "
                    f"{selection['text']}"
                )
            start_time = selected_characters[0].start
            end_time = selected_characters[-1].end
            assert start_time is not None and end_time is not None
            if not force_index_boundary(cues, start_index, start_time):
                if start_index not in {cue.char_start for cue in cues}:
                    raise ValueError(f"Cannot create exact card start boundary: {selection['text']}")
            if not force_index_boundary(cues, end_index, end_time):
                if end_index not in {cue.char_end for cue in cues}:
                    raise ValueError(f"Cannot create exact card end boundary: {selection['text']}")
            phrase = span_text(master, positions, start_index, end_index)
            resolved_cards.append(
                (
                    start_time,
                    end_time,
                    phrase,
                    selection.get("occurrence", 1),
                    sorted({item.mapping_status for item in selected_characters}),
                )
            )
        resolved_cards.sort(key=lambda item: (item[0], item[1]))
        for previous, following in zip(resolved_cards, resolved_cards[1:]):
            if following[0] < previous[1] - 0.001:
                raise ValueError(
                    f"Emphasis card selections overlap: {previous[2]} / {following[2]}"
                )
        card_blocks: list[str] = []
        for index, (start_time, end_time, phrase, occurrence, mapping_statuses) in enumerate(
            resolved_cards, 1
        ):
            card_blocks.append(
                f"{index}\n{format_time(start_time)} --> {format_time(end_time)}\n{phrase}"
            )
            card_records.append(
                {
                    "text": phrase,
                    "occurrence": occurrence,
                    "start": format_time(start_time),
                    "end": format_time(end_time),
                    "timing_source": "first_character_start_to_last_character_end",
                    "alignment_source": alignment_source,
                    "mapping_statuses": mapping_statuses,
                }
            )
        card_srt = "\n\n".join(card_blocks) + ("\n" if card_blocks else "")

    for check in checks:
        if check["mode"] != "word_aware":
            continue
        boundary = int(check.pop("_boundary_index"))
        boundary_cue = next(
            (
                index
                for index, cue in enumerate(cues)
                if cue.char_start == boundary
                and abs(cue.start - parse_time(str(check["final_boundary"]))) <= 0.001
            ),
            None,
        )
        if boundary_cue is None or boundary_cue == 0:
            raise ValueError("Final XML boundary no longer has adjacent subtitle cues")
        check["before_text"] = span_text(
            master,
            positions,
            cues[boundary_cue - 1].char_start,
            cues[boundary_cue - 1].char_end,
        )
        check["after_text"] = span_text(
            master,
            positions,
            cues[boundary_cue].char_start,
            cues[boundary_cue].char_end,
        )

    output_srt = render_srt(cues, master, positions)
    if compact("".join(span_text(master, positions, cue.char_start, cue.char_end) for cue in cues)) != text:
        raise ValueError("Text preservation invariant failed")
    report = {
        "schema_version": 1,
        "sequence_name": sequence_name,
        "fps": round(fps, 6),
        "alignment_source": alignment_source,
        "alignment_loss": alignment_payload.get("loss"),
        "additional_api_calls": alignment_payload.get(
            "additional_api_calls", 0 if alignment_source == "scribe_v2_corrected_mapping" else None
        ),
        "additional_api_cost_usd": alignment_payload.get(
            "additional_api_cost_usd",
            0 if alignment_source == "scribe_v2_corrected_mapping" else None,
        ),
        "source_mapping_summary": alignment_payload.get("summary"),
        "xml_post_snap_review_required": True,
        "xml_edit_checks": checks,
        "emphasis_cards": card_records,
        "warnings": warnings,
        "summary": {
            "visible_xml_cuts": len(cuts),
            "word_aware_adopted": sum(item["mode"] == "word_aware" for item in checks),
            "not_used": sum(item["mode"] == "not_used" for item in checks),
            "semantic_review_required": sum(
                "semantic_review_candidate" in item for item in checks
            ),
            "emphasis_cards": len(card_records),
            "unmapped_characters": sum(
                item.start is None or item.end is None for item in characters
            ),
        },
    }
    report["delivery_grade"] = "candidate_only"
    report["xml_cut_scope"] = "enabled_track_candidates_visibility_requires_review"
    report["output_srt_sha256"] = hashlib.sha256(output_srt.encode("utf-8")).hexdigest()
    report["summary"]["xml_track_cut_candidates"] = len(cuts)
    write_exclusive(args.output_srt.expanduser(), output_srt)
    write_exclusive(args.report.expanduser(), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.cards_output:
        write_exclusive(args.cards_output.expanduser(), card_srt)
    return {
        "output_srt": str(args.output_srt.expanduser().resolve()),
        "report": str(args.report.expanduser().resolve()),
        "cards_output": str(args.cards_output.expanduser().resolve()) if args.cards_output else None,
        **report["summary"],
        "post_snap_audio_check": args.post_snap_audio_check,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run(parse_args(argv)), ensure_ascii=False, indent=2))
        return 0
    except (ET.ParseError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
