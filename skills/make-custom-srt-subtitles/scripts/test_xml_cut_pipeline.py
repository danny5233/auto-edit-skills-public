import copy
import json
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from json_cut_align_xml import Cue, parse_visible_cuts, xml_rate
from reviewed_cut_release import cut_ledger, release, sha, validate_release
from xml_cut_pipeline import prepare
import test_reviewed_cut_release as fixtures


class PipelineTests(unittest.TestCase):
    setUp = fixtures.ReviewedReleaseTests.setUp
    run_release = fixtures.ReviewedReleaseTests.run_release

    def test_ntsc_uses_rational_rate_and_millisecond_exact_hits(self):
        self.xml.write_text(self.xml.read_text().replace('</timebase>', '</timebase><ntsc>TRUE</ntsc>'))
        self.assertEqual(str(xml_rate(self.xml)), '30000/1001')
        fps, cuts, _ = parse_visible_cuts(self.xml, None)
        self.assertAlmostEqual(cuts[0], 1.001)
        # 1.010 seconds maps to the same frame but is not cut-exact.
        ledger = cut_ledger([Cue(0,1.010,0,1), Cue(1.010,2,1,2)], cuts, fps)
        self.assertFalse(ledger[0]['continuous_change'])
        ledger = cut_ledger([Cue(0,1.001,0,1), Cue(1.001,2,1,2)], cuts, fps)
        self.assertTrue(ledger[0]['continuous_change'])

    def test_prepare_executes_alignment_and_never_claims_review(self):
        result = prepare(self.srt, self.alignment, self.xml, self.root/'prepared')
        self.assertTrue((self.root/'prepared/candidate.srt').exists())
        review = json.loads((self.root/'prepared/review.json').read_text())
        self.assertEqual(len(review['cuts']), 2)
        self.assertTrue(all(x['action']=='pending' for x in review['cuts']))
        self.assertEqual(review['cut_policy'], 'split_at_visible_edit')
        self.assertTrue(review['cuts'][0]['subtitle_spans_cut'])
        self.assertEqual(review['cuts'][0]['expected_action'], 'adopt')
        self.assertEqual(result['status'], 'review-required')

    def test_visible_cut_crossing_speech_needs_split_or_documented_conflict(self):
        self.review['cut_policy'] = 'split_at_visible_edit'
        self.review['cuts'][0].update(action='keep', reason='reviewed', visible_cut_confirmed=True)
        with self.assertRaisesRegex(ValueError, 'reviewed split'):
            self.run_release()
        self.review['cuts'][0].update(audio_conflict_confirmed=True,
                                      exception_reason='Original speech overlaps the cut within one indivisible word')
        self.run_release()

    def test_prepare_with_sequence_audio_writes_per_channel_review_bundle(self):
        audio = self.root/'sequence.wav'
        samples = array('h')
        for index in range(3 * 8000):
            samples.extend((9000 if 7500 <= index < 8500 else 0,
                            12000 if 15500 <= index < 16500 else 0,
                            3000 if 7500 <= index < 8500 else 0))
        with wave.open(str(audio), 'wb') as writer:
            writer.setnchannels(3); writer.setsampwidth(2); writer.setframerate(8000)
            writer.writeframes(samples.tobytes())
        result = prepare(self.srt, self.alignment, self.xml, self.root/'prepared', audio=audio)
        folder = self.root/'prepared/audio_review'
        evidence = json.loads((folder/'audio_review.json').read_text(encoding='utf-8'))
        review = json.loads((self.root/'prepared/review.json').read_text(encoding='utf-8'))
        self.assertEqual(result['audio_review_status'], 'listening_pending')
        self.assertEqual(len(evidence['cuts']), 2)
        self.assertEqual(evidence['channels'], 3)
        self.assertEqual([item['frame'] for item in evidence['cuts']], [30, 60])
        self.assertTrue(all(item['review_status'] == 'pending' for item in evidence['cuts']))
        self.assertEqual(len(evidence['cuts'][0]['channel_clips']), 3)
        self.assertGreater(evidence['cuts'][0]['channel_peak_rms'][0], evidence['cuts'][0]['channel_peak_rms'][2])
        self.assertIn('audio_review_sha256', review)
        self.assertIn('聲道 3', (folder/'index.html').read_text(encoding='utf-8'))
        self.assertIn('stroke="#f97316"', (folder/'cut_0000030.svg').read_text(encoding='utf-8'))

    def test_audio_review_requires_sequence_length(self):
        audio = self.root/'short.wav'
        with wave.open(str(audio), 'wb') as writer:
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(8000)
            writer.writeframes(b'\0\0' * 8000)
        from xml_cut_audio_review import build
        with self.assertRaisesRegex(ValueError, 'exceed audio duration'):
            build(audio, self.xml, self.root/'too-short')

    def test_serialized_verifier_rejects_changed_final_and_false_ledger(self):
        self.run_release()
        output, report = self.root/'out.srt', self.root/'report.json'
        validate_release(output, self.xml, report)
        output.write_bytes(output.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            validate_release(output, self.xml, report)
        data=json.loads(report.read_text()); data['output_srt_sha256']=sha(output)
        data['cut_ledger'][0]['continuous_change']=False
        report.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'ledger'):
            validate_release(output, self.xml, report)

    def test_end_only_does_not_pull_next_caption_across_silence(self):
        self.srt.write_text('1\n00:00:00,000 --> 00:00:01,200\n甲乙丙\n\n2\n00:00:01,700 --> 00:00:02,000\n丁戊己\n')
        self.review['input_srt_sha256']=sha(self.srt)
        item=self.review['cuts'][0]
        item.pop('replace_boundary_index'); item.update(boundary_index=3,endpoint='end')
        result=self.run_release()
        self.assertIn('00:00:01,700', (self.root/'out.srt').read_text())
        self.assertTrue(result['cut_ledger'][0]['subtitle_end'])
        self.assertFalse(result['cut_ledger'][0]['continuous_change'])

    def test_exact_last_endpoint_can_be_reviewed(self):
        self.review['cuts'][0]['action']='keep'
        item=copy.deepcopy(self.review['cuts'][0])
        item.pop('replace_boundary_index')
        item.update(frame=60,action='adopt',endpoint='end',boundary_index=6,speech_boundary=1.99)
        self.review['cuts'][1]=item
        result=self.run_release()
        self.assertTrue(result['cut_ledger'][-1]['subtitle_end'])

    def test_rate_tampering_is_rejected(self):
        self.run_release()
        report=self.root/'report.json'; data=json.loads(report.read_text())
        data['fps_numerator']=30000; data['fps_denominator']=1001
        report.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'rate mismatch'):
            validate_release(self.root/'out.srt',self.xml,report)

    def test_gate_rejects_empty_ledger_even_if_output_hash_matches(self):
        self.run_release()
        report=self.root/'report.json'; data=json.loads(report.read_text())
        data['reviewed_cuts']=[]; report.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'coverage'):
            validate_release(self.root/'out.srt',self.xml,report)


if __name__ == '__main__':
    unittest.main()
