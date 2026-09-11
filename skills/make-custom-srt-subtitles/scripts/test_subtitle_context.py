#!/usr/bin/env python3
"""Synthetic handoff tests: no client selection, real media, or paid API calls."""
import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_context as sc
import transcribe_elevenlabs as transcribe
import srt_style


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile_path = self.root / "client/series.json"
        self.profile = {"series_id": "series-a", "client_id": "client-a",
                        "transcription_keyterms": [], "confirmed_terms": ["ExampleBrand"],
                        "subtitle_preferences": {"tail_fill_threshold_ms": 3000},
                        "subtitle_preferences_by_content_type": {
                            "short_form": {"tail_fill_threshold_ms": 1800}}}
        write(self.profile_path, self.profile)
        self.source = self.root / "media/episode.wav"
        self.source.parent.mkdir()
        self.source.write_bytes(b"synthetic-media-for-hash-checks")
        self.decisions = self.root / "jobs/ep1/decisions.json"
        write(self.decisions, {"job_id": "ep1", "exact": [{"start": "00:00:00,000", "text": "原話"}]})
        self.case_path = self.root / "editing/case.json"
        classification = ["影片剪輯", "Example Client", "Example Type"]
        self.case = {"schema_version": 1, "case_id": "case-1", "client_id": "client-a",
                     "type_id": "already-selected-type", "episode_id": "episode-1",
                     "version": "edit-1", "title": "Synthetic episode", "status": "in_progress",
                     "scope": {"stages": ["text"]},
                     "classification": {"status": "confirmed", "path": classification,
                         "confirmation": {"case_id": "case-1", "client_id": "client-a",
                             "type_id": "already-selected-type", "path": classification,
                             "quote": "Synthetic confirmation for tests", "source": "test", "at": "test-time"}},
                     "sources": [{"id": "audio", "role": "audio", "case_id": "case-1",
                                  "root": "workspace", "path": "media/episode.wav",
                                  "version": "edit-1", "sha256": sc.digest(self.source)}],
                     "artifacts": [],
                     "subtitle": {"job_id": "ep1", "series_id": "series-a",
                         "subtitle_content_type": "long_form",
                         "profile": {"root": "workspace", "path": "client/series.json",
                                     "sha256": sc.digest(self.profile_path)},
                         "delivery": {"directory": {"root": "workspace", "path": "media/Ai字幕"}}}}
        self.context_path = self.root / "jobs/ep1/context.json"
        self.local = {"schema_version": 1, "case_id": "case-1", "job_id": "ep1",
                      "upstream_case": {"root": "workspace", "path": "editing/case.json"},
                      "source_ids": ["audio"],
                      "decisions": {"root": "workspace", "path": "jobs/ep1/decisions.json",
                                    "sha256": sc.digest(self.decisions)},
                      "evidence_dir": {"root": "workspace", "path": "jobs/ep1/evidence"}}
        self.save()

    def save(self):
        write(self.case_path, self.case)
        self.local["upstream_case"]["sha256"] = sc.digest(self.case_path)
        write(self.context_path, self.local)

    def resolve(self):
        return sc.resolve_context(self.root, self.context_path)

    def args(self, *extra):
        return transcribe.parse_args([str(self.source), "--mode", "single", "--workspace",
                                      str(self.root), "--context", str(self.context_path), "--dry-run", *extra])

    def test_inherits_upstream_without_catalog_selection_or_writes(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        checker = sc.upstream_checker()
        with patch.object(checker, "load_layers", side_effect=AssertionError("Do not select clients")):
            with patch.object(sc, "upstream_checker", return_value=checker):
                context = self.resolve()
        self.assertEqual(context["job"]["client_id"], "client-a")
        self.assertEqual(context["job"]["type_id"], "already-selected-type")
        self.assertEqual(context["job"]["episode_id"], "episode-1")
        self.assertEqual(context["delivery_dir"], self.root / "media/Ai字幕")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_existing_confirmation_is_reused_but_wrong_case_is_rejected(self):
        self.resolve()
        self.local["case_id"] = "case-2"
        self.save()
        with self.assertRaisesRegex(ValueError, "different case"):
            self.resolve()

    def test_unconfirmed_handoff_stops_before_source_reads(self):
        self.case["classification"]["status"] = "proposed"
        self.save()
        checker = sc.upstream_checker()
        with patch.object(checker, "verify_sources", side_effect=AssertionError("Must not read media")):
            with patch.object(sc, "upstream_checker", return_value=checker):
                with self.assertRaisesRegex(ValueError, "Classification awaits"):
                    self.resolve()

    def test_subtitle_stage_authorization_is_required(self):
        self.case["scope"]["stages"] = ["architecture-audit"]
        self.save()
        with self.assertRaisesRegex(ValueError, "text stage"):
            self.resolve()

    def test_local_client_or_routing_cannot_override_upstream(self):
        for field, value in (("client_id", "client-b"), ("series_id", "series-b"),
                             ("subtitle_content_type", "short_form"), ("type_id", "pov"),
                             ("profile", {}), ("delivery", {})):
            with self.subTest(field=field):
                self.local[field] = value
                self.save()
                with self.assertRaises(ValueError):
                    self.resolve()
                del self.local[field]

    def test_upstream_profile_must_match_case_and_series(self):
        for key in ("client_id", "series_id"):
            original = self.profile[key]
            self.profile[key] = "wrong"
            write(self.profile_path, self.profile)
            self.case["subtitle"]["profile"]["sha256"] = sc.digest(self.profile_path)
            self.save()
            with self.assertRaisesRegex(ValueError, "identity mismatch|client_id mismatch"):
                self.resolve()
            self.profile[key] = original

    def test_short_form_does_not_contaminate_long_form(self):
        self.case["subtitle"]["subtitle_content_type"] = "short_form"
        self.save()
        self.assertEqual(srt_style.load_style_policy(None, self.resolve()["profile"])["tail_fill_threshold_ms"], 1800)
        self.case["subtitle"]["subtitle_content_type"] = "long_form"
        self.save()
        self.assertEqual(self.resolve()["profile"]["subtitle_preferences"]["tail_fill_threshold_ms"], 3000)
        self.assertEqual(json.loads(self.profile_path.read_text()), self.profile)

    def test_unspecified_format_uses_common_rules_without_reclassification(self):
        del self.case["subtitle"]["subtitle_content_type"]
        self.save()
        context = self.resolve()
        self.assertIsNone(context["content_rules"])
        self.assertEqual(context["profile"]["subtitle_preferences"]["tail_fill_threshold_ms"], 3000)
        self.assertTrue(context["warnings"])

    def test_no_profile_uses_generic_rules_without_searching_another_client(self):
        self.case["subtitle"]["profile"] = None
        del self.case["subtitle"]["series_id"]
        self.save()
        context = self.resolve()
        self.assertIsNone(context["profile_path"])
        self.assertEqual(srt_style.load_style_policy(None, context["profile"])["tail_fill_threshold_ms"], 2000)
        self.assertNotIn("ExampleBrand", str(context["profile"]))

    def test_changed_case_media_decisions_or_profile_stop_before_api(self):
        for path in (self.case_path, self.source, self.decisions, self.profile_path):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                args = self.args("--confirm-cost")
                args.dry_run = False
                with patch.object(transcribe, "load_api_key", side_effect=AssertionError("must not load key")):
                    with self.assertRaises(ValueError):
                        transcribe.run(args)
                path.write_bytes(before)

    def test_unknown_source_and_other_episode_are_rejected(self):
        self.local["source_ids"] = ["not-in-case"]
        self.save()
        with self.assertRaisesRegex(ValueError, "upstream whitelist"):
            self.resolve()
        self.local["source_ids"] = ["audio"]
        self.case["sources"][0]["case_id"] = "case-2"
        self.save()
        with self.assertRaisesRegex(ValueError, "Cross-episode"):
            self.resolve()

    def test_case_revision_is_separate_from_edit_version_and_mixed_edits_rejected(self):
        self.case["version"] = "management-revision-2"
        self.save()
        self.resolve()
        transcript = self.root / "media/scribe.json"
        write(transcript, {"words": []})
        self.case["sources"].append({"id": "scribe", "role": "transcript", "case_id": "case-1",
                                    "root": "workspace", "path": "media/scribe.json",
                                    "version": "edit-2", "sha256": sc.digest(transcript)})
        self.local["source_ids"].append("scribe")
        self.save()
        with self.assertRaisesRegex(ValueError, "edit versions"):
            self.resolve()

    def test_upstream_delivery_cannot_be_overridden_by_local_context(self):
        self.local["delivery"] = {"directory": {"root": "workspace", "path": "tool-output"}}
        self.save()
        with self.assertRaisesRegex(ValueError, "delivery must come from upstream"):
            self.resolve()

    def test_traversal_symlink_and_mutable_skill_paths_rejected(self):
        for value in ("../elsewhere", "C:/outside", "/outside", "folder\\file"):
            with self.assertRaises(ValueError):
                sc.inside(self.root, value)
        (self.root / "escape").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            sc.inside(self.root, "escape/elsewhere")
        self.case["subtitle"]["learning_registry"] = {"root": "alias", "path": "learning.json"}
        self.save()
        binding = self.root / "bindings.json"
        write(binding, {"alias": str(sc.SKILL_ROOT)})
        with self.assertRaisesRegex(ValueError, "installed skills"):
            sc.resolve_context(self.root, self.context_path, binding)

    def test_external_profile_binding_preserves_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory).resolve() / "existing.json"
            external.write_bytes(self.profile_path.read_bytes())
            self.case["subtitle"]["profile"].update(root="profiles", path="existing.json")
            self.save()
            binding = self.root / "bindings.json"
            write(binding, {"profiles": str(external.parent)})
            context = sc.resolve_context(self.root, self.context_path, binding)
            self.assertEqual(context["profile_path"], external)

    def test_legacy_sidecar_checks_identity_without_rewriting_legacy(self):
        legacy = self.root / "legacy/job.json"
        write(legacy, {"job_id": "ep1", "series_id": "series-a"})
        self.local["legacy_job"] = {"root": "workspace", "path": "legacy/job.json", "sha256": sc.digest(legacy)}
        self.save()
        before = legacy.read_bytes()
        self.resolve()
        self.assertEqual(legacy.read_bytes(), before)
        write(legacy, {"job_id": "ep2"})
        self.local["legacy_job"]["sha256"] = sc.digest(legacy)
        self.save()
        with self.assertRaisesRegex(ValueError, "Legacy job job_id"):
            self.resolve()

    def test_handoff_dry_run_uses_profile_whitelist_without_writes(self):
        result = transcribe.run(self.args())
        self.assertEqual(result["job_id"], "ep1")
        self.assertEqual(result["series_id"], "series-a")
        self.assertNotIn("keyterms", result["parameters"])
        self.assertEqual(result["subtitle_context"]["client_id"], "client-a")
        self.assertFalse((self.root / "jobs/ep1/evidence").exists())

    def test_mock_api_records_upstream_pins_without_secrets(self):
        calls = []
        def convert(**kwargs):
            calls.append(kwargs["model_id"])
            return {"text": "測試", "words": []}
        fake = types.SimpleNamespace(speech_to_text=types.SimpleNamespace(convert=convert))
        args = self.args("--confirm-cost")
        args.dry_run = False
        with patch.object(transcribe, "load_api_key", return_value=("synthetic-key", "test")):
            result = transcribe.run(args, client_factory=lambda _: fake)
        metadata = json.loads(Path(result["metadata_path"]).read_text())
        self.assertEqual(metadata["subtitle_context"]["pins"]["upstream_case_sha256"], sc.digest(self.case_path))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("synthetic-key", Path(result["metadata_path"]).read_text())

    def test_transcription_cannot_override_source_output_job_or_profile(self):
        for extra in (("--profile", str(self.profile_path)), ("--output-dir", str(self.root / "other")),
                      ("--job-id", "ep2")):
            with self.assertRaises(ValueError):
                transcribe.run(self.args(*extra))
        other = self.root / "other.wav"
        other.write_bytes(b"other")
        args = self.args()
        args.source = other
        with self.assertRaisesRegex(ValueError, "registered media"):
            transcribe.run(args)

    def test_delivery_requires_all_pinned_roles(self):
        with self.assertRaises(ValueError):
            sc.check_delivery(self.resolve())
        self.local["deliverables"] = []
        for role in sc.REQUIRED_ROLES:
            path = self.root / "media/Ai字幕" / f"{role}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(role)
            self.local["deliverables"].append({"role": role, "job_id": "ep1", "path": path.name,
                                                "sha256": sc.digest(path)})
        self.save()
        self.assertEqual(set(sc.check_delivery(self.resolve())), sc.REQUIRED_ROLES)
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Delivery changed"):
            sc.check_delivery(self.resolve())

    def test_srt_cli_obeys_pinned_decisions(self):
        path = self.root / "jobs/ep1/evidence/candidate.srt"
        path.parent.mkdir()
        path.write_text("1\n00:00:00,000 --> 00:00:02,000\n改錯了\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(srt_style.__file__)), "validate", str(path),
                                 "--workspace", str(self.root), "--context", str(self.context_path), "--json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("原話", result.stdout)


if __name__ == "__main__":
    unittest.main()
