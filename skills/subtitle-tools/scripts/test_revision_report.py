#!/usr/bin/env python3
"""Regression tests for human-revision comparison."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("revision_report.py")


def srt(blocks: list[tuple[str, str, str]]) -> str:
    return "\n\n".join(
        f"{index}\n{start} --> {end}\n{text}"
        for index, (start, end, text) in enumerate(blocks, 1)
    ) + "\n"


class RevisionReportTests(unittest.TestCase):
    def compare(self, base: str, corrected: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "show-ep01_AI基準.srt"
            corrected_path = root / "show-ep01_人工修正.srt"
            base_path.write_text(base, encoding="utf-8")
            corrected_path.write_text(corrected, encoding="utf-8")
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    str(base_path),
                    str(corrected_path),
                    "--job-id",
                    "show-ep01",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_detects_text_correction(self) -> None:
        report = self.compare(
            srt([("00:00:00,000", "00:00:02,000", "剪應字幕")]),
            srt([("00:00:00,000", "00:00:02,000", "剪映字幕")]),
        )
        self.assertEqual(report["summary"]["changed_groups"], 1)
        self.assertEqual(report["summary"]["change_type_counts"]["text"], 1)

    def test_detects_split_without_false_content_change(self) -> None:
        report = self.compare(
            srt([("00:00:00,000", "00:00:04,000", "我覺得這樣可以")]),
            srt(
                [
                    ("00:00:00,000", "00:00:02,000", "我覺得"),
                    ("00:00:02,000", "00:00:04,000", "這樣可以"),
                ]
            ),
        )
        counts = report["summary"]["change_type_counts"]
        self.assertEqual(counts["segmentation"], 1)
        self.assertNotIn("text", counts)

    def test_detects_timing_change_above_threshold(self) -> None:
        report = self.compare(
            srt([("00:00:00,000", "00:00:02,000", "同一句")]),
            srt([("00:00:00,300", "00:00:02,400", "同一句")]),
        )
        counts = report["summary"]["change_type_counts"]
        self.assertEqual(counts["start_timing"], 1)
        self.assertEqual(counts["end_timing"], 1)

    def test_omits_unchanged_groups_by_default(self) -> None:
        sample = srt([("00:00:00,000", "00:00:02,000", "完全相同")])
        report = self.compare(sample, sample)
        self.assertEqual(report["summary"]["changed_groups"], 0)
        self.assertEqual(report["groups"], [])

    def test_review_validation_rejects_pending_group(self) -> None:
        report = self.compare(
            srt([("00:00:00,000", "00:00:02,000", "舊切句")]),
            srt([("00:00:00,000", "00:00:02,000", "新切句")]),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-diff.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                ["python3", str(SCRIPT), "--validate-review", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("still need", result.stderr)

    def test_review_validation_accepts_classified_resolved_group(self) -> None:
        report = self.compare(
            srt([("00:00:00,000", "00:00:02,000", "兩人黏句")]),
            srt(
                [
                    ("00:00:00,000", "00:00:01,000", "第一人"),
                    ("00:00:01,000", "00:00:02,000", "第二人"),
                ]
            ),
        )
        for group in report["groups"]:
            group["classification"] = ["speaker_overlap", "segmentation"]
            group["review_status"] = "resolved"
            group["review_note"] = "已依兩條近講分軌拆開並回聽"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-diff.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                ["python3", str(SCRIPT), "--validate-review", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("changed groups reviewed", result.stdout)


if __name__ == "__main__":
    unittest.main()
