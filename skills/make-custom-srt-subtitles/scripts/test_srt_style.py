#!/usr/bin/env python3
"""Regression tests for subtitle-style validation."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("srt_style.py")
V3_TEXT_ORACLE_BASENAME = "editorial_snap_candidate_v3.srt"
FORMAL_CUT_AUDIT_BASENAME = "full_wordaware_cut_audit_v3.json"


def cue(text: str, end: str = "00:00:02,200") -> str:
    return f"1\n00:00:00,000 --> {end}\n{text}\n"


TEST_POLICY_ID = "USR-EP-TIMING-TEST-V3"


def stamp_ms(value: str) -> int:
    hours, minutes, remainder = value.split(":", 2)
    seconds, millis = remainder.split(",", 1)
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(
        millis
    )


def test_nominal_frame(value: str) -> int:
    return stamp_ms(value) * 30000 // (1000 * 1001)


def test_serialized_frame(frame: int) -> str:
    milliseconds = (frame * 1001 + 29) // 30
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def test_sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_canonical_json_sha256(value: object) -> str:
    return test_sha256_bytes(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def job_aware_evidence_fixture(
    root: Path, *, write_local_oracle: bool = True
) -> tuple[Path, Path, dict[str, object]]:
    job_root = root / "test-job"
    draft = job_root / "05_draft"
    delivery = job_root / "05_delivery"
    analysis = job_root / "04_analysis"
    for directory in (draft, delivery, analysis):
        directory.mkdir(parents=True, exist_ok=True)

    candidate_path = root / "candidate.srt"
    candidate_text = cue("測試")
    candidate_path.write_text(candidate_text, encoding="utf-8")
    oracle_path = draft / V3_TEXT_ORACLE_BASENAME
    if write_local_oracle:
        oracle_path.write_text(candidate_text, encoding="utf-8")

    frames = list(range(400, 716))
    audit_records = [
        {
            "cut_id": f"XML-HARD-{frame}",
            "frame": frame,
            "roundtrip_safe_srt_timestamp": test_serialized_frame(frame),
            "classification": "reject_no_safe_boundary",
            "disposition": "reject",
            "selected_candidate": None,
        }
        for frame in frames
    ]
    audit = {
        "schema_version": "test-job-aware-1.0.0",
        "job_id": "test-job-aware",
        "fps_numerator": 30000,
        "fps_denominator": 1001,
        "snap_window_frames": 5,
        "hard_cut_count": 316,
        "record_count": 316,
        "all_cuts_accounted_for": True,
        "records": audit_records,
    }
    audit_path = analysis / FORMAL_CUT_AUDIT_BASENAME
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    stream_hash = test_sha256_bytes("測試".encode("utf-8"))
    decisions: dict[str, object] = {
        "job_id": "test-job-aware",
        "visible_text_stream_lock": {
            "reference_srt": V3_TEXT_ORACLE_BASENAME,
            "reference_srt_sha256": test_sha256_bytes(candidate_text.encode("utf-8")),
            "reference_visible_text_stream_sha256": stream_hash,
            "candidate_visible_text_stream_sha256": stream_hash,
            "join_method": "concatenate_visible_cue_text_without_delimiter",
            "conserved": True,
            "status": "locked",
        },
        "xml_cut_audit_lock": {
            "path": f"../04_analysis/{FORMAL_CUT_AUDIT_BASENAME}",
            "sha256": test_sha256_bytes(audit_path.read_bytes()),
            "schema_version": "test-job-aware-1.0.0",
            "expected_hard_cut_count": 316,
            "status": "locked",
        },
        "xml_edit_checks": [
            {
                "cut": test_serialized_frame(frame),
                "xml_frame": frame,
                "original_boundary": test_serialized_frame(frame),
                "final_boundary": test_serialized_frame(frame),
                "mode": "not_used",
                "status": "not_used",
                "evidence": "formal audit rejected this hard cut",
                "reason": "no safe retained-word boundary",
            }
            for frame in frames
        ],
    }
    return job_root, candidate_path, decisions


def episode_policy(
    *, allow_hidden_visual_overlap: bool = False, sequence_zero: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {
        "policy_id": TEST_POLICY_ID,
        "scope": {"job_id": "test-job", "cross_episode_reuse": False},
        "authority": "latest_explicit_user_instruction",
        "instruction": "本集以使用者確認的 Premiere nominal frame 畫面錨點為準",
        "status": "user_confirmed",
        "fps_numerator": 30000,
        "fps_denominator": 1001,
        "max_visual_lead_frames": 5,
        "allow_hidden_visual_overlap": allow_hidden_visual_overlap,
    }
    if sequence_zero:
        result["sequence_head_anchor"] = "00:00:00,000"
    return result


def visual_exact(
    start: str, text: str, xml_frame: int, anchor_kind: str
) -> dict[str, object]:
    return {
        "id": f"LOCK-{xml_frame}-{text}",
        "start": start,
        "text": text,
        "lock_kind": "user_confirmed_visual_anchor",
        "timing_policy_id": TEST_POLICY_ID,
        "anchor_kind": anchor_kind,
        "xml_frame": xml_frame,
        "status": "user_confirmed",
    }


def custom_source(
    *,
    start: str,
    text: str,
    waveform_onset: str,
    xml_frame: int,
    anchor_kind: str,
    pre_anchor_start: str | None = None,
    allow_hidden_visual_overlap: bool = False,
) -> dict[str, object]:
    pre_anchor_start = pre_anchor_start or waveform_onset
    reference_frame = test_nominal_frame(waveform_onset)
    return {
        "subtitle_start": start,
        "cue_text": text,
        "speaker": "測試講者",
        "source_track": "TRACK01",
        "timing_authority": TEST_POLICY_ID,
        "waveform_onset": waveform_onset,
        "other_tracks_role": "speaker_overlap_only",
        "mode": "user_confirmed_visual_anchor",
        "status": "confirmed",
        "evidence": "保留真實主軌開口並套用本集使用者確認的畫面錨點",
        "timing_policy_id": TEST_POLICY_ID,
        "anchor_kind": anchor_kind,
        "visual_anchor": start,
        "xml_frame": xml_frame,
        "fps_numerator": 30000,
        "fps_denominator": 1001,
        "max_visual_lead_frames": 5,
        "reference_boundary": waveform_onset,
        "reference_frame": reference_frame,
        "visual_lead_frames": reference_frame - xml_frame,
        "pre_anchor_subtitle_start": pre_anchor_start,
        "pre_anchor_display_frame": test_nominal_frame(pre_anchor_start),
        "source_track_role": (
            "speaker_and_waveform_evidence_not_display_timing_authority"
        ),
        "allow_hidden_visual_overlap": allow_hidden_visual_overlap,
    }


def custom_xml(
    *,
    cut: str,
    text: str,
    waveform_onset: str,
    xml_frame: int,
    original_boundary: str | None = None,
    allow_hidden_visual_overlap: bool = False,
    hidden_event_ids: list[str] | None = None,
) -> dict[str, object]:
    reference_frame = test_nominal_frame(waveform_onset)
    hidden_event_ids = list(hidden_event_ids or [])
    return {
        "cut": cut,
        "xml_frame": xml_frame,
        "fps": 30000 / 1001,
        "fps_numerator": 30000,
        "fps_denominator": 1001,
        "max_snap_frames": 5,
        "original_boundary": original_boundary or waveform_onset,
        "final_boundary": cut,
        "mode": "user_confirmed_visual_anchor",
        "status": "confirmed",
        "timing_policy_id": TEST_POLICY_ID,
        "anchor_kind": "xml_hard_cut",
        "reference_boundary": waveform_onset,
        "reference_frame": reference_frame,
        "visual_lead_frames": reference_frame - xml_frame,
        "before_text": "前一句",
        "after_text": text,
        "speaker": "測試講者",
        "source_track": "TRACK01",
        "waveform_onset": waveform_onset,
        "serialized_cut_roundtrip_passed": True,
        "post_snap_audio_check": "passed",
        "hidden_event_conflict": bool(hidden_event_ids),
        "hidden_event_ids": hidden_event_ids,
        "allow_hidden_visual_overlap": allow_hidden_visual_overlap,
        "speaker_boundary_preserved": True,
        "manual_listening_claimed": False,
        "evidence": "Premiere visible hard cut and retained source-track onset",
    }


def xml_anchor_fixture(
    *,
    cut: str,
    waveform_onset: str,
    xml_frame: int,
    text: str = "後一句",
    before_end: str | None = None,
    allow_hidden_visual_overlap: bool = False,
    hidden_event_ids: list[str] | None = None,
) -> tuple[str, dict[str, object]]:
    before_end = before_end or cut
    srt = (
        f"1\n00:00:00,000 --> {before_end}\n前一句\n\n"
        f"2\n{cut} --> 00:01:00,000\n{text}\n"
    )
    decisions = {
        "job_id": "test-job",
        "episode_timing_policy": episode_policy(
            allow_hidden_visual_overlap=allow_hidden_visual_overlap
        ),
        "exact": [visual_exact(cut, text, xml_frame, "xml_hard_cut")],
        "source_timing_checks": [
            custom_source(
                start=cut,
                text=text,
                waveform_onset=waveform_onset,
                xml_frame=xml_frame,
                anchor_kind="xml_hard_cut",
                allow_hidden_visual_overlap=allow_hidden_visual_overlap,
            )
        ],
        "xml_post_snap_review_required": True,
        "xml_edit_checks": [
            custom_xml(
                cut=cut,
                text=text,
                waveform_onset=waveform_onset,
                xml_frame=xml_frame,
                allow_hidden_visual_overlap=allow_hidden_visual_overlap,
                hidden_event_ids=hidden_event_ids,
            )
        ],
    }
    return srt, decisions


def sequence_zero_fixture(
    waveform_onset: str = "00:00:00,200",
) -> tuple[str, dict[str, object]]:
    text = "歡迎來到"
    decisions = {
        "job_id": "test-job",
        "episode_timing_policy": episode_policy(sequence_zero=True),
        "exact": [visual_exact("00:00:00,000", text, 0, "sequence_zero")],
        "source_timing_checks": [
            custom_source(
                start="00:00:00,000",
                text=text,
                waveform_onset=waveform_onset,
                xml_frame=0,
                anchor_kind="sequence_zero",
            )
        ],
    }
    return cue(text), decisions


def hidden_rebind(
    *,
    hidden_event_id: str,
    visual_anchor_start: str,
    next_visible_start: str,
    next_text: str,
    overlaps_hidden_audio: bool,
    visual_anchor_inside_hidden_interval: bool = False,
) -> dict[str, object]:
    return {
        "hidden_event_id": hidden_event_id,
        "audio_next_visible_start": next_visible_start,
        "visual_anchor_start": visual_anchor_start,
        "next_text": next_text,
        "visual_anchor_inside_hidden_interval": (
            visual_anchor_inside_hidden_interval
        ),
        "timing_policy_id": TEST_POLICY_ID,
        "visual_lead_interval_start": visual_anchor_start,
        "visual_lead_interval_end": next_visible_start,
        "visual_lead_interval_overlaps_hidden_audio": overlaps_hidden_audio,
        "allow_hidden_visual_overlap": overlaps_hidden_audio,
    }


class SubtitleStyleTests(unittest.TestCase):
    def run_validate(
        self,
        text: str,
        decisions: dict[str, object] | None = None,
        profile: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "test.srt"
            srt.write_text(text, encoding="utf-8")
            command = [sys.executable, str(SCRIPT), "validate", str(srt), "--json"]
            if decisions is not None:
                decision_path = root / "decisions.json"
                decision_path.write_text(
                    json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
                )
                command.extend(["--decisions", str(decision_path)])
            if profile is not None:
                command.extend(["--profile", str(profile)])
            return subprocess.run(
                command, text=True, capture_output=True, check=False
            )

    def validate(
        self,
        text: str,
        decisions: dict[str, object] | None = None,
        profile: Path | None = None,
    ) -> dict[str, object]:
        result = self.run_validate(text, decisions, profile)
        self.assertIn(result.returncode, {0, 1}, msg=result.stderr)
        return json.loads(result.stdout)

    def run_validate_paths(
        self, srt_path: Path, decisions_path: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "validate",
                str(srt_path),
                "--json",
                "--decisions",
                str(decisions_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def validate_reconciled_xml(
        self,
        mutate: object | None = None,
    ) -> dict[str, object]:
        mode = "source_track_xml_after_locked_text_prefix_waveform_reconciliation"
        cut = test_serialized_frame(30)
        onset = "00:00:01,120"
        raw_token_onset = "00:00:01,140"
        candidate = (
            f"1\n00:00:00,000 --> {cut}\n前一句\n\n"
            f"2\n{cut} --> 00:00:03,000\n但後一句\n"
        )
        reference = (
            "1\n00:00:00,000 --> 00:00:01,300\n前一句但\n\n"
            "2\n00:00:01,300 --> 00:00:03,000\n後一句\n"
        )
        mismatch = {
            "unmatched_visible_prefix": "但",
            "mapped_raw_prefix_token": {
                "token_id": "T02:000001",
                "text": "當",
                "start": raw_token_onset,
            },
            "first_literal_match": "後一句",
            "first_literal_target_index": 1,
            "raw_prefix_phrase": "當然",
            "visible_locked_prefix": "但",
        }
        selected = {
            "mode": "clean_inter_utterance_prefix_reconciliation",
            "status": "adopt",
            "accepted": True,
            "previous_cue": 1,
            "following_cue": 2,
            "text_before": {"left": "前一句", "right": "但後一句"},
            "text_after": {"left": "前一句", "right": "但後一句"},
            "global_text_conservation": True,
            "proposed_cut_frame": 30,
            "proposed_srt_start": cut,
            "speaker": "乙",
            "source_track": "TRACK02",
            "cross_speaker": True,
            "raw_gap": {
                "previous_speech_end": "00:00:00,900",
                "cut": "00:00:01,000",
                "following_reported_onset": "00:00:01,300",
            },
            "prefix_reconciliation": {
                "first_retained_token": {
                    "token_id": "T02:000001",
                    "text": "當",
                    "start": raw_token_onset,
                    "end": "00:00:01,240",
                },
                "prefix_mismatch": mismatch,
                "selected_onset": onset,
                "selection_mode": "locked_source_waveform_refined_onset_within_5f",
                "dominance": {"passed": True},
            },
            "hidden_conflicts": [],
            "hidden_conflict": False,
            "protected_conflicts": [],
            "post_check": {
                "onset_within_0_to_5_frames": True,
                "hidden_clear": True,
                "protected_clear": True,
                "speaker_boundary_preserved": True,
                "following_timing_authority_is_own_locked_source": True,
            },
            "reasons": [],
        }
        adopted_record = {
            "cut_id": "XML-HARD-30",
            "frame": 30,
            "roundtrip_safe_srt_timestamp": cut,
            "classification": "adopt_clean_gap_waveform_refined",
            "disposition": "adopt",
            "selected_candidate": selected,
        }
        audit_records = [adopted_record]
        for frame in range(31, 346):
            audit_records.append(
                {
                    "cut_id": f"XML-HARD-{frame}",
                    "frame": frame,
                    "roundtrip_safe_srt_timestamp": test_serialized_frame(frame),
                    "classification": "reject_no_safe_boundary",
                    "disposition": "reject",
                    "selected_candidate": None,
                }
            )
        audit = {
            "schema_version": "test-1.1.0",
            "job_id": "test-reconciled-job",
            "fps_numerator": 30000,
            "fps_denominator": 1001,
            "snap_window_frames": 5,
            "hard_cut_count": 316,
            "record_count": 316,
            "all_cuts_accounted_for": True,
            "records": audit_records,
        }
        stream_hash = test_sha256_bytes("前一句但後一句".encode("utf-8"))
        mismatch_hash = test_canonical_json_sha256(mismatch)
        prefix_hash = test_sha256_bytes("但".encode("utf-8"))
        audit_record_hash = test_canonical_json_sha256(adopted_record)
        shared = {
            "xml_frame": 30,
            "fps": 30000 / 1001,
            "fps_numerator": 30000,
            "fps_denominator": 1001,
            "max_snap_frames": 5,
            "waveform_onset": onset,
            "refined_onset_frame": test_nominal_frame(onset),
            "cut_to_refined_onset_frames": test_nominal_frame(onset) - 30,
            "serialized_cut_roundtrip_passed": True,
            "reconciliation_method": "locked_source_waveform_refined_onset_within_5f",
            "raw_alignment_mismatch": mismatch,
            "raw_alignment_mismatch_present": True,
            "raw_alignment_mismatch_sha256": mismatch_hash,
            "raw_alignment_mismatch_preserved": True,
            "raw_token_onset": raw_token_onset,
            "locked_visible_prefix": "但",
            "locked_visible_prefix_sha256": prefix_hash,
            "locked_visible_text_stream_sha256": stream_hash,
            "formal_audit_cut_id": "XML-HARD-30",
            "formal_audit_record_sha256": audit_record_hash,
            "outgoing_speech_end": "00:00:00,900",
            "outgoing_speaker": "甲",
            "outgoing_source_track": "TRACK01",
            "cross_speaker": True,
            "timing_source_role": "incoming_locked_source_track_only",
            "outgoing_track_used_for_timing": False,
            "source_track_dominance_passed": True,
            "speaker_boundary_preserved": True,
        }
        source_record = {
            "subtitle_start": cut,
            "cue_text": "但後一句",
            "speaker": "乙",
            "source_track": "TRACK02",
            "timing_authority": "TRACK02",
            "other_tracks_role": "speaker_overlap_only",
            "mode": mode,
            "status": "resolved",
            "evidence": "incoming TRACK02 locked-prefix waveform refined onset",
            "xml_cut": cut,
            **shared,
        }
        xml_record = {
            "cut": cut,
            "original_boundary": "00:00:01,300",
            "final_boundary": cut,
            "mode": mode,
            "status": "resolved",
            "evidence": "formal audit and incoming source-track waveform",
            "speech_boundary": onset,
            "before_text": "前一句",
            "after_text": "但後一句",
            "pre_resegment_text_stream": "前一句但後一句",
            "post_resegment_text_stream": "前一句但後一句",
            "text_conserved": True,
            "speaker": "乙",
            "source_track": "TRACK02",
            "post_snap_audio_check": "passed",
            "hidden_event_conflict": False,
            "hidden_event_ids": [],
            **shared,
        }
        xml_records = [xml_record]
        for frame in range(31, 346):
            boundary = test_serialized_frame(frame)
            xml_records.append(
                {
                    "cut": boundary,
                    "xml_frame": frame,
                    "original_boundary": boundary,
                    "final_boundary": boundary,
                    "mode": "not_used",
                    "status": "not_used",
                    "evidence": "formal audit rejected this hard cut",
                    "reason": "no safe retained-word boundary",
                }
            )
        decisions: dict[str, object] = {
            "job_id": "test-reconciled-job",
            "speaker_coverage_required": True,
            "visible_cue_speakers": [
                {
                    "start": "00:00:00,000",
                    "text": "前一句",
                    "speaker": "甲",
                    "source_track": "TRACK01",
                    "speaker_count": 1,
                    "status": "resolved",
                    "evidence": "TRACK01 outgoing locked source",
                },
                {
                    "start": cut,
                    "text": "但後一句",
                    "speaker": "乙",
                    "source_track": "TRACK02",
                    "speaker_count": 1,
                    "status": "resolved",
                    "evidence": "TRACK02 incoming locked source",
                },
            ],
            "source_timing_checks": [source_record],
            "xml_post_snap_review_required": True,
            "xml_edit_checks": xml_records,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate_path = root / "candidate.srt"
            reference_path = root / "reference.srt"
            audit_path = root / "formal-audit.json"
            decisions_path = root / "decisions.json"
            candidate_path.write_text(candidate, encoding="utf-8")
            reference_path.write_text(reference, encoding="utf-8")
            audit_path.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            decisions["visible_text_stream_lock"] = {
                "reference_srt": "reference.srt",
                "reference_srt_sha256": test_sha256_bytes(reference_path.read_bytes()),
                "reference_visible_text_stream_sha256": stream_hash,
                "candidate_visible_text_stream_sha256": stream_hash,
                "join_method": "concatenate_visible_cue_text_without_delimiter",
                "conserved": True,
                "status": "locked",
            }
            decisions["xml_cut_audit_lock"] = {
                "path": "formal-audit.json",
                "sha256": test_sha256_bytes(audit_path.read_bytes()),
                "schema_version": "test-1.1.0",
                "expected_hard_cut_count": 316,
                "status": "locked",
            }
            if mutate is not None:
                assert callable(mutate)
                mutate(decisions, audit_path, reference_path)
            decisions_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate",
                    str(candidate_path),
                    "--json",
                    "--decisions",
                    str(decisions_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn(result.returncode, {0, 1}, msg=result.stderr)
            return json.loads(result.stdout)

    def test_job_aware_evidence_accepts_same_blob_in_all_release_locations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(root)
            decision_blob = json.dumps(decisions, ensure_ascii=False)
            decision_paths = (
                job_root / "05_draft" / "decisions.json",
                job_root / "05_delivery" / "decisions.json",
                job_root / "decisions.json",
            )
            for decision_path in decision_paths:
                decision_path.write_text(decision_blob, encoding="utf-8")

            for decision_path in decision_paths:
                with self.subTest(decisions=str(decision_path)):
                    result = self.run_validate_paths(candidate_path, decision_path)
                    self.assertEqual(result.returncode, 0, msg=result.stderr)
                    self.assertEqual(json.loads(result.stdout)["errors"], 0)

    def test_job_aware_evidence_fallback_still_enforces_declared_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(root)
            text_lock = decisions["visible_text_stream_lock"]
            assert isinstance(text_lock, dict)
            text_lock["reference_srt_sha256"] = "0" * 64
            decision_path = job_root / "decisions.json"
            decision_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )

            result = self.run_validate_paths(candidate_path, decision_path)
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            messages = [issue["message"] for issue in json.loads(result.stdout)["issues"]]
            self.assertTrue(any("參考 SRT SHA-256 不符" in item for item in messages))

    def test_job_aware_xml_fallback_still_enforces_declared_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(root)
            audit_lock = decisions["xml_cut_audit_lock"]
            assert isinstance(audit_lock, dict)
            audit_lock["sha256"] = "0" * 64
            decision_path = job_root / "decisions.json"
            decision_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )

            result = self.run_validate_paths(candidate_path, decision_path)
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            messages = [issue["message"] for issue in json.loads(result.stdout)["issues"]]
            self.assertTrue(any("audit SHA-256 不符" in item for item in messages))

    def test_job_aware_evidence_keeps_existing_direct_path_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(root)
            direct_oracle = job_root / "05_delivery" / V3_TEXT_ORACLE_BASENAME
            direct_oracle.write_text(cue("不同文字"), encoding="utf-8")
            decision_path = job_root / "05_delivery" / "decisions.json"
            decision_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )

            result = self.run_validate_paths(candidate_path, decision_path)
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            messages = [issue["message"] for issue in json.loads(result.stdout)["issues"]]
            self.assertTrue(any("參考 SRT SHA-256 不符" in item for item in messages))

    def test_job_aware_evidence_fallback_does_not_search_other_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(
                root, write_local_oracle=False
            )
            other_oracle = root / "other-job" / "05_draft" / V3_TEXT_ORACLE_BASENAME
            other_oracle.parent.mkdir(parents=True)
            other_oracle.write_text(cue("測試"), encoding="utf-8")
            decision_path = job_root / "decisions.json"
            decision_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )

            result = self.run_validate_paths(candidate_path, decision_path)
            self.assertEqual(result.returncode, 2)
            self.assertIn(V3_TEXT_ORACLE_BASENAME, result.stderr)

    def test_job_aware_evidence_fallback_rejects_ambiguous_job_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_root, candidate_path, decisions = job_aware_evidence_fixture(root)
            ambiguous_parent = job_root / "05_delivery"
            nested_draft = ambiguous_parent / "05_draft"
            nested_analysis = ambiguous_parent / "04_analysis"
            nested_draft.mkdir()
            nested_analysis.mkdir()
            (nested_draft / V3_TEXT_ORACLE_BASENAME).write_text(
                cue("測試"), encoding="utf-8"
            )
            decision_path = ambiguous_parent / "decisions.json"
            decision_path.write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
            )

            result = self.run_validate_paths(candidate_path, decision_path)
            self.assertEqual(result.returncode, 2)
            self.assertIn(V3_TEXT_ORACLE_BASENAME, result.stderr)

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
                [sys.executable, str(SCRIPT), "flatten", str(source), str(output)],
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

    def test_rejects_known_human_draft_typos(self) -> None:
        for text, correction in (("希望説", "說"), ("打開的時後", "時候")):
            with self.subTest(text=text):
                result = self.validate(cue(text))
                messages = [issue["message"] for issue in result["issues"]]
                self.assertTrue(any(correction in item for item in messages))
                self.assertGreater(result["errors"], 0)

    def test_uses_global_two_second_tail_fill_policy_without_profile(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:03,500 --> 00:00:05,000\n後一句\n"
        )
        result = self.validate(text)
        self.assertEqual(result["style_policy"]["scope"], "global")
        self.assertEqual(result["style_policy"]["tail_fill_threshold_ms"], 2000)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("超過尾端延續門檻 2000ms" in item for item in messages))



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

    def test_accepts_locked_prefix_waveform_reconciliation_with_cross_speaker(self) -> None:
        result = self.validate_reconciled_xml()
        self.assertEqual(result["errors"], 0)

    def test_reconciled_mode_rejects_six_frame_refined_onset(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            sources = decisions["source_timing_checks"]
            xml_checks = decisions["xml_edit_checks"]
            assert isinstance(sources, list) and isinstance(xml_checks, list)
            assert isinstance(sources[0], dict) and isinstance(xml_checks[0], dict)
            for record in (sources[0], xml_checks[0]):
                record["waveform_onset"] = "00:00:01,202"
                record["refined_onset_frame"] = 36
                record["cut_to_refined_onset_frames"] = 6
                record["raw_token_onset"] = "00:00:01,220"
            xml_checks[0]["speech_boundary"] = "00:00:01,202"

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("0 至 5" in item or "0 至 5 幀" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_outgoing_track_as_timing_source(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            for collection_name in ("source_timing_checks", "xml_edit_checks"):
                collection = decisions[collection_name]
                assert isinstance(collection, list) and isinstance(collection[0], dict)
                collection[0]["outgoing_track_used_for_timing"] = True

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("outgoing" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_changed_raw_mismatch(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            sources = decisions["source_timing_checks"]
            assert isinstance(sources, list) and isinstance(sources[0], dict)
            mismatch = sources[0]["raw_alignment_mismatch"]
            assert isinstance(mismatch, dict)
            mismatch["raw_prefix_phrase"] = "tampered"

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("raw alignment mismatch" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_requires_strict_passed_post_snap_audio_check(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            xml_checks = decisions["xml_edit_checks"]
            assert isinstance(xml_checks, list) and isinstance(xml_checks[0], dict)
            xml_checks[0]["post_snap_audio_check"] = "reviewed"

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("尚未完成原音回查" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_dishonest_hidden_clear(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            decisions["hidden_events"] = [
                {
                    "id": "HIDDEN-TEST",
                    "start": "00:00:01,010",
                    "end": "00:00:01,080",
                    "audio_start": "00:00:01,010",
                    "audio_end": "00:00:01,080",
                    "next_visible_start": "00:00:01,120",
                    "next_text": "但後一句",
                }
            ]

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("hidden conflict 衍生值不如實" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_tampered_formal_audit_hash(self) -> None:
        def mutate(
            decisions: dict[str, object], audit: Path, _reference: Path
        ) -> None:
            audit.write_text(audit.read_text(encoding="utf-8") + " ", encoding="utf-8")

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("audit SHA-256" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_incomplete_316_cut_ledger(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            xml_checks = decisions["xml_edit_checks"]
            assert isinstance(xml_checks, list)
            xml_checks.pop()

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("316 個 hard cuts" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_reconciled_mode_rejects_changed_global_visible_text_stream(self) -> None:
        def mutate(
            decisions: dict[str, object], _audit: Path, _reference: Path
        ) -> None:
            lock = decisions["visible_text_stream_lock"]
            assert isinstance(lock, dict)
            lock["candidate_visible_text_stream_sha256"] = "0" * 64

        result = self.validate_reconciled_xml(mutate)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("候選 SRT 的可見文字串 SHA-256" in item for item in messages))
        self.assertGreater(result["errors"], 0)

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

    def test_rejected_xml_candidate_preserves_audio_semantic_boundary(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:00,900\n前一句\n\n"
            "2\n00:00:00,900 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,000",
                        "fps": 30,
                        "max_snap_frames": 5,
                        "original_boundary": "00:00:00,900",
                        "final_boundary": "00:00:00,900",
                        "mode": "not_used",
                        "status": "resolved",
                        "evidence": "剪輯點落在同一詞發聲期間",
                        "reason": "保留聲音與語意邊界",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_rejected_xml_candidate_cannot_move_the_original_boundary(self) -> None:
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
                        "original_boundary": "00:00:00,900",
                        "final_boundary": "00:00:01,000",
                        "mode": "not_used",
                        "status": "resolved",
                        "evidence": "候選未採用",
                        "reason": "剪輯點破壞語意",
                    }
                ]
            },
        )
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("必須保留原本" in item for item in messages))
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

    def test_accepts_user_cue_49_10_to_49_05_by_nominal_frames(self) -> None:
        srt, decisions = xml_anchor_fixture(
            cut="00:00:49,216",
            waveform_onset="00:00:49,400",
            xml_frame=1475,
            text="我變硬了但也禿了",
        )
        continuous_delta_ms = stamp_ms("00:00:49,400") - stamp_ms(
            "00:00:49,216"
        )
        self.assertGreater(continuous_delta_ms, 5 * 1000 / (30000 / 1001))
        self.assertEqual(
            test_nominal_frame("00:00:49,400")
            - test_nominal_frame("00:00:49,216"),
            5,
        )
        result = self.validate(srt, decisions)
        self.assertEqual(result["errors"], 0)

    def test_nominal_frame_window_accepts_200ms_and_rejects_201ms(self) -> None:
        passing_srt, passing_decisions = sequence_zero_fixture("00:00:00,200")
        passing = self.validate(passing_srt, passing_decisions)
        self.assertEqual(test_nominal_frame("00:00:00,200"), 5)
        self.assertEqual(passing["errors"], 0)

        failing_srt, failing_decisions = sequence_zero_fixture("00:00:00,201")
        failing = self.validate(failing_srt, failing_decisions)
        messages = [issue["message"] for issue in failing["issues"]]
        self.assertEqual(test_nominal_frame("00:00:00,201"), 6)
        self.assertTrue(any("0 至 5 個 nominal frames" in item for item in messages))
        self.assertGreater(failing["errors"], 0)

    def test_rejects_visual_anchor_serialized_to_previous_frame(self) -> None:
        srt, decisions = xml_anchor_fixture(
            cut="00:00:49,215",
            waveform_onset="00:00:49,400",
            xml_frame=1475,
            text="我變硬了但也禿了",
        )
        self.assertEqual(test_nominal_frame("00:00:49,215"), 1474)
        result = self.validate(srt, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("反算" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_custom_visual_anchor_requires_episode_policy(self) -> None:
        srt, decisions = sequence_zero_fixture()
        decisions.pop("episode_timing_policy")
        result = self.validate(srt, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("缺少 episode_timing_policy" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_custom_visual_anchor_authority_must_be_policy_id(self) -> None:
        srt, decisions = sequence_zero_fixture()
        decisions["source_timing_checks"][0]["timing_authority"] = "TRACK01"
        result = self.validate(srt, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("timing_authority" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_sequence_zero_is_restricted_to_cue_one_at_zero(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n歡迎來到\n"
        )
        source = custom_source(
            start="00:00:01,000",
            text="歡迎來到",
            waveform_onset="00:00:01,100",
            xml_frame=test_nominal_frame("00:00:01,000"),
            anchor_kind="sequence_zero",
        )
        decisions = {
            "job_id": "test-job",
            "episode_timing_policy": episode_policy(sequence_zero=True),
            "exact": [
                visual_exact(
                    "00:00:01,000",
                    "歡迎來到",
                    test_nominal_frame("00:00:01,000"),
                    "sequence_zero",
                )
            ],
            "source_timing_checks": [source],
        }
        result = self.validate(text, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("只能套用 cue 1" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_custom_visual_anchor_requires_one_exact_lock(self) -> None:
        srt, decisions = xml_anchor_fixture(
            cut="00:00:49,216",
            waveform_onset="00:00:49,400",
            xml_frame=1475,
        )
        decisions["exact"] = []
        result = self.validate(srt, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("對應 exact lock" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_custom_xml_anchor_preserves_an_intentional_previous_gap(self) -> None:
        srt, decisions = xml_anchor_fixture(
            cut="00:00:49,216",
            waveform_onset="00:00:49,400",
            xml_frame=1475,
            before_end="00:00:49,000",
        )
        result = self.validate(srt, decisions)
        self.assertEqual(result["errors"], 0)

    def test_custom_xml_requires_passed_post_snap_audio_check(self) -> None:
        srt, decisions = xml_anchor_fixture(
            cut="00:00:49,216",
            waveform_onset="00:00:49,400",
            xml_frame=1475,
        )
        decisions["xml_edit_checks"][0]["post_snap_audio_check"] = "reviewed"
        result = self.validate(srt, decisions)
        messages = [issue["message"] for issue in result["issues"]]
        self.assertTrue(any("尚未完成原音回查" in item for item in messages))
        self.assertGreater(result["errors"], 0)

    def test_not_used_xml_record_remains_backward_compatible_without_fps(self) -> None:
        text = (
            "1\n00:00:00,000 --> 00:00:01,000\n前一句\n\n"
            "2\n00:00:01,000 --> 00:00:03,000\n後一句\n"
        )
        result = self.validate(
            text,
            {
                "xml_edit_checks": [
                    {
                        "cut": "00:00:01,100",
                        "original_boundary": "00:00:01,000",
                        "final_boundary": "00:00:01,000",
                        "mode": "not_used",
                        "status": "not_used",
                        "evidence": "沿用既有未採用證據",
                        "reason": "保留聲音與語意邊界",
                    }
                ]
            },
        )
        self.assertEqual(result["errors"], 0)

    def test_hidden_audio_overlap_requires_explicit_policy_source_and_rebind(self) -> None:
        cut = "00:00:01,435"
        onset = "00:00:01,600"
        srt, decisions = xml_anchor_fixture(
            cut=cut,
            waveform_onset=onset,
            xml_frame=43,
            allow_hidden_visual_overlap=True,
            hidden_event_ids=["HIDDEN-OVERLAP"],
        )
        decisions["hidden_events"] = [
            {
                "id": "HIDDEN-OVERLAP",
                "start": "00:00:01,450",
                "end": "00:00:01,500",
                "audio_start": "00:00:01,430",
                "audio_end": "00:00:01,650",
                "next_visible_start": onset,
                "next_text": "後一句",
            }
        ]
        decisions["hidden_event_visual_rebinds"] = [
            hidden_rebind(
                hidden_event_id="HIDDEN-OVERLAP",
                visual_anchor_start=cut,
                next_visible_start=onset,
                next_text="後一句",
                overlaps_hidden_audio=True,
            )
        ]
        passing = self.validate(srt, decisions)
        self.assertEqual(passing["errors"], 0)

        missing_source_allow = copy.deepcopy(decisions)
        missing_source_allow["source_timing_checks"][0][
            "allow_hidden_visual_overlap"
        ] = False
        missing_source_result = self.validate(srt, missing_source_allow)
        self.assertGreater(missing_source_result["errors"], 0)

        missing_policy_allow = copy.deepcopy(decisions)
        missing_policy_allow["episode_timing_policy"][
            "allow_hidden_visual_overlap"
        ] = False
        missing_policy_result = self.validate(srt, missing_policy_allow)
        self.assertGreater(missing_policy_result["errors"], 0)

        missing_rebind = copy.deepcopy(decisions)
        missing_rebind["hidden_event_visual_rebinds"] = []
        missing_rebind_result = self.validate(srt, missing_rebind)
        self.assertGreater(missing_rebind_result["errors"], 0)

    def test_hidden_pointer_rebind_without_audio_overlap_keeps_allow_false(self) -> None:
        cut = "00:00:01,435"
        onset = "00:00:01,600"
        srt, decisions = xml_anchor_fixture(
            cut=cut,
            waveform_onset=onset,
            xml_frame=43,
            allow_hidden_visual_overlap=False,
        )
        decisions["episode_timing_policy"]["allow_hidden_visual_overlap"] = True
        decisions["hidden_events"] = [
            {
                "id": "HIDDEN-NO-OVERLAP",
                "start": "00:00:01,100",
                "end": "00:00:01,300",
                "audio_start": "00:00:01,100",
                "audio_end": "00:00:01,300",
                "next_visible_start": onset,
                "next_text": "後一句",
            }
        ]
        decisions["hidden_event_visual_rebinds"] = [
            hidden_rebind(
                hidden_event_id="HIDDEN-NO-OVERLAP",
                visual_anchor_start=cut,
                next_visible_start=onset,
                next_text="後一句",
                overlaps_hidden_audio=False,
            )
        ]
        result = self.validate(srt, decisions)
        self.assertEqual(result["errors"], 0)

    def test_conflicting_policy_aliases_are_schema_errors(self) -> None:
        srt, decisions = sequence_zero_fixture()
        decisions["episode_timing_policy"]["fps_num"] = 30
        result = self.run_validate(srt, decisions)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must match", result.stderr)


if __name__ == "__main__":
    unittest.main()
