#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from json_cut_align_xml import (
    AlignedChar,
    boundary_at_cut,
    inside_character_review_candidate,
    known_boundary_review_candidate,
    parse_visible_cuts,
)


class InsideCharacterReviewCandidateTests(unittest.TestCase):
    def test_reports_both_sides_instead_of_guessing(self) -> None:
        characters = [
            AlignedChar("冷", 0.0, 0.1, "exact", 0),
            AlignedChar("氣", 0.1, 0.3, "exact", 1),
            AlignedChar("必", 0.3, 0.4, "exact", 2),
            AlignedChar("須", 0.4, 0.5, "exact", 3),
        ]

        candidate = inside_character_review_candidate(characters, 0.2)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertTrue(candidate["requires_semantic_review"])
        self.assertEqual(candidate["overlapping_character"], "氣")
        self.assertEqual(candidate["split_before_character"]["boundary_index"], 1)
        self.assertEqual(candidate["split_after_character"]["boundary_index"], 2)
        self.assertEqual(candidate["split_after_character"]["before_text"], "冷氣")
        self.assertEqual(candidate["split_after_character"]["after_text"], "必須")

    def test_returns_none_without_overlap(self) -> None:
        characters = [
            AlignedChar("冷", 0.0, 0.1, "exact", 0),
            AlignedChar("氣", 0.2, 0.3, "exact", 1),
        ]

        self.assertIsNone(inside_character_review_candidate(characters, 0.15))

    def test_known_boundary_preserves_short_reaction_for_review(self) -> None:
        characters = [
            AlignedChar("對", 0.0, 0.1, "exact", 0),
            AlignedChar("如", 0.2, 0.3, "exact", 1),
            AlignedChar("果", 0.3, 0.4, "exact", 2),
        ]

        candidate = known_boundary_review_candidate(characters, 1)

        self.assertTrue(candidate["requires_semantic_review"])
        self.assertEqual(candidate["before_text"], "對")
        self.assertEqual(candidate["after_text"], "如果")


class VisibleCutParsingTests(unittest.TestCase):
    def test_persistent_upper_overlay_does_not_hide_lower_track_edits(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<xmeml><sequence><name>test</name><rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
<media><video>
  <track>
    <clipitem id="a"><start>0</start><end>90</end><enabled>TRUE</enabled></clipitem>
    <clipitem id="b"><start>90</start><end>180</end><enabled>TRUE</enabled></clipitem>
  </track>
  <track>
    <clipitem id="logo"><start>0</start><end>180</end><enabled>TRUE</enabled></clipitem>
  </track>
</video></media></sequence></xmeml>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.xml"
            path.write_text(xml, encoding="utf-8")
            fps, cuts, name = parse_visible_cuts(path, None)

        self.assertAlmostEqual(fps, 30000 / 1001)
        self.assertEqual(name, "test")
        self.assertEqual(cuts, [90 / fps, 180 / fps])

    def test_disabled_track_and_transition_are_not_candidates(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<xmeml><sequence><name>test</name><rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
<media><video>
  <track>
    <clipitem id="a"><start>0</start><end>60</end></clipitem>
    <transitionitem><start>55</start><end>65</end></transitionitem>
    <clipitem id="b"><start>60</start><end>120</end></clipitem>
  </track>
  <track><enabled>FALSE</enabled>
    <clipitem id="hidden"><start>0</start><end>30</end></clipitem>
  </track>
</video></media></sequence></xmeml>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.xml"
            path.write_text(xml, encoding="utf-8")
            _, cuts, _ = parse_visible_cuts(path, None)

        self.assertEqual(cuts, [4.0])


class DeletedSourceGapTests(unittest.TestCase):
    def test_omitted_punctuation_does_not_reject_a_valid_boundary(self) -> None:
        characters = [
            AlignedChar("好", 0.0, 0.1, "exact", 0),
            AlignedChar("我", 0.3, 0.4, "exact", 2),
        ]
        source = [
            {"text": "好"},
            {"text": "，"},
            {"text": "我"},
        ]

        boundary, speech_boundary, reason, _ = boundary_at_cut(
            characters, 0.2, source
        )

        self.assertEqual(boundary, 1)
        self.assertIn(speech_boundary, (0.1, 0.3))
        self.assertIsNone(reason)

    def test_omitted_spoken_character_still_rejects_the_boundary(self) -> None:
        characters = [
            AlignedChar("好", 0.0, 0.1, "exact", 0),
            AlignedChar("我", 0.3, 0.4, "exact", 2),
        ]
        source = [
            {"text": "好"},
            {"text": "啊"},
            {"text": "我"},
        ]

        boundary, _, reason, _ = boundary_at_cut(characters, 0.2, source)

        self.assertIsNone(boundary)
        self.assertEqual(reason, "deleted_source_characters_at_cut")


if __name__ == "__main__":
    unittest.main()
