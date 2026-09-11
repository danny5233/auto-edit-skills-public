import copy
import json
import tempfile
import unittest
from pathlib import Path
from reviewed_cut_release import release, sha


class ReviewedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.srt = self.root/'source.srt'
        self.srt.write_text('1\n00:00:00,000 --> 00:00:01,200\n甲乙丙\n\n2\n00:00:01,200 --> 00:00:02,000\n丁戊己\n')
        self.xml = self.root/'timeline.xml'
        self.xml.write_text('<xmeml><sequence><name>test</name><rate><timebase>30</timebase></rate><media><video><track><clipitem id="a"><start>0</start><end>30</end></clipitem><clipitem id="b"><start>30</start><end>60</end></clipitem></track></video></media></sequence></xmeml>')
        self.alignment = self.root/'alignment.json'
        self.alignment.write_text(json.dumps({'characters': [dict(text=t,start=i*.3,end=i*.3+.3) for i,t in enumerate('甲乙丙丁戊己')]}))
        self.review = {'mode':'reviewed_boundaries','reviewer':'editor','evidence':'context reviewed',
                       'input_srt_sha256':sha(self.srt),'xml_sha256':sha(self.xml),
                       'alignment_sha256':sha(self.alignment), 'cuts':[
                           dict(frame=30, action='adopt', reason='complete left phrase',boundary_index=2,replace_boundary_index=3,
                                speech_boundary=.95,visible_cut_confirmed=True,semantic_boundary_confirmed=True,
                                speaker_boundary_preserved=True,hidden_event_clear=True,post_snap_audio_reviewed=True,
                                audio_evidence='editor reviewed original 0.5–1.5 s'),
                           dict(frame=60, action='keep',reason='sequence end') ]}

    def run_release(self):
        review = self.root/'review.json'; review.write_text(json.dumps(self.review))
        return release(self.srt,self.xml,review,self.root/'out.srt',self.root/'report.json',alignment=self.alignment)

    def test_reassigns_text_instead_of_only_moving_timestamp(self):
        result = self.run_release()
        output = (self.root/'out.srt').read_text()
        self.assertIn('00:00:01,000\n甲乙', output)
        self.assertIn('丙丁戊己', output)
        self.assertEqual(result['continuous_changes_after'],1)
        self.assertEqual(result['applied_reviewed_operations'],1)
        self.assertEqual(result['output_srt_sha256'],sha(self.root/'out.srt'))

    def test_missing_review_does_not_write_a_final_file(self):
        self.review['cuts'].pop()
        with self.assertRaisesRegex(ValueError,'exactly one'):
            self.run_release()
        self.assertFalse((self.root/'out.srt').exists())

    def test_stale_input_rejected(self):
        self.review['input_srt_sha256']='stale'
        with self.assertRaisesRegex(ValueError,'Stale'):
            self.run_release()

    def test_pending_audio_cannot_be_promoted_by_global_flag(self):
        self.review['cuts'][0]['post_snap_audio_reviewed']=False
        with self.assertRaisesRegex(ValueError,'per-cut'):
            self.run_release()

    def test_protected_term_not_split(self):
        self.review['protected_terms']=['乙丙']
        with self.assertRaisesRegex(ValueError,'protected'):
            self.run_release()

    def test_five_frame_limit_still_applies(self):
        self.review['cuts'][0]['speech_boundary']=.5
        with self.assertRaisesRegex(ValueError,'five frames'):
            self.run_release()

    def test_human_replay_preserves_bytes_and_locks_both_endpoints(self):
        human=self.root/'human.srt'
        data=b'\xef\xbb\xbf1\r\n00:00:00,100 --> 00:00:01,950\r\nHuman edit \r\n'
        human.write_bytes(data)
        self.review.update(mode='human_reference',human_srt=str(human),human_srt_sha256=sha(human))
        result=self.run_release()
        self.assertEqual((self.root/'out.srt').read_bytes(),data)
        self.assertEqual(result['grade'],'human_reference_replay')
        self.assertFalse(result['independent_generation_evaluation'])
        self.assertEqual(result['exact'][0]['end'],'00:00:01,950')

if __name__=='__main__':
    unittest.main()
