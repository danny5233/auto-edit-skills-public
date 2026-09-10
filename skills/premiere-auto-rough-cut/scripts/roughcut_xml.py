#!/usr/bin/env python3
"""Split Premiere-exported XMEML clips at classified timeline intervals.

The decision generator is responsible for any editorial padding (for example,
the project's three-frame handles).  This module only converts the supplied
second ranges to enclosing frame ranges and performs lossless, frame-aligned
XML segmentation.
"""

from __future__ import annotations

import argparse
import codecs
import copy
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence


PREMIERE_TICKS_PER_SECOND = 254_016_000_000
UTF8_BOM = codecs.BOM_UTF8
XML_DECLARATION_RE = re.compile(br"^\s*(<\?xml\s+[^?]*\?>)", re.IGNORECASE)
DOCTYPE_RE = re.compile(br"<!DOCTYPE\s+xmeml(?:\s+[^>]*)?>", re.IGNORECASE)
LABEL_PRECEDENCE = {None: 0, "yellow": 1, "red": 2}
TRACK_NAME_RE = re.compile(r"^[VA][1-9][0-9]*$")
DEFAULT_TARGET_TRACKS = ("V1", "A1", "A2")


class RoughCutXmlError(ValueError):
    """Raised when an input cannot be split without risking timeline damage."""


@dataclass(frozen=True)
class FrameDecision:
    start: int
    end: int
    label: str


@dataclass(frozen=True)
class TimelineSegment:
    start: int
    end: int
    label: str | None


@dataclass(frozen=True)
class XmlProlog:
    had_bom: bool
    declaration: bytes
    doctype: bytes


class ClipIdAllocator:
    def __init__(self, used_ids: Iterable[str]) -> None:
        self._used_ids = set(used_ids)
        self._counter = 1

    def next(self) -> str:
        while True:
            candidate = f"clipitem-ai-{self._counter:06d}"
            self._counter += 1
            if candidate not in self._used_ids:
                self._used_ids.add(candidate)
                return candidate


def _require_int_text(parent: ET.Element, tag: str, context: str) -> int:
    value = parent.findtext(tag)
    if value is None:
        raise RoughCutXmlError(f"{context} is missing <{tag}>")
    try:
        return int(value)
    except ValueError as exc:
        raise RoughCutXmlError(
            f"{context} has a non-integer <{tag}> value: {value!r}"
        ) from exc


def _set_int_text(parent: ET.Element, tag: str, value: int, context: str) -> None:
    element = parent.find(tag)
    if element is None:
        raise RoughCutXmlError(f"{context} is missing <{tag}>")
    element.text = str(value)


def _extract_prolog(xml_bytes: bytes) -> XmlProlog:
    had_bom = xml_bytes.startswith(UTF8_BOM)
    without_bom = xml_bytes[len(UTF8_BOM) :] if had_bom else xml_bytes

    declaration_match = XML_DECLARATION_RE.search(without_bom)
    declaration = (
        declaration_match.group(1)
        if declaration_match
        else b'<?xml version="1.0" encoding="UTF-8"?>'
    )
    if b"utf-8" not in declaration.lower():
        raise RoughCutXmlError("only UTF-8 XMEML input is supported")

    doctype_match = DOCTYPE_RE.search(without_bom)
    if not doctype_match:
        raise RoughCutXmlError("input is missing <!DOCTYPE xmeml>")

    return XmlProlog(
        had_bom=had_bom,
        declaration=declaration,
        doctype=doctype_match.group(0),
    )


def _find_single_sequence(root: ET.Element) -> ET.Element:
    if root.tag != "xmeml":
        raise RoughCutXmlError(f"expected <xmeml> root, found <{root.tag}>")

    direct_sequences = root.findall("sequence")
    if len(direct_sequences) == 1:
        return direct_sequences[0]

    sequences = root.findall(".//sequence")
    if len(sequences) != 1:
        raise RoughCutXmlError(
            f"expected exactly one sequence, found {len(sequences)}"
        )
    return sequences[0]


def _read_rate(parent: ET.Element, context: str) -> Fraction:
    rate = parent.find("rate")
    if rate is None:
        raise RoughCutXmlError(f"{context} is missing <rate>")

    timebase = _require_int_text(rate, "timebase", f"{context} rate")
    ntsc = (rate.findtext("ntsc") or "FALSE").strip().upper() == "TRUE"
    if timebase <= 0:
        raise RoughCutXmlError(f"{context} has invalid timebase {timebase}")
    return Fraction(timebase * 1000, 1001) if ntsc else Fraction(timebase, 1)


def _ticks_per_frame(fps: Fraction) -> int:
    ticks = Fraction(PREMIERE_TICKS_PER_SECOND, 1) / fps
    if ticks.denominator != 1:
        raise RoughCutXmlError(
            f"Premiere ticks per frame are non-integral for rate {float(fps):.9f}"
        )
    return ticks.numerator


def _number_as_fraction(value: Any, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, float)):
        raise RoughCutXmlError(f"{field} must be a JSON number")
    try:
        if isinstance(value, float):
            return Fraction(str(value))
        return Fraction(value)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise RoughCutXmlError(f"{field} must be a finite JSON number") from exc


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def _frame_decisions(
    decisions: dict[str, Any], fps: Fraction, duration: int
) -> list[FrameDecision]:
    intervals = decisions.get("intervals")
    if not isinstance(intervals, list):
        raise RoughCutXmlError("decisions JSON must contain an 'intervals' list")

    converted: list[FrameDecision] = []
    for index, interval in enumerate(intervals):
        context = f"intervals[{index}]"
        if not isinstance(interval, dict):
            raise RoughCutXmlError(f"{context} must be an object")

        label = interval.get("label")
        if label not in {"red", "yellow"}:
            raise RoughCutXmlError(
                f"{context}.label must be either 'red' or 'yellow'"
            )

        seconds_keys = {"start_seconds", "end_seconds"}
        frame_keys = {"start_frame", "end_frame"}
        has_seconds = bool(seconds_keys.intersection(interval))
        has_frames = bool(frame_keys.intersection(interval))
        if has_seconds and has_frames:
            raise RoughCutXmlError(
                f"{context} cannot mix seconds and frame boundaries"
            )
        if not has_seconds and not has_frames:
            raise RoughCutXmlError(
                f"{context} must use either start_seconds/end_seconds or "
                "start_frame/end_frame"
            )

        if has_frames:
            if not frame_keys.issubset(interval):
                raise RoughCutXmlError(
                    f"{context} must contain both start_frame and end_frame"
                )
            start_value = interval["start_frame"]
            end_value = interval["end_frame"]
            if (
                isinstance(start_value, bool)
                or isinstance(end_value, bool)
                or not isinstance(start_value, int)
                or not isinstance(end_value, int)
            ):
                raise RoughCutXmlError(
                    f"{context}.start_frame and end_frame must be integers"
                )
            start_frame = start_value
            end_frame = end_value
            if end_frame <= start_frame:
                raise RoughCutXmlError(
                    f"{context}.end_frame must be greater than start_frame"
                )
        else:
            if not seconds_keys.issubset(interval):
                raise RoughCutXmlError(
                    f"{context} must contain both start_seconds and end_seconds"
                )
            start_seconds = _number_as_fraction(
                interval["start_seconds"], f"{context}.start_seconds"
            )
            end_seconds = _number_as_fraction(
                interval["end_seconds"], f"{context}.end_seconds"
            )
            if end_seconds <= start_seconds:
                raise RoughCutXmlError(
                    f"{context}.end_seconds must be greater than start_seconds"
                )

            # Enclose every source-time instant named by the decision.  Any
            # desired three-frame editorial handle must already be present.
            start_frame = _floor(start_seconds * fps)
            end_frame = _ceil(end_seconds * fps)

        # Both input modes may straddle the sequence boundaries. Clamp to the
        # valid half-open frame domain [0, duration), then discard empty spans.
        start_frame = min(duration, max(0, start_frame))
        end_frame = min(duration, max(0, end_frame))
        if start_frame < end_frame:
            converted.append(FrameDecision(start_frame, end_frame, label))

    return converted


def _timeline_segments(
    decisions: Sequence[FrameDecision], duration: int
) -> list[TimelineSegment]:
    boundaries = {0, duration}
    for decision in decisions:
        boundaries.add(decision.start)
        boundaries.add(decision.end)
    ordered = sorted(boundaries)

    segments: list[TimelineSegment] = []
    for start, end in zip(ordered, ordered[1:]):
        active_labels = [
            decision.label
            for decision in decisions
            if decision.start < end and decision.end > start
        ]
        label = max(active_labels, key=LABEL_PRECEDENCE.get) if active_labels else None
        if segments and segments[-1].label == label:
            previous = segments[-1]
            segments[-1] = TimelineSegment(previous.start, end, label)
        else:
            segments.append(TimelineSegment(start, end, label))

    if not segments or segments[0].start != 0 or segments[-1].end != duration:
        raise RoughCutXmlError("internal error: decisions do not cover sequence domain")
    for previous, current in zip(segments, segments[1:]):
        if previous.end != current.start:
            raise RoughCutXmlError("internal error: decision segments have a gap")
    return segments


def _normalize_target_track_names(
    requested_tracks: Sequence[str] | None,
) -> list[str]:
    raw_names = DEFAULT_TARGET_TRACKS if requested_tracks is None else requested_tracks
    normalized: list[str] = []
    for raw_name in raw_names:
        if not isinstance(raw_name, str):
            raise RoughCutXmlError("target track names must be strings")
        name = raw_name.strip().upper()
        if not TRACK_NAME_RE.fullmatch(name):
            raise RoughCutXmlError(
                f"invalid target track {raw_name!r}; use names such as V1 or A2"
            )
        if name in normalized:
            raise RoughCutXmlError(f"duplicate target track {name}")
        normalized.append(name)
    if not normalized:
        raise RoughCutXmlError("at least one target track is required")
    return normalized


def _target_tracks(
    sequence: ET.Element, requested_tracks: Sequence[str] | None = None
) -> list[tuple[str, ET.Element]]:
    video_tracks = sequence.findall("./media/video/track")
    audio_tracks = sequence.findall("./media/audio/track")
    available = {
        **{f"V{index}": track for index, track in enumerate(video_tracks, start=1)},
        **{f"A{index}": track for index, track in enumerate(audio_tracks, start=1)},
    }
    target_names = _normalize_target_track_names(requested_tracks)
    missing = [name for name in target_names if name not in available]
    if missing:
        raise RoughCutXmlError(
            "requested target tracks do not exist: " + ", ".join(missing)
        )

    targets: list[tuple[str, ET.Element]] = []
    for name in target_names:
        track = available[name]
        if track.find("transitionitem") is not None:
            raise RoughCutXmlError(
                f"{name} contains transitionitems and cannot be split safely"
            )
        if track.find("clipitem") is not None:
            targets.append((name, track))
    if not targets:
        raise RoughCutXmlError("the selected target tracks contain no clipitems to split")
    return targets


def _clip_timing(clip: ET.Element, context: str) -> tuple[int, int, int, int]:
    start = _require_int_text(clip, "start", context)
    end = _require_int_text(clip, "end", context)
    source_in = _require_int_text(clip, "in", context)
    source_out = _require_int_text(clip, "out", context)
    if end <= start:
        raise RoughCutXmlError(f"{context} has an empty or reversed timeline range")
    if source_out <= source_in:
        raise RoughCutXmlError(
            f"{context} has an empty or reversed legacy source range"
        )
    return start, end, source_in, source_out


def _read_tick_pair(
    clip: ET.Element, context: str
) -> tuple[int, int] | None:
    ticks_in_element = clip.find("pproTicksIn")
    ticks_out_element = clip.find("pproTicksOut")
    if (ticks_in_element is None) != (ticks_out_element is None):
        raise RoughCutXmlError(
            f"{context} must contain both pproTicksIn and pproTicksOut or neither"
        )
    if ticks_in_element is None or ticks_out_element is None:
        return None
    try:
        return int(ticks_in_element.text or ""), int(ticks_out_element.text or "")
    except ValueError as exc:
        raise RoughCutXmlError(f"{context} has invalid Premiere ticks") from exc


def _validate_source_timing(
    *,
    start: int,
    end: int,
    source_in: int,
    source_out: int,
    tick_pair: tuple[int, int] | None,
    ticks_per_frame: int,
    context: str,
) -> int:
    """Return the legacy source-span delta after validating exact timing.

    Premiere can export frame-aligned sequence audio whose source in-point is
    between video frames.  In that case pproTicksIn/Out carry the exact source
    range, while the legacy XMEML ``in`` is rounded up and ``out`` is rounded
    down.  Its integer source span is therefore one frame shorter than its
    sequence span.  Exact ticks, not those lossy legacy fields, are the timing
    authority for this narrowly recognized form.
    """

    timeline_span = end - start
    legacy_span = source_out - source_in
    legacy_delta = legacy_span - timeline_span

    if tick_pair is None:
        if legacy_delta != 0:
            raise RoughCutXmlError(
                f"{context} source span does not match its timeline span and "
                "has no Premiere ticks to prove an exact normal-speed range"
            )
        return 0

    ticks_in, ticks_out = tick_pair
    if ticks_out - ticks_in != timeline_span * ticks_per_frame:
        raise RoughCutXmlError(
            f"{context} Premiere tick span does not match its frame span"
        )

    if legacy_delta == 0:
        return 0
    if legacy_delta != -1:
        raise RoughCutXmlError(
            f"{context} has unsupported legacy source-span delta "
            f"{legacy_delta}; only exact spans and Premiere's -1-frame "
            "audio subframe rounding are safe to split"
        )

    tick_remainder = ticks_in % ticks_per_frame
    if tick_remainder == 0 or ticks_out % ticks_per_frame != tick_remainder:
        raise RoughCutXmlError(
            f"{context} has a -1-frame legacy source span but its Premiere "
            "ticks do not describe a constant subframe offset"
        )
    if source_in != _ceil(Fraction(ticks_in, ticks_per_frame)):
        raise RoughCutXmlError(
            f"{context} legacy in-point is inconsistent with its Premiere "
            "subframe tick in-point"
        )
    if source_out != _floor(Fraction(ticks_out, ticks_per_frame)):
        raise RoughCutXmlError(
            f"{context} legacy out-point is inconsistent with its Premiere "
            "subframe tick out-point"
        )
    return legacy_delta


def _clip_resource(clip: ET.Element, context: str) -> tuple[str, ET.Element]:
    """Return the clip's media resource, including Premiere nested sequences."""

    resources = [
        (tag, element)
        for tag in ("file", "sequence")
        if (element := clip.find(tag)) is not None
    ]
    if len(resources) != 1:
        raise RoughCutXmlError(
            f"{context} must contain exactly one <file> or nested <sequence> resource"
        )
    tag, element = resources[0]
    if not element.get("id"):
        raise RoughCutXmlError(f"{context} has a <{tag}> resource without id")
    return tag, element


def _replace_resource_with_reference(clip: ET.Element, context: str) -> None:
    tag, resource = _clip_resource(clip, context)
    resource_id = resource.get("id")
    assert resource_id is not None
    if not list(resource):
        return

    children = list(clip)
    position = children.index(resource)
    reference = ET.Element(tag, {"id": resource_id})
    reference.tail = resource.tail
    clip.remove(resource)
    clip.insert(position, reference)


def _set_label(clip: ET.Element, label_name: str) -> None:
    labels = clip.find("labels")
    if labels is None:
        labels = ET.SubElement(clip, "labels")
    label2 = labels.find("label2")
    if label2 is None:
        label2 = ET.SubElement(labels, "label2")
    label2.text = label_name


def _rotation_values(root: ET.Element) -> set[str]:
    """Collect authored rotation values without interpreting or normalizing them."""

    values: set[str] = set()
    for parameter in root.findall(".//parameter"):
        if (parameter.findtext("parameterid") or "").strip() == "rotation":
            value = parameter.findtext("value")
            if value is None:
                raise RoughCutXmlError("rotation parameter is missing its value")
            values.add(value.strip())
    return values


def _split_clip(
    clip: ET.Element,
    timeline: Sequence[TimelineSegment],
    sequence_fps: Fraction,
    ticks_per_frame: int,
    allocator: ClipIdAllocator,
    label_names: dict[str, str],
    context: str,
) -> list[ET.Element]:
    clip_id = clip.get("id")
    if not clip_id:
        raise RoughCutXmlError(f"{context} is missing its clipitem id")
    clip_fps = _read_rate(clip, context)
    if clip_fps != sequence_fps:
        raise RoughCutXmlError(
            f"{context} rate {float(clip_fps):.9f} does not match sequence rate "
            f"{float(sequence_fps):.9f}"
        )

    start, end, source_in, source_out = _clip_timing(clip, context)
    _clip_resource(clip, context)

    tick_pair = _read_tick_pair(clip, context)
    _validate_source_timing(
        start=start,
        end=end,
        source_in=source_in,
        source_out=source_out,
        tick_pair=tick_pair,
        ticks_per_frame=ticks_per_frame,
        context=context,
    )
    ticks_base = tick_pair[0] if tick_pair is not None else None

    pieces: list[tuple[int, int, str | None]] = []
    for segment in timeline:
        piece_start = max(start, segment.start)
        piece_end = min(end, segment.end)
        if piece_start < piece_end:
            pieces.append((piece_start, piece_end, segment.label))
    if not pieces:
        raise RoughCutXmlError(f"{context} lies outside the sequence duration")

    split_clips: list[ET.Element] = []
    for index, (piece_start, piece_end, decision_label) in enumerate(pieces):
        split = copy.deepcopy(clip)
        split.set("id", clip_id if index == 0 else allocator.next())

        # Advance each lossy legacy endpoint from its own original endpoint.
        # For ordinary clips this is equivalent to deriving both values from
        # source_in.  For Premiere audio with a subframe in-point, it preserves
        # the exported ceil(in)/floor(out) convention and leaves exact timing
        # to pproTicksIn/Out.
        new_in = source_in + (piece_start - start)
        new_out = source_out + (piece_end - end)
        if new_out <= new_in:
            raise RoughCutXmlError(
                f"{context} would create a {piece_end - piece_start}-frame "
                "piece whose rounded legacy in/out collapse; merge or widen "
                "that decision instead of fabricating source timing"
            )
        _set_int_text(split, "start", piece_start, context)
        _set_int_text(split, "end", piece_end, context)
        _set_int_text(split, "in", new_in, context)
        _set_int_text(split, "out", new_out, context)

        if ticks_base is not None:
            new_ticks_in = ticks_base + (piece_start - start) * ticks_per_frame
            new_ticks_out = ticks_base + (piece_end - start) * ticks_per_frame
            _set_int_text(split, "pproTicksIn", new_ticks_in, context)
            _set_int_text(split, "pproTicksOut", new_ticks_out, context)

        if index > 0:
            _replace_resource_with_reference(split, context)
        if decision_label is not None:
            _set_label(split, label_names[decision_label])
        split_clips.append(split)

    return split_clips


def _coverage(intervals: Sequence[tuple[int, int]], context: str) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if end <= start:
            raise RoughCutXmlError(f"{context} contains an empty clip")
        if merged and start < merged[-1][1]:
            raise RoughCutXmlError(f"{context} contains overlapping clipitems")
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _track_intervals(track: ET.Element, context: str) -> list[tuple[int, int]]:
    intervals = []
    for index, clip in enumerate(track.findall("clipitem")):
        start, end, _, _ = _clip_timing(clip, f"{context} clipitem[{index}]")
        intervals.append((start, end))
    return intervals


def _validate_clip_ids(root: ET.Element) -> None:
    clip_ids = [clip.get("id") for clip in root.findall(".//clipitem")]
    if any(not clip_id for clip_id in clip_ids):
        raise RoughCutXmlError("every clipitem must have an id")
    if len(clip_ids) != len(set(clip_ids)):
        raise RoughCutXmlError("clipitem ids are not globally unique")


def _validate_resource_references(root: ET.Element) -> None:
    definitions: dict[tuple[str, str], int] = {}
    referenced: set[tuple[str, str]] = set()
    for clip in root.findall(".//clipitem"):
        context = f"clipitem {clip.get('id')!r}"
        tag, resource = _clip_resource(clip, context)
        resource_id = resource.get("id")
        assert resource_id is not None
        key = (tag, resource_id)
        referenced.add(key)
        if list(resource):
            definitions[key] = definitions.get(key, 0) + 1

    missing = sorted(referenced.difference(definitions))
    if missing:
        raise RoughCutXmlError(
            "media references lack a full definition: "
            + ", ".join(f"{tag}:{resource_id}" for tag, resource_id in missing)
        )
    duplicates = sorted(key for key, count in definitions.items() if count > 1)
    if duplicates:
        raise RoughCutXmlError(
            "media ids have multiple full definitions: "
            + ", ".join(f"{tag}:{resource_id}" for tag, resource_id in duplicates)
        )


def _validate_ticks(
    targets: Sequence[tuple[str, ET.Element]], ticks_per_frame: int
) -> None:
    for track_name, track in targets:
        for index, clip in enumerate(track.findall("clipitem")):
            context = f"{track_name} clipitem[{index}]"
            start, end, source_in, source_out = _clip_timing(clip, context)
            tick_pair = _read_tick_pair(clip, context)
            _validate_source_timing(
                start=start,
                end=end,
                source_in=source_in,
                source_out=source_out,
                tick_pair=tick_pair,
                ticks_per_frame=ticks_per_frame,
                context=context,
            )


def _serialize(root: ET.Element, prolog: XmlProlog) -> bytes:
    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
    prefix = UTF8_BOM if prolog.had_bom else b""
    return (
        prefix
        + prolog.declaration
        + b"\n"
        + prolog.doctype
        + b"\n"
        + body
        + b"\n"
    )


def split_xmeml_bytes(
    xml_bytes: bytes,
    decisions: dict[str, Any],
    *,
    red_label: str = "Rose",
    yellow_label: str = "Mango",
    sequence_name: str | None = None,
    target_tracks: Sequence[str] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Return a split XMEML document and a machine-readable operation summary."""
    if not red_label.strip() or not yellow_label.strip():
        raise RoughCutXmlError("label names cannot be empty")

    prolog = _extract_prolog(xml_bytes)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RoughCutXmlError(f"invalid XML: {exc}") from exc

    sequence = _find_single_sequence(root)
    source_rotation_values = _rotation_values(root)
    if sequence_name is not None:
        renamed_sequence = sequence_name.strip()
        if not renamed_sequence:
            raise RoughCutXmlError("sequence name cannot be empty")
        name_element = sequence.find("name")
        if name_element is None:
            raise RoughCutXmlError("sequence is missing <name>")
        name_element.text = renamed_sequence
    duration = _require_int_text(sequence, "duration", "sequence")
    if duration <= 0:
        raise RoughCutXmlError("sequence duration must be positive")
    fps = _read_rate(sequence, "sequence")
    ticks_per_frame = _ticks_per_frame(fps)

    frame_decisions = _frame_decisions(decisions, fps, duration)
    timeline = _timeline_segments(frame_decisions, duration)
    targets = _target_tracks(sequence, target_tracks)

    _validate_clip_ids(root)
    used_clip_ids = [
        clip.get("id") for clip in root.findall(".//clipitem") if clip.get("id")
    ]
    allocator = ClipIdAllocator(used_clip_ids)
    label_names = {"red": red_label.strip(), "yellow": yellow_label.strip()}

    original_coverage: dict[str, list[tuple[int, int]]] = {}
    for track_name, track in targets:
        original_coverage[track_name] = _coverage(
            _track_intervals(track, track_name), track_name
        )
        new_children: list[ET.Element] = []
        clip_index = 0
        for child in list(track):
            if child.tag != "clipitem":
                new_children.append(child)
                continue
            context = f"{track_name} clipitem[{clip_index}]"
            new_children.extend(
                _split_clip(
                    child,
                    timeline,
                    fps,
                    ticks_per_frame,
                    allocator,
                    label_names,
                    context,
                )
            )
            clip_index += 1
        track[:] = new_children

    for track_name, track in targets:
        after = _coverage(_track_intervals(track, track_name), track_name)
        if after != original_coverage[track_name]:
            raise RoughCutXmlError(
                f"{track_name} coverage changed: {original_coverage[track_name]} -> {after}"
            )

    _validate_clip_ids(root)
    _validate_resource_references(root)
    _validate_ticks(targets, ticks_per_frame)
    if _rotation_values(root) != source_rotation_values:
        raise RoughCutXmlError(
            "video rotation values changed while splitting; automatic rotation is forbidden"
        )
    output_bytes = _serialize(root, prolog)
    try:
        ET.fromstring(output_bytes)
    except ET.ParseError as exc:
        raise RoughCutXmlError(f"serialized output is invalid XML: {exc}") from exc

    classified_frames = {
        label: sum(segment.end - segment.start for segment in timeline if segment.label == label)
        for label in ("red", "yellow")
    }
    report = {
        "sequence_name": sequence.findtext("name") or "",
        "duration_frames": duration,
        "fps": float(fps),
        "ticks_per_frame": ticks_per_frame,
        "target_tracks": [name for name, _ in targets],
        "timeline_segments": len(timeline),
        "classified_frames": classified_frames,
        "red_label": label_names["red"],
        "yellow_label": label_names["yellow"],
    }
    return output_bytes, report


def _load_decisions(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle, parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise RoughCutXmlError(f"cannot read decisions JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RoughCutXmlError("decisions JSON root must be an object")
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(data)
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
        description=(
            "Split selected Premiere XMEML clipitems at red/yellow decision ranges "
            "without changing sequence coverage or duration."
        )
    )
    parser.add_argument("--xml", required=True, type=Path, help="input XMEML file")
    parser.add_argument(
        "--decisions", required=True, type=Path, help="decision intervals JSON"
    )
    parser.add_argument("--output", required=True, type=Path, help="output XML path")
    parser.add_argument(
        "--red-label", default="Rose", help="Premiere label2 name for red decisions"
    )
    parser.add_argument(
        "--yellow-label",
        default="Mango",
        help="Premiere label2 name for yellow decisions",
    )
    parser.add_argument(
        "--sequence-name",
        help="replace only the output sequence <name>",
    )
    parser.add_argument(
        "--target-tracks",
        default=",".join(DEFAULT_TARGET_TRACKS),
        help="comma-separated Premiere tracks to split (default: V1,A1,A2)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output file"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_path = args.xml.resolve()
        output_path = args.output.resolve()
        if input_path == output_path:
            raise RoughCutXmlError("output path must differ from input XML")
        if output_path.exists() and not args.force:
            raise RoughCutXmlError(
                f"output already exists: {output_path} (use --force to replace it)"
            )

        xml_bytes = args.xml.read_bytes()
        decisions = _load_decisions(args.decisions)
        output_bytes, report = split_xmeml_bytes(
            xml_bytes,
            decisions,
            red_label=args.red_label,
            yellow_label=args.yellow_label,
            sequence_name=args.sequence_name,
            target_tracks=args.target_tracks.split(","),
        )
        _atomic_write(args.output, output_bytes)
        report["output"] = str(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RoughCutXmlError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
