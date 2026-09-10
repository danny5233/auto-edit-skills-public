#!/usr/bin/env python3
"""Conservatively identify rough-cut candidates in an SRT transcript.

The analyzer never edits media or XML.  Its JSON uses the ``intervals`` shape
accepted by ``roughcut_xml.py`` while retaining the evidence needed for human,
audio, and semantic review before those intervals are applied to a timeline.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence


TIMING_RE = re.compile(
    r"^(?P<sh>\d{2,}):(?P<sm>[0-5]\d):(?P<ss>[0-5]\d),(?P<sms>\d{3})"
    r"\s+-->\s+"
    r"(?P<eh>\d{2,}):(?P<em>[0-5]\d):(?P<es>[0-5]\d),(?P<ems>\d{3})$"
)

# These expressions are deliberately narrower than a keyword search.  In
# particular, ordinary exposition such as "再來生活上呢" is not a production cue.
UNAMBIGUOUS_PRODUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "discard_directive",
        re.compile(r"(?:這段不要|不要這段|這(?:一)?句不要|這題不要)"),
    ),
    (
        "recording_directive",
        re.compile(
            r"(?:再錄一次|重新錄|重錄|再拍一次|重新拍|"
            r"問題再錄一次|再幫我[^\n]{0,16}(?:問|講|錄)一次)"
        ),
    ),
    (
        "from_start_directive",
        re.compile(
            r"(?:再講一次[^\n]{0,8}從頭|從頭[^\n]{0,8}(?:再來|再講|再錄))"
        ),
    ),
    (
        "take_marker",
        re.compile(r"(?:^|[\s，,])(?:好)?過(?:了|囉|囉|嘍)?(?:$|[\s，,])", re.I),
    ),
    (
        "stage_direction",
        re.compile(
            r"(?:直接做收尾|我來問你來答|"
            r"你來問我來答|接下來[^\n]{0,12}(?<![A-Za-z])cue(?![A-Za-z])|"
            r"我們[^\n]{0,12}(?<![A-Za-z])cue(?![A-Za-z]))"
        ),
    ),
)

AMBIGUOUS_PRODUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("repeat_once", re.compile(r"(?:再來一次|再問一次|再講一次)")),
    ("restart_word", re.compile(r"(?:重來(?:一次)?|從頭)")),
    ("recording_problem", re.compile(r"(?:小卡|卡住|口誤|講錯)")),
    ("adjustment", re.compile(r"(?:^|[\s，,])(?:好)?調整一下(?:$|[\s，,])")),
    ("outro_planning", re.compile(r"(?:做收尾|我的收尾|直接說)")),
    (
        "question_rehearsal",
        re.compile(r"(?:這題要講|就問這個|然後回答|回答這個問題)"),
    ),
)

STAGE_CONTEXT_RE = re.compile(
    r"(?:準備|倒數|重來|再錄|重錄|再拍|從頭|小卡|卡住|"
    r"口誤|講錯|調整一下|收尾|(?<![A-Za-z])cue(?![A-Za-z])|"
    r"好過(?:了|囉|囉|嘍)?)",
    re.I,
)

REVIEW_NOTICE = (
    "All candidates are transcript heuristics. Confirm speaker function, semantics, "
    "true acoustic silence, and speech boundaries against the matching audio before "
    "using these intervals to split or label a Premiere timeline."
)


class SrtAnalyzerError(ValueError):
    """Raised when SRT input is malformed or unsafe to analyze."""


@dataclass(frozen=True)
class Cue:
    cue_id: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Finding:
    kind: str
    start_ms: int
    end_ms: int
    cue_ids: list[int]
    text: str
    recommended_label: str
    evidence: list[dict[str, Any]]


def _timestamp_to_ms(match: re.Match[str], prefix: str) -> int:
    return (
        int(match.group(f"{prefix}h")) * 3_600_000
        + int(match.group(f"{prefix}m")) * 60_000
        + int(match.group(f"{prefix}s")) * 1_000
        + int(match.group(f"{prefix}ms"))
    )


def format_timestamp(milliseconds: int) -> str:
    if milliseconds < 0:
        raise SrtAnalyzerError("timestamp cannot be negative")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(text: str) -> list[Cue]:
    """Strictly parse SRT text and return chronologically safe cues."""
    if not isinstance(text, str):
        raise SrtAnalyzerError("SRT input must be text")
    if "\x00" in text:
        raise SrtAnalyzerError("SRT input contains a NUL byte")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    normalized = normalized.strip()
    if not normalized:
        raise SrtAnalyzerError("SRT input is empty")

    blocks = re.split(r"\n[ \t]*\n+", normalized)
    cues: list[Cue] = []
    expected_id = 1
    previous: Cue | None = None

    for block_number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise SrtAnalyzerError(
                f"cue block {block_number} must contain id, timing, and text"
            )

        id_text = lines[0].strip()
        if not re.fullmatch(r"\d+", id_text):
            raise SrtAnalyzerError(
                f"cue block {block_number} has invalid id {lines[0]!r}"
            )
        cue_id = int(id_text)
        if cue_id != expected_id:
            raise SrtAnalyzerError(
                f"cue ids must be consecutive from 1: expected {expected_id}, "
                f"found {cue_id}"
            )

        timing_text = lines[1].strip()
        match = TIMING_RE.fullmatch(timing_text)
        if match is None:
            raise SrtAnalyzerError(
                f"cue {cue_id} has invalid timing line {lines[1]!r}"
            )
        start_ms = _timestamp_to_ms(match, "s")
        end_ms = _timestamp_to_ms(match, "e")
        if end_ms <= start_ms:
            raise SrtAnalyzerError(
                f"cue {cue_id} end must be later than its start"
            )

        text_lines = [line.rstrip() for line in lines[2:]]
        if not text_lines or any(not line.strip() for line in text_lines):
            raise SrtAnalyzerError(f"cue {cue_id} contains an empty text line")
        cue_text = "\n".join(text_lines)
        cue = Cue(cue_id, start_ms, end_ms, cue_text)

        if previous is not None and cue.start_ms < previous.end_ms:
            raise SrtAnalyzerError(
                f"cue {cue_id} overlaps cue {previous.cue_id}"
            )
        cues.append(cue)
        previous = cue
        expected_id += 1

    return cues


def _compact(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        char
        for char in normalized
        if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def _display_text(cues: Sequence[Cue]) -> str:
    return " / ".join(" ".join(cue.text.splitlines()) for cue in cues)


def _countdown_fragment(text: str) -> str | None:
    compact = _compact(text).translate(str.maketrans({"三": "3", "二": "2", "一": "1"}))
    compact = re.sub(r"(?:okay|ok|準備|好|哦|喔|啊|嗯|囉|囉|嘍)", "", compact)
    return compact if re.fullmatch(r"[321]+", compact or "") else None


def _looks_like_explicit_countdown(text: str) -> bool:
    compact = _compact(text).translate(str.maketrans({"三": "3", "二": "2", "一": "1"}))
    return compact.endswith("321") and ("準備" in compact or len(compact) <= 8)


def _countdown_ranges(cues: Sequence[Cue]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    consumed: set[int] = set()
    for start in range(len(cues)):
        if start in consumed:
            continue
        combined = ""
        for end in range(start, min(start + 3, len(cues))):
            if end > start and cues[end].start_ms - cues[end - 1].end_ms > 600:
                break
            fragment = _countdown_fragment(cues[end].text)
            if fragment is None:
                break
            combined += fragment
            if combined == "321":
                ranges.append((start, end))
                consumed.update(range(start, end + 1))
                break
            if not "321".startswith(combined):
                break
    return ranges


def _matches_unambiguous(text: str) -> list[str]:
    return [name for name, pattern in UNAMBIGUOUS_PRODUCTION_PATTERNS if pattern.search(text)]


def _matches_ambiguous(text: str) -> list[str]:
    return [name for name, pattern in AMBIGUOUS_PRODUCTION_PATTERNS if pattern.search(text)]


def _has_adjacent_production_evidence(cues: Sequence[Cue], index: int) -> bool:
    lower = max(0, index - 3)
    upper = min(len(cues), index + 4)
    for other_index in range(lower, upper):
        if other_index == index:
            continue
        text = cues[other_index].text
        if _matches_unambiguous(text) or STAGE_CONTEXT_RE.search(text):
            return True
        fragment = _countdown_fragment(text)
        if fragment == "321":
            return True
    return False


def _production_findings(cues: Sequence[Cue]) -> list[Finding]:
    findings: list[Finding] = []
    countdown_ranges = _countdown_ranges(cues)
    countdown_members = {
        cue_index
        for start, end in countdown_ranges
        for cue_index in range(start, end + 1)
    }

    for start, end in countdown_ranges:
        selected = list(cues[start : end + 1])
        findings.append(
            Finding(
                kind="production_countdown",
                start_ms=selected[0].start_ms,
                end_ms=selected[-1].end_ms,
                cue_ids=[cue.cue_id for cue in selected],
                text=_display_text(selected),
                recommended_label="red",
                evidence=[
                    {
                        "type": "countdown",
                        "detail": "standalone or stage-form 3-2-1 countdown",
                    }
                ],
            )
        )

    for index, cue in enumerate(cues):
        unambiguous = _matches_unambiguous(cue.text)
        ambiguous = _matches_ambiguous(cue.text)

        # A countdown embedded at the end of a longer rehearsal line is a
        # candidate, but is red only when surrounding production evidence exists.
        compact = _compact(cue.text).translate(
            str.maketrans({"三": "3", "二": "2", "一": "1"})
        )
        explicit_mixed_countdown = (
            index not in countdown_members and _looks_like_explicit_countdown(cue.text)
        )
        mixed_countdown = (
            index not in countdown_members
            and not explicit_mixed_countdown
            and compact.endswith("321")
        )
        if explicit_mixed_countdown:
            unambiguous.append("stage_countdown")

        if not unambiguous and not ambiguous and not mixed_countdown:
            continue

        adjacent_evidence = _has_adjacent_production_evidence(cues, index)
        if unambiguous:
            label = "red"
        elif adjacent_evidence:
            label = "red"
        else:
            label = "yellow"

        evidence: list[dict[str, Any]] = []
        for pattern_name in unambiguous:
            evidence.append(
                {
                    "type": "production_phrase",
                    "pattern": pattern_name,
                    "strength": "unambiguous",
                }
            )
        for pattern_name in ambiguous:
            evidence.append(
                {
                    "type": "production_phrase",
                    "pattern": pattern_name,
                    "strength": "context_required",
                }
            )
        if explicit_mixed_countdown or mixed_countdown:
            evidence.append(
                {
                    "type": (
                        "countdown" if explicit_mixed_countdown else "mixed_countdown"
                    ),
                    "detail": "cue ends with a possible 3-2-1 production countdown",
                }
            )
        if adjacent_evidence and not unambiguous:
            evidence.append(
                {
                    "type": "adjacent_production_context",
                    "detail": "nearby cue supplies recording or retake context",
                }
            )

        findings.append(
            Finding(
                kind="production_instruction",
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                cue_ids=[cue.cue_id],
                text=_display_text([cue]),
                recommended_label=label,
                evidence=evidence,
            )
        )
    return findings


def _common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def _has_retake_context(cues: Sequence[Cue], start: int, end: int) -> bool:
    lower = max(0, start - 3)
    upper = min(len(cues), end + 3)
    for cue in cues[lower:upper]:
        if _matches_unambiguous(cue.text):
            return True
        if STAGE_CONTEXT_RE.search(cue.text):
            return True
        if _countdown_fragment(cue.text) == "321":
            return True
    return False


def _nearby_repetition_findings(cues: Sequence[Cue]) -> list[Finding]:
    findings: list[Finding] = []
    for earlier_index, earlier in enumerate(cues):
        earlier_text = _compact(earlier.text)
        if len(earlier_text) < 5:
            continue
        for later_index in range(
            earlier_index + 1, min(earlier_index + 4, len(cues))
        ):
            later = cues[later_index]
            if later.start_ms - earlier.end_ms > 10_000:
                break
            later_text = _compact(later.text)
            if len(later_text) < 5:
                continue

            similarity = SequenceMatcher(None, earlier_text, later_text).ratio()
            prefix_length = _common_prefix_length(earlier_text, later_text)
            relative_length = min(len(earlier_text), len(later_text)) / max(
                len(earlier_text), len(later_text)
            )
            is_candidate = similarity >= 0.82 or (
                prefix_length >= 6 and relative_length >= 0.45
            )
            if not is_candidate:
                continue

            replaced_cues = list(cues[earlier_index:later_index])
            has_context = _has_retake_context(cues, earlier_index, later_index)
            kind = "nearby_restart" if prefix_length >= 6 else "nearby_repetition"
            findings.append(
                Finding(
                    kind=kind,
                    start_ms=replaced_cues[0].start_ms,
                    end_ms=replaced_cues[-1].end_ms,
                    cue_ids=[cue.cue_id for cue in replaced_cues]
                    + [later.cue_id],
                    text=_display_text(replaced_cues),
                    recommended_label="red" if has_context else "yellow",
                    evidence=[
                        {
                            "type": "near_duplicate",
                            "earlier_cue_id": earlier.cue_id,
                            "later_cue_id": later.cue_id,
                            "similarity": round(similarity, 3),
                            "common_prefix_characters": prefix_length,
                            "later_text": _display_text([later]),
                        },
                        {
                            "type": "retake_context",
                            "present": has_context,
                            "detail": (
                                "production/retake evidence is nearby"
                                if has_context
                                else "no production context; preserve unless audio review confirms a restart"
                            ),
                        },
                    ],
                )
            )
    return findings


def _repeated_substring(text: str) -> str | None:
    compact = _compact(text)
    for size in range(min(10, len(compact) // 2), 2, -1):
        match = re.search(rf"(.{{{size}}})\1", compact)
        if match is not None:
            return match.group(1)
    return None


def _within_cue_repetition_findings(cues: Sequence[Cue]) -> list[Finding]:
    findings: list[Finding] = []
    for cue in cues:
        repeated = _repeated_substring(cue.text)
        if repeated is None:
            continue
        findings.append(
            Finding(
                kind="within_cue_repetition",
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                cue_ids=[cue.cue_id],
                text=_display_text([cue]),
                recommended_label="yellow",
                evidence=[
                    {
                        "type": "immediate_text_repetition",
                        "repeated_text": repeated,
                        "detail": "word-level timing and discourse intent are unavailable in SRT",
                    }
                ],
            )
        )
    return findings


def _gap_findings(cues: Sequence[Cue], threshold_ms: int) -> list[Finding]:
    findings: list[Finding] = []
    for previous, following in zip(cues, cues[1:]):
        gap_ms = following.start_ms - previous.end_ms
        if gap_ms < threshold_ms:
            continue
        findings.append(
            Finding(
                kind="cue_gap",
                start_ms=previous.end_ms,
                end_ms=following.start_ms,
                cue_ids=[previous.cue_id, following.cue_id],
                text=(
                    f"before: {_display_text([previous])} | "
                    f"after: {_display_text([following])}"
                ),
                recommended_label="yellow",
                evidence=[
                    {
                        "type": "cue_gap",
                        "duration_seconds": round(gap_ms / 1_000, 3),
                        "detail": "caption gap is not proof of acoustic silence",
                    }
                ],
            )
        )
    return findings


def _deduplicate_findings(findings: Sequence[Finding]) -> list[Finding]:
    by_key: dict[tuple[str, int, int, tuple[int, ...]], Finding] = {}
    precedence = {"yellow": 1, "red": 2}
    for finding in findings:
        key = (
            finding.kind,
            finding.start_ms,
            finding.end_ms,
            tuple(finding.cue_ids),
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = finding
            continue
        if precedence[finding.recommended_label] > precedence[existing.recommended_label]:
            existing.recommended_label = finding.recommended_label
        for evidence in finding.evidence:
            if evidence not in existing.evidence:
                existing.evidence.append(evidence)
    return sorted(
        by_key.values(),
        key=lambda item: (item.start_ms, item.end_ms, item.kind, item.cue_ids),
    )


def _finding_to_interval(finding: Finding, ordinal: int) -> dict[str, Any]:
    if finding.recommended_label not in {"red", "yellow"}:
        raise SrtAnalyzerError("internal error: invalid recommendation label")
    return {
        "id": f"candidate-{ordinal:04d}",
        "kind": finding.kind,
        "start_seconds": round(finding.start_ms / 1_000, 3),
        "end_seconds": round(finding.end_ms / 1_000, 3),
        "start": format_timestamp(finding.start_ms),
        "end": format_timestamp(finding.end_ms),
        # roughcut_xml.py consumes these three fields and ignores the evidence.
        "label": finding.recommended_label,
        "recommended_label": finding.recommended_label,
        "cue_ids": finding.cue_ids,
        "text": finding.text,
        "evidence": finding.evidence,
        "review_required": True,
        "review_notice": REVIEW_NOTICE,
    }


def analyze_cues(
    cues: Sequence[Cue], *, gap_seconds: float = 2.0, source_srt: str | None = None
) -> dict[str, Any]:
    """Return a roughcut_xml-compatible candidate report."""
    if not cues:
        raise SrtAnalyzerError("at least one cue is required")
    if not isinstance(gap_seconds, (int, float)) or isinstance(gap_seconds, bool):
        raise SrtAnalyzerError("gap_seconds must be a number")
    if not math.isfinite(float(gap_seconds)) or gap_seconds < 0:
        raise SrtAnalyzerError("gap_seconds must be finite and non-negative")
    threshold_ms = int(round(float(gap_seconds) * 1_000))

    findings = _deduplicate_findings(
        _gap_findings(cues, threshold_ms)
        + _production_findings(cues)
        + _nearby_repetition_findings(cues)
        + _within_cue_repetition_findings(cues)
    )
    intervals = [
        _finding_to_interval(finding, ordinal)
        for ordinal, finding in enumerate(findings, start=1)
    ]
    by_label = {
        label: sum(1 for interval in intervals if interval["recommended_label"] == label)
        for label in ("red", "yellow")
    }
    by_kind: dict[str, int] = {}
    for interval in intervals:
        by_kind[interval["kind"]] = by_kind.get(interval["kind"], 0) + 1

    return {
        "schema_version": 1,
        "source_srt": source_srt,
        "gap_threshold_seconds": round(threshold_ms / 1_000, 3),
        "review_required": True,
        "review_notice": REVIEW_NOTICE,
        "intervals": intervals,
        "summary": {
            "cue_count": len(cues),
            "candidate_count": len(intervals),
            "by_label": by_label,
            "by_kind": by_kind,
        },
    }


def analyze_srt(
    text: str, *, gap_seconds: float = 2.0, source_srt: str | None = None
) -> dict[str, Any]:
    return analyze_cues(
        parse_srt(text), gap_seconds=gap_seconds, source_srt=source_srt
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly parse an SRT and emit conservative red/yellow rough-cut "
            "candidate intervals as JSON. This command never reads or writes XML."
        )
    )
    parser.add_argument("srt", type=Path, help="input UTF-8 SRT file")
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=2.0,
        help="minimum caption gap to report (default: 2.0)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON output path; stdout is used when omitted",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing JSON output file"
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.indent < 0:
            raise SrtAnalyzerError("indent must be non-negative")
        raw = args.srt.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SrtAnalyzerError(f"input SRT is not valid UTF-8: {exc}") from exc

        report = analyze_srt(
            text,
            gap_seconds=args.gap_seconds,
            source_srt=str(args.srt),
        )
        output_text = json.dumps(report, ensure_ascii=False, indent=args.indent) + "\n"

        if args.output is None:
            sys.stdout.write(output_text)
        else:
            if args.srt.resolve() == args.output.resolve():
                raise SrtAnalyzerError("JSON output path must differ from input SRT")
            if args.output.exists() and not args.force:
                raise SrtAnalyzerError(
                    f"output already exists: {args.output} (use --force to replace it)"
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output_text, encoding="utf-8")
        return 0
    except (OSError, SrtAnalyzerError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
