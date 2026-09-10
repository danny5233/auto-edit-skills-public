#!/usr/bin/env python3
"""Regression tests for subtitle-style validation."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("srt_style.py")


def cue(text: str, end: str = "00:00:02,200") -> str:
    return f"1\n00:00:00,000 --> {end}\n{text}\n"


class SubtitleStyleTests(unittest.TestCase):
    def validate(
        self, text: str, decisions: dict[str, object] | None = None
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "test.srt"
            srt.write_text(text, encoding="utf-8")
            command = ["python3", str(SCRIPT), "validate", str(srt), "--json"]
            if decisions is not None:
                decision_path = root / "decisions.json"
                decision_path.write_text(
                    json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
                )
                command.extend(["--decisions", str(decision_path)])
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            return json.loads(result.stdout)

    def test_preserves_spoken_and_english_spaces(self) -> None:
        result = self.validate(cue("對啊 哦嗯 就想要Stephen Curry"))
        messages = [issue["message"] for issue in result["issues"]]
        self.assertFalse(any("英文多字詞疑似被黏合" in item for item in messages))

    def test_rejects_multiline_cue(self) -> None:
        result = self.validate(cue("第一句\n第二句"))
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("只能有一行" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_explicit_linebreak_marker(self) -> None:
        result = self.validate(cue(r"第一句\N第二句"))
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("強制換行標記" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_flatten_joins_physical_lines_with_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            output = root / "output.srt"
            source.write_text(cue("第一句\n第二句"), encoding="utf-8")
            result = subprocess.run(
                ["python3", str(SCRIPT), "flatten", str(source), str(output)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("第一句 第二句", output.read_text(encoding="utf-8"))
            flattened = self.validate(output.read_text(encoding="utf-8"))
            self.assertEqual(flattened["errors"], 0)

    def test_allows_official_name_symbols_with_decision(self) -> None:
        result = self.validate(
            cue("整個(G)I-DLE"),
            {"allowed_official_terms": ["(G)I-DLE"]},
        )
        self.assertEqual(result["errors"], 0)

    def test_allows_half_width_colon_in_numeric_ratio(self) -> None:
        result = self.validate(cue("基本上是有點1:1的概念"))
        self.assertEqual(result["errors"], 0)

    def test_rejects_colon_outside_numeric_ratio(self) -> None:
        for text in ("一般句子:不保留冒號", "畫面比例1：1"):
            with self.subTest(text=text):
                result = self.validate(cue(text))
                self.assertGreater(result["errors"], 0)

    def test_rejects_collapsed_english_phrase(self) -> None:
        result = self.validate(cue("我的finalanswer"))
        self.assertGreater(result["errors"], 0)

    def test_warns_about_possible_english_word_split_between_cues(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\nStephen Cur\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\nry很厲害\n"
        )
        result = self.validate(text)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("單字被切開" in item for item in messages))
        self.assertGreater(result["warnings"], 0)

    def test_rejects_protected_term_split_between_cues(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n兩個馬東\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n石站在那裡\n"
        )
        result = self.validate(text, {"protected_terms": ["馬東石"]})
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("受保護詞" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_warns_about_suspicious_incomplete_boundary(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n我真的不\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n知道為什麼\n"
        )
        result = self.validate(text)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("未完成詞組" in item for item in messages))

    def test_accepts_natural_semantic_boundary(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n我今天很累\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n所以先回家\n"
        )
        result = self.validate(text)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertFalse(any("未完成詞組" in item for item in messages))

    def test_short_laugh_is_error(self) -> None:
        result = self.validate(cue("hahaha", "00:00:01,200"))
        self.assertGreater(result["errors"], 0)

    def test_decision_locks_exact_text(self) -> None:
        result = self.validate(
            cue("對啊哦嗯就想要有一起的東西"),
            {
                "exact": [
                    {
                        "start": "00:00:00,000",
                        "text": "對啊 哦嗯 就想要有一起的東西",
                    }
                ]
            },
        )
        self.assertGreater(result["errors"], 0)

    def test_lexical_repetition_is_not_rejected(self) -> None:
        result = self.validate(cue("拜拜 我陪你 你陪我"))
        self.assertEqual(result["errors"], 0)

    def test_hidden_filler_rejects_early_next_subtitle(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n下一句\n"
        )
        result = self.validate(
            text,
            {
                "hidden_events": [
                    {
                        "start": "00:00:01,000",
                        "end": "00:00:01,400",
                        "next_visible_start": "00:00:01,600",
                        "next_text": "下一句",
                    }
                ]
            },
        )
        self.assertGreater(result["errors"], 0)

    def test_hidden_filler_accepts_first_visible_word_onset(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,600\n前一句\n\n"
            "2\n00:00:01,600 --> 00:00:03,000\n下一句\n"
        )
        result = self.validate(
            text,
            {
                "hidden_events": [
                    {
                        "start": "00:00:01,000",
                        "end": "00:00:01,400",
                        "next_visible_start": "00:00:01,600",
                        "next_text": "下一句",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_hidden_filler_rejects_late_next_subtitle(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,800\n前一句\n\n"
            "2\n00:00:01,800 --> 00:00:03,000\n下一句\n"
        )
        result = self.validate(
            text,
            {
                "hidden_events": [
                    {
                        "start": "00:00:01,000",
                        "end": "00:00:01,400",
                        "next_visible_start": "00:00:01,600",
                        "next_text": "下一句",
                    }
                ]
            },
        )
        self.assertGreater(result["errors"], 0)

    def test_functional_short_reaction_is_not_rejected(self) -> None:
        result = self.validate(cue("喔女生", "00:00:00,800"))
        self.assertEqual(result["errors"], 0)

    def test_unresolved_speaker_check_is_error(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "speaker_checks": [
                    {
                        "start": "00:00:00,000",
                        "end": "00:00:01,000",
                        "type": "speaker_switch",
                        "result": "尚未判斷",
                        "evidence": "兩軌皆有聲音",
                        "status": "pending",
                    }
                ]
            },
        )
        self.assertGreater(result["errors"], 0)

    def test_resolved_speaker_check_is_accepted(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "speaker_checks": [
                    {
                        "start": "00:00:00,000",
                        "end": "00:00:01,000",
                        "type": "speaker_switch",
                        "result": "同一講者 另一軌為串音",
                        "evidence": "主軌持續發聲且切點落在詞中",
                        "status": "resolved",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_requires_unique_speaker_coverage_for_every_visible_cue(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n第一位講者\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n第二位講者\n"
        )
        result = self.validate(
            text,
            {
                "speaker_coverage_required": True,
                "visible_cue_speakers": [
                    {
                        "start": "00:00:00,000",
                        "text": "第一位講者",
                        "speaker": "甲",
                        "source_track": "TRACK01",
                        "speaker_count": 1,
                        "status": "resolved",
                        "evidence": "TRACK01為近講主聲源",
                    }
                ],
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("缺少唯一講者覆蓋" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_visible_cue_with_two_speakers(self) -> None:
        result = self.validate(
            cue("兩個人被黏在一起"),
            {
                "speaker_coverage_required": True,
                "visible_cue_speakers": [
                    {
                        "start": "00:00:00,000",
                        "text": "兩個人被黏在一起",
                        "speaker": "甲與乙",
                        "source_track": "TRACK01+TRACK02",
                        "speaker_count": 2,
                        "status": "resolved",
                        "evidence": "兩條近講主軌依序發聲",
                    }
                ],
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("只能有一位講者" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_accepts_complete_unique_speaker_coverage(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "speaker_coverage_required": True,
                "visible_cue_speakers": [
                    {
                        "start": "00:00:00,000",
                        "text": "我就是很喜歡",
                        "speaker": "趴控",
                        "source_track": "TRACK01",
                        "speaker_count": 1,
                        "status": "resolved",
                        "evidence": "TRACK01為近講主聲源",
                    }
                ],
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_accepts_main_speaker_track_waveform_onset(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "source_timing_checks": [
                    {
                        "subtitle_start": "00:00:00,000",
                        "cue_text": "我就是很喜歡",
                        "speaker": "趴控",
                        "source_track": "TRACK01",
                        "timing_authority": "TRACK01",
                        "waveform_onset": "00:00:00,000",
                        "other_tracks_role": "speaker_overlap_only",
                        "mode": "source_track",
                        "status": "resolved",
                        "evidence": "TRACK01為近講主聲源 其他軌為串音",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_rejects_crosstalk_track_as_timing_authority(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "source_timing_checks": [
                    {
                        "subtitle_start": "00:00:00,000",
                        "cue_text": "我就是很喜歡",
                        "speaker": "趴控",
                        "source_track": "TRACK01",
                        "timing_authority": "TRACK02",
                        "waveform_onset": "00:00:00,000",
                        "other_tracks_role": "speaker_overlap_only",
                        "mode": "source_track",
                        "status": "resolved",
                        "evidence": "錯把較早出現的串音軌當作開口",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("時間權威" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_subtitle_start_before_main_track_waveform_onset(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "source_timing_checks": [
                    {
                        "subtitle_start": "00:00:00,000",
                        "cue_text": "我就是很喜歡",
                        "speaker": "趴控",
                        "source_track": "TRACK01",
                        "timing_authority": "TRACK01",
                        "waveform_onset": "00:00:00,120",
                        "other_tracks_role": "speaker_overlap_only",
                        "mode": "source_track",
                        "status": "resolved",
                        "evidence": "他軌串音比主講者近講波形更早",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("波形頭" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_accepts_main_track_onset_snapped_to_xml_within_five_frames(self) -> None:
        result = self.validate(
            cue("我就是很喜歡"),
            {
                "source_timing_checks": [
                    {
                        "subtitle_start": "00:00:00,000",
                        "cue_text": "我就是很喜歡",
                        "speaker": "趴控",
                        "source_track": "TRACK01",
                        "timing_authority": "TRACK01",
                        "waveform_onset": "00:00:00,100",
                        "other_tracks_role": "speaker_overlap_only",
                        "mode": "source_track_xml",
                        "xml_cut": "00:00:00,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "status": "resolved",
                        "evidence": "TRACK01真實開口距XML剪輯點3幀",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_accepts_word_aware_xml_cut_within_five_frames(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n他都沒有發達應該是\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n小手臂吧小手臂\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,300",
                        "final_boundary": "00:00:01,000",
                        "mode": "word_aware",
                        "status": "resolved",
                        "evidence": "原音詞級開口與 Premiere XML",
                        "speech_boundary": "00:00:00,950",
                        "before_text": "他都沒有發達應該是",
                        "after_text": "小手臂吧小手臂",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_rejects_xml_snap_beyond_five_frames(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,000",
                        "final_boundary": "00:00:01,000",
                        "mode": "timing_only",
                        "status": "resolved",
                        "evidence": "只有 SRT 與 XML",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("超過 5 幀" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_xml_snap_that_conflicts_with_hidden_event(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_post_snap_review_required": True,
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,900",
                        "final_boundary": "00:00:01,000",
                        "mode": "timing_only",
                        "status": "resolved",
                        "evidence": "剪輯點位於原邊界3幀內",
                        "post_snap_audio_check": "passed",
                        "hidden_event_conflict": True,
                        "speaker_boundary_preserved": True,
                    }
                ],
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("落入被省略聲音事件" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_accepts_xml_snap_after_audio_and_speaker_recheck(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_post_snap_review_required": True,
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,900",
                        "final_boundary": "00:00:01,000",
                        "mode": "timing_only",
                        "status": "resolved",
                        "evidence": "剪輯點位於原邊界3幀內",
                        "post_snap_audio_check": "passed",
                        "hidden_event_conflict": False,
                        "speaker_boundary_preserved": True,
                    }
                ],
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_rejects_deprecated_one_second_snap_window(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 30,
                        "original_boundary": "00:00:00,900",
                        "final_boundary": "00:00:01,000",
                        "mode": "timing_only",
                        "status": "resolved",
                        "evidence": "只有 SRT 與 XML",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("必須固定為前後 5 幀" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_word_aware_xml_cut_without_resegmented_text(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n他都沒有發達\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n應該是小手臂吧小手臂\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,850",
                        "final_boundary": "00:00:01,000",
                        "mode": "word_aware",
                        "status": "resolved",
                        "evidence": "原音證明 應該是 位於剪輯點前",
                        "speech_boundary": "00:00:00,950",
                        "before_text": "他都沒有發達應該是",
                        "after_text": "小手臂吧小手臂",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("剪輯點前文字不符" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_rejects_word_aware_xml_cut_far_from_speech_boundary(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n他都沒有發達應該是\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n小手臂吧小手臂\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,300",
                        "final_boundary": "00:00:01,000",
                        "mode": "word_aware",
                        "status": "resolved",
                        "evidence": "原音詞級開口與 Premiere XML",
                        "speech_boundary": "00:00:00,700",
                        "before_text": "他都沒有發達應該是",
                        "after_text": "小手臂吧小手臂",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("真實詞彙邊界" in item for item in messages))
        self.assertGreater(result["errors"], 0)


if __name__ == "__main__":
    unittest.main()
