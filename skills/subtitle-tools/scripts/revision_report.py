#!/usr/bin/env python3
"""Create an objective structural diff between an AI SRT and a human revision."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from srt_style import Cue, format_timestamp, parse_srt


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(text: str) -> str:
    return re.sub(r"[\s|]+", "", text)


def cue_record(cue: Cue) -> dict[str, object]:
    return {
        "number": cue.number,
        "start": format_timestamp(cue.start_ms),
        "end": format_timestamp(cue.end_ms),
        "text": cue.visible_text,
    }


def overlap_ratio(left: Cue, right: Cue) -> float:
    overlap = min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)
    if overlap <= 0:
        return 0.0
    return overlap / max(1, min(left.end_ms - left.start_ms, right.end_ms - right.start_ms))


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def align(base: list[Cue], corrected: list[Cue]) -> list[tuple[list[Cue], list[Cue]]]:
    """Align cues into local groups without chaining through tiny boundary overlaps."""
    total = len(base) + len(corrected)
    groups = UnionFind(total)
    for base_index, base_cue in enumerate(base):
        for corrected_index, corrected_cue in enumerate(corrected):
            ratio = overlap_ratio(base_cue, corrected_cue)
            midpoint_distance = abs(
                (base_cue.start_ms + base_cue.end_ms)
                - (corrected_cue.start_ms + corrected_cue.end_ms)
            ) / 2
            close_match = (
                midpoint_distance <= 500
                and abs(base_cue.start_ms - corrected_cue.start_ms) <= 750
            )
            if ratio >= 0.20 or close_match:
                groups.union(base_index, len(base) + corrected_index)

    components: dict[int, tuple[list[Cue], list[Cue]]] = {}
    for index, cue in enumerate(base):
        root = groups.find(index)
        components.setdefault(root, ([], []))[0].append(cue)
    for index, cue in enumerate(corrected):
        root = groups.find(len(base) + index)
        components.setdefault(root, ([], []))[1].append(cue)

    ordered = list(components.values())
    ordered.sort(
        key=lambda pair: min(
            [cue.start_ms for cue in pair[0]] + [cue.start_ms for cue in pair[1]]
        )
    )
    return ordered


def classify_group(
    base: list[Cue], corrected: list[Cue], threshold_ms: int
) -> dict[str, object]:
    base_text = " | ".join(cue.visible_text for cue in base)
    corrected_text = " | ".join(cue.visible_text for cue in corrected)
    change_types: list[str] = []

    if not base:
        change_types.append("added")
    elif not corrected:
        change_types.append("removed")
    else:
        if compact(base_text) != compact(corrected_text):
            change_types.append("text")
        elif base_text != corrected_text:
            change_types.append("format_or_boundary")
        if len(base) != len(corrected):
            change_types.append("segmentation")

        base_start = min(cue.start_ms for cue in base)
        corrected_start = min(cue.start_ms for cue in corrected)
        base_end = max(cue.end_ms for cue in base)
        corrected_end = max(cue.end_ms for cue in corrected)
        if abs(base_start - corrected_start) > threshold_ms:
            change_types.append("start_timing")
        if abs(base_end - corrected_end) > threshold_ms:
            change_types.append("end_timing")

    return {
        "changed": bool(change_types),
        "change_types": change_types,
        "base": [cue_record(cue) for cue in base],
        "corrected": [cue_record(cue) for cue in corrected],
        "classification": [],
        "scope": "episode_lock",
        "review_status": "pending",
        "review_note": "",
    }


def build_report(
    base_path: Path,
    corrected_path: Path,
    job_id: str,
    reviewer: str,
    threshold_ms: int,
    include_unchanged: bool,
) -> dict[str, object]:
    base = parse_srt(base_path)
    corrected = parse_srt(corrected_path)
    all_groups = [
        classify_group(base_group, corrected_group, threshold_ms)
        for base_group, corrected_group in align(base, corrected)
    ]
    changed_groups = [group for group in all_groups if group["changed"]]
    output_groups = all_groups if include_unchanged else changed_groups
    counts: dict[str, int] = {}
    for group in changed_groups:
        for change_type in group["change_types"]:
            counts[change_type] = counts.get(change_type, 0) + 1

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "reviewer": reviewer,
        "base": {
            "file": base_path.name,
            "sha256": sha256(base_path),
            "segments": len(base),
        },
        "corrected": {
            "file": corrected_path.name,
            "sha256": sha256(corrected_path),
            "segments": len(corrected),
        },
        "summary": {
            "alignment_groups": len(all_groups),
            "changed_groups": len(changed_groups),
            "change_type_counts": counts,
        },
        "groups": output_groups,
        "learning_status": "episode_evidence_only",
        "notes": [
            "This report is structural evidence only.",
            "Use audio and context before assigning semantic, speaker, or timing causes.",
        ],
    }


def validate_review_report(path: Path) -> int:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("groups"), list):
        raise ValueError("review report must contain a groups list")
    incomplete: list[int] = []
    for index, group in enumerate(report["groups"], 1):
        if not isinstance(group, dict):
            raise ValueError("each review report group must be an object")
        status = str(group.get("review_status", "")).lower().strip()
        classification = group.get("classification", [])
        review_note = str(group.get("review_note", "")).strip()
        if (
            status not in {"resolved", "confirmed", "reviewed", "已確認", "已處理"}
            or not isinstance(classification, list)
            or not classification
            or not review_note
        ):
            incomplete.append(index)
    if incomplete:
        preview = ", ".join(str(index) for index in incomplete[:20])
        suffix = "..." if len(incomplete) > 20 else ""
        print(
            f"ERROR: {len(incomplete)} groups still need classification, "
            f"review_status and review_note: {preview}{suffix}",
            file=sys.stderr,
        )
        return 1
    print(f"{path}: {len(report['groups'])} changed groups reviewed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", type=Path, nargs="?")
    parser.add_argument("corrected", type=Path, nargs="?")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--reviewer", default="unknown")
    parser.add_argument("--threshold-ms", type=int, default=120)
    parser.add_argument("--include-unchanged", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--validate-review", type=Path)
    args = parser.parse_args()

    try:
        if args.validate_review:
            if args.base is not None or args.corrected is not None:
                parser.error(
                    "base and corrected must be omitted with --validate-review"
                )
            return validate_review_report(args.validate_review)
        if args.base is None or args.corrected is None:
            parser.error("base and corrected are required")
        report = build_report(
            args.base,
            args.corrected,
            args.job_id or args.base.stem.removesuffix("_AI基準"),
            args.reviewer,
            args.threshold_ms,
            args.include_unchanged,
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            args.out.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return 0
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
