#!/usr/bin/env python3
"""Regression tests for the portable ElevenLabs transcription CLI."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("transcribe_elevenlabs.py")
SPEC = importlib.util.spec_from_file_location("transcribe_elevenlabs", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeSpeechToText:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def convert(self, *, file, **parameters):
        self.calls.append({"filename": Path(file.name).name, "parameters": parameters})
        return {
            "language_code": "zho",
            "text": "測試",
            "words": [{"text": "測", "start": 0.0, "end": 0.2}],
        }


class FakeClient:
    def __init__(self) -> None:
        self.speech_to_text = FakeSpeechToText()


class TranscribeElevenLabsTests(unittest.TestCase):
    def test_builds_distinct_parameters_for_all_three_modes(self) -> None:
        single = MODULE.build_parameters("single", "zho", None, [])
        diarized = MODULE.build_parameters("diarized", "zho", 2, [])
        multichannel = MODULE.build_parameters("multichannel", "zho", None, [])
        self.assertEqual((single["diarize"], single["use_multi_channel"]), (False, False))
        self.assertEqual((diarized["diarize"], diarized["num_speakers"]), (True, 2))
        self.assertEqual(
            (multichannel["diarize"], multichannel["use_multi_channel"]),
            (False, True),
        )
        self.assertEqual(multichannel["multichannel_output_style"], "separate")

    def test_rejects_num_speakers_outside_diarized_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "only valid"):
            MODULE.build_parameters("single", "zho", 1, [])

    def test_dry_run_needs_no_key_and_writes_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.wav"
            source.write_bytes(b"not-real-audio-but-valid-dry-run-input")
            output = root / "output"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    "--mode",
                    "diarized",
                    "--output-dir",
                    str(output),
                    "--job-id",
                    "dry-run",
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={key: value for key, value in os.environ.items() if key != "ELEVENLABS_API_KEY"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(output.exists())
            self.assertNotIn("api_key", result.stdout.lower())

    def test_profile_adds_confirmed_and_misrecognition_keyterms(self) -> None:
        profile = {"confirmed_terms": ["Example Term"], "common_misrecognitions": {"錯別字": "正確字"}}
        keyterms = MODULE.normalize_keyterms(MODULE.profile_keyterms(profile))
        self.assertIn("Example Term", keyterms)
        self.assertIn("錯別字", keyterms)
        self.assertIn("正確字", keyterms)

    def test_process_environment_key_has_highest_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "elevenlabs.env"
            env_file.write_text("ELEVENLABS_API_KEY=file-secret\n", encoding="utf-8")
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "process-secret"}):
                key, source = MODULE.load_api_key(env_file)
        self.assertEqual(key, "process-secret")
        self.assertEqual(source, "process_environment")

    def test_explicit_env_file_is_second_key_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "elevenlabs.env"
            env_file.write_text("placeholder\n", encoding="utf-8")
            fake_dotenv = types.SimpleNamespace(
                dotenv_values=lambda _: {"ELEVENLABS_API_KEY": "explicit-secret"}
            )
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
                with patch.dict(sys.modules, {"dotenv": fake_dotenv}):
                    key, source = MODULE.load_api_key(env_file)
        self.assertEqual(key, "explicit-secret")
        self.assertEqual(source, "explicit_env_file")

    def test_default_private_env_file_is_final_key_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "elevenlabs.env"
            env_file.write_text("placeholder\n", encoding="utf-8")
            fake_dotenv = types.SimpleNamespace(
                dotenv_values=lambda _: {"ELEVENLABS_API_KEY": "default-secret"}
            )
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
                with patch.dict(sys.modules, {"dotenv": fake_dotenv}):
                    with patch.object(MODULE, "default_env_file", return_value=env_file):
                        key, source = MODULE.load_api_key(None)
        self.assertEqual(key, "default-secret")
        self.assertEqual(source, "default_private_env_file")

    def test_actual_run_saves_raw_metadata_without_secret_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.wav"
            source.write_bytes(b"fake-audio")
            output = root / "output"
            args = MODULE.parse_args(
                [
                    str(source),
                    "--mode",
                    "single",
                    "--output-dir",
                    str(output),
                    "--job-id",
                    "immutable-job",
                    "--confirm-cost",
                ]
            )
            client = FakeClient()
            seen_keys: list[str] = []

            def factory(api_key: str) -> FakeClient:
                seen_keys.append(api_key)
                return client

            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-secret-value"}):
                result = MODULE.run(args, client_factory=factory)
                with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                    MODULE.run(args, client_factory=factory)

            raw_path = Path(result["raw_path"])
            metadata_path = Path(result["metadata_path"])
            self.assertTrue(raw_path.is_file())
            self.assertTrue(metadata_path.is_file())
            combined = raw_path.read_text(encoding="utf-8") + metadata_path.read_text(
                encoding="utf-8"
            )
            self.assertNotIn("test-secret-value", combined)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["api_key_source"], "process_environment")
            self.assertEqual(seen_keys, ["test-secret-value"])
            self.assertEqual(len(client.speech_to_text.calls), 1)

    def test_actual_run_requires_explicit_cost_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.wav"
            source.write_bytes(b"fake-audio")
            args = MODULE.parse_args(
                [
                    str(source),
                    "--mode",
                    "single",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            with self.assertRaisesRegex(ValueError, "--confirm-cost"):
                MODULE.run(args, client_factory=lambda _: FakeClient())


if __name__ == "__main__":
    unittest.main()
