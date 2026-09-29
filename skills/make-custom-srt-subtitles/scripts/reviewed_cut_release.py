#!/usr/bin/env python3
"""Apply reviewed XML boundary decisions, or preserve an authoritative human SRT.

No API calls or automatic audio certification. A human replay is labelled as
such; it is not a blind evaluation of subtitle generation quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from json_cut_align_xml import (
    MAX_SNAP_FRAMES, Cue, alignment_characters, apply_xml_boundary, compact,
    crossing_protection, format_time, parse_srt, parse_visible_cuts,
    protected_spans, render_srt, xml_rate,
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(path: Path):
    cues, master, positions = parse_srt(path)
    previous = -1.0
    for cue in cues:
        if not math.isfinite(cue.start) or cue.start < previous or cue.end <= cue.start:
            raise ValueError("Invalid or overlapping subtitle timing")
        previous = cue.end
    return cues, master, positions


def cut_ledger(cues, cuts, fps):
    # Millisecond SRT serialization can round/truncate by <= 1 ms. Merely
    # landing somewhere in the same frame is not exact cut alignment.
    close = lambda a, b: abs(a - b) <= .001001
    return [{"frame": round(c*fps), "time": format_time(c),
             "subtitle_start": any(close(x.start, c) for x in cues),
             "subtitle_end": any(close(x.end, c) for x in cues),
             "continuous_change": any(close(a.end, c) and close(b.start, c)
                                      for a, b in zip(cues, cues[1:]))} for c in cuts]


def validate_release(srt: Path, xml: Path, report: Path, sequence_name=None):
    """Recompute the ledger from serialized output; never trust counts alone."""
    data = json.loads(report.read_text(encoding="utf-8"))
    if data.get("schema_version") != 2 or data.get("grade") not in {
            "reviewed_boundary_release", "human_reference_replay"}:
        raise ValueError("XML cut release requires a completed version-2 release report")
    if data.get("output_srt_sha256") != sha(srt) or data.get("xml_sha256") != sha(xml):
        raise ValueError("Stale XML cut release: output or XML changed")
    selected = sequence_name or data.get("sequence_name")
    fps, cuts, name = parse_visible_cuts(xml, selected)
    rate = xml_rate(xml, name)
    if data.get("fps_numerator") != rate.numerator or data.get("fps_denominator") != rate.denominator:
        raise ValueError("XML cut release frame rate mismatch")
    cues, _, _ = checked(srt)
    ledger = cut_ledger(cues, cuts, fps)
    if data.get("cut_ledger") != ledger:
        raise ValueError("XML cut ledger differs from serialized SRT")
    if data["grade"] == "reviewed_boundary_release":
        reviews = data.get("reviewed_cuts", [])
        frames = [x.get("frame") for x in reviews]
        if len(frames) != len(ledger) or set(frames) != {x["frame"] for x in ledger}:
            raise ValueError("Incomplete XML cut review coverage")
        by_frame = {x["frame"]: x for x in ledger}
        for item in reviews:
            if item.get("action") not in {"adopt", "keep", "not_visible"} or not item.get("reason"):
                raise ValueError("Unresolved XML cut review")
            if item["action"] == "adopt":
                key = {"change": "continuous_change", "start": "subtitle_start", "end": "subtitle_end"}.get(item.get("endpoint", "change"))
                if not key or not by_frame[item["frame"]][key]:
                    raise ValueError("Adopted cut missing from serialized output")
    return {"status": "xml-cut-release-verified", "grade": data["grade"],
            "xml_track_cut_candidates": len(ledger),
            "exact_endpoint_hits": sum(x["subtitle_start"] or x["subtitle_end"] for x in ledger)}

def release(srt: Path, xml: Path, review: Path, output: Path, report: Path,
            sequence_name=None, alignment: Path | None = None):
    for path in (output, report):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite: {path}")
    if output.resolve() == report.resolve():
        raise ValueError("Output and report must be different paths")
    decision = json.loads(review.read_text(encoding="utf-8"))
    for key, path in (("input_srt_sha256", srt), ("xml_sha256", xml)):
        if decision.get(key) != sha(path):
            raise ValueError(f"Stale review: {key}")
    if not decision.get("reviewer") or not decision.get("evidence"):
        raise ValueError("Review requires a named reviewer and evidence")
    cues, master, positions = checked(srt)
    fps, cuts, name = parse_visible_cuts(xml, sequence_name)
    before = cut_ledger(cues, cuts, fps)
    mode = decision.get("mode")
    exact = []
    items = []
    if mode == "human_reference":
        human = Path(decision["human_srt"])
        if not human.is_absolute():
            human = review.parent / human
        if decision.get("human_srt_sha256") != sha(human):
            raise ValueError("Stale human reference")
        cues, master, positions = checked(human)
        content = human.read_bytes()  # Preserve original BOM/spacing/endpoints.
        from srt_style import parse_srt as parse_style, format_timestamp
        for cue in parse_style(human):
            exact.append({"start": format_timestamp(cue.start_ms),
                          "end": format_timestamp(cue.end_ms), "text": cue.visible_text})
        applied = []
        grade = "human_reference_replay"
    elif mode == "reviewed_boundaries":
        if alignment is None or decision.get("alignment_sha256") != sha(alignment):
            raise ValueError("Missing or stale alignment")
        chars, payload = alignment_characters(alignment, compact(master))
        spans, _ = protected_spans(compact(master), payload, decision.get("protected_terms", []))
        items = decision.get("cuts", [])
        frames = [x.get("frame") for x in items]
        expected = {round(c*fps) for c in cuts}
        if len(frames) != len(set(frames)) or set(frames) != expected:
            raise ValueError("Every XML track-cut candidate needs exactly one review")
        applied = []
        for item in sorted(items, key=lambda x: x["frame"]):
            if not item.get("reason"):
                raise ValueError("Each cut needs an explicit reason")
            action = item.get("action")
            if action in {"keep", "not_visible"}:
                continue
            if action != "adopt":
                raise ValueError("Unresolved cut decision")
            if not all(item.get(k) is True for k in
                       ("visible_cut_confirmed", "semantic_boundary_confirmed",
                        "speaker_boundary_preserved", "hidden_event_clear", "post_snap_audio_reviewed")):
                raise ValueError("Adoption requires per-cut visibility, semantic and audio checks")
            if not item.get("audio_evidence"):
                raise ValueError("Adoption requires audio review evidence")
            boundary = item.get("boundary_index")
            endpoint = item.get("endpoint", "change")
            valid_index = (0 <= boundary < len(chars) if endpoint == "start" else
                           0 < boundary <= len(chars) if endpoint == "end" else
                           0 < boundary < len(chars)) if type(boundary) is int else False
            if not valid_index:
                raise ValueError("Invalid character boundary")
            if crossing_protection(spans, boundary):
                raise ValueError("Cannot split a protected term")
            cut = item["frame"] / fps
            speech = item.get("speech_boundary")
            if type(speech) not in (int, float) or not math.isfinite(speech) or abs(speech-cut) > MAX_SNAP_FRAMES/fps + .001:
                raise ValueError("Reviewed speech boundary exceeds five frames")
            # The per-cut review may refine ASR timing; it may not silently
            # invent a location for text with no source mapping at all.
            adjacent = ([chars[boundary]] if endpoint == "start" else
                        [chars[boundary-1]] if endpoint == "end" else
                        [chars[boundary-1], chars[boundary]])
            if any(c.start is None or c.end is None for c in adjacent):
                raise ValueError("Unmapped text at reviewed boundary")
            endpoint = item.get("endpoint", "change")
            if endpoint in {"start", "end"}:
                # Gaps need independent endpoints. Do not stretch the previous
                # cue across silence or move the next speaker just to join cues.
                matches = [c for c in cues if (c.char_start if endpoint == "start" else c.char_end) == boundary]
                if len(matches) != 1:
                    raise ValueError("Reviewed endpoint does not identify one cue")
                target = matches[0]
                if endpoint == "start":
                    target.start = cut
                else:
                    target.end = cut
                ok, operation = True, "reviewed_" + endpoint
            elif endpoint != "change":
                raise ValueError("Unknown reviewed endpoint")
            elif "replace_boundary_index" in item:
                old = item["replace_boundary_index"]
                pair = next((i for i, (a, b) in enumerate(zip(cues, cues[1:]))
                             if a.char_end == b.char_start == old), None)
                if pair is None:
                    raise ValueError("Reviewed original boundary no longer exists")
                left, right = cues[pair:pair+2]
                if not (left.start < cut < right.end and left.char_start < boundary < right.char_end):
                    raise ValueError("Reviewed redistribution exceeds adjacent cues")
                cues[pair:pair+2] = [Cue(left.start, cut, left.char_start, boundary),
                                    Cue(cut, right.end, boundary, right.char_end)]
                ok, operation = True, "reviewed_adjacent_redistribution"
            else:
                ok, _, operation = apply_xml_boundary(cues, boundary, cut, MAX_SNAP_FRAMES/fps)
            if not ok:
                raise ValueError(f"Reviewed cut was not applied: {operation}")
            applied.append((boundary, cut, endpoint))
        # A subsequent nearby edit must not silently undo an earlier approval.
        for boundary, cut, endpoint in applied:
            if endpoint == "start":
                retained = any(c.char_start == boundary and abs(c.start-cut) < .001 for c in cues)
            elif endpoint == "end":
                retained = any(c.char_end == boundary and abs(c.end-cut) < .001 for c in cues)
            else:
                retained = any(a.char_end == b.char_start == boundary and
                               abs(a.end-cut) < .001 and abs(b.start-cut) < .001
                               for a, b in zip(cues, cues[1:]))
            if not retained:
                raise ValueError("Conflicting reviews: an adopted boundary was displaced")
        content = render_srt(cues, master, positions).encode("utf-8")
        grade = "reviewed_boundary_release"
    else:
        raise ValueError("Unknown review mode")
    after = cut_ledger(cues, cuts, fps)
    rate = xml_rate(xml, name)
    result = {"schema_version": 2, "fps_numerator": rate.numerator, "fps_denominator": rate.denominator,
              "reviewed_cuts": items, "alignment_sha256": sha(alignment) if alignment else None, "grade": grade, "reviewer": decision["reviewer"],
              "input_srt_sha256": sha(srt), "xml_sha256": sha(xml), "review_sha256": sha(review),
              "output_srt_sha256": hashlib.sha256(content).hexdigest(),
              "sequence_name": name, "fps": fps, "cue_count": len(cues),
              "xml_track_cut_candidates": len(cuts),
              "visibility_note": "Track boundaries are candidates; full-frame occlusion requires review.",
              "continuous_changes_before": sum(x["continuous_change"] for x in before),
              "continuous_changes_after": sum(x["continuous_change"] for x in after),
              "applied_reviewed_operations": len(applied), "exact": exact,
              "cut_ledger": after, "independent_generation_evaluation": False}
    for path in (output, report):
        path.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        handle.write(content)
    with report.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    # Verify bytes actually written, including SRT timestamp quantization.
    result["serialized_validation"] = validate_release(output, xml, report, name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("srt", "xml", "review", "output", "report"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--sequence-name")
    parser.add_argument("--alignment", type=Path)
    args = parser.parse_args()
    result = release(**vars(args))
    print(json.dumps({k: v for k, v in result.items() if k not in {"cut_ledger", "exact"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
