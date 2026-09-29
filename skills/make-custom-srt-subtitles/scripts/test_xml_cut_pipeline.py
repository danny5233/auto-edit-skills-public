import copy
import json
import tempfile
import unittest
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
        self.assertEqual(result['status'], 'review-required')

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
