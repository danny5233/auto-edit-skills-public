import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from json_cut_map_scribe import source_characters
from transcribe_elevenlabs import profile_keyterms
from srt_style import validate


class SourceCharacterTests(unittest.TestCase):
    def test_native_nested_timestamps_and_speaker_are_preserved(self):
        source, _ = source_characters({'words':[{'text':'AB','start':1,'end':2,'speaker_id':'one','characters':[
            {'text':'A','start':1,'end':1.15},{'text':'B','start':1.2,'end':2}]}]},None)
        self.assertEqual([x['end'] for x in source],[1.15,2])
        self.assertEqual(source[1]['speaker_id'],'one')
        self.assertEqual(source[1]['source_word_character_index'],1)
        self.assertEqual(source[1]['source_event_index'],0)

    def test_word_without_native_characters_never_interpolated(self):
        with self.assertRaisesRegex(ValueError,'refusing to split'):
            source_characters({'words':[dict(text='AB',start=1,end=2)]},None)

    def test_nested_missing_timing_not_inherited_from_parent(self):
        with self.assertRaisesRegex(ValueError,'lacks timestamps'):
            source_characters({'words':[dict(text='A',start=1,end=2,characters=[dict(text='A')])]},None)

    def test_nested_text_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'reproduce'):
            source_characters({'words':[dict(text='A',characters=[dict(text='B',start=1,end=2)])]},None)

    def test_bad_nested_order_rejected(self):
        with self.assertRaisesRegex(ValueError,'monotonic'):
            source_characters({'words':[dict(text='AB',characters=[dict(text='A',start=2,end=3),dict(text='B',start=1,end=2)])]},None)


class LearningGuardTests(unittest.TestCase):
    def test_wrong_spellings_not_used_as_recognition_hints(self):
        terms=profile_keyterms({'confirmed_terms':['Brand'],'common_misrecognitions':{'Brend':'Brand'}})
        self.assertIn('Brand',terms); self.assertNotIn('Brend',terms)

    def test_episode_roster_can_exclude_past_guests(self):
        self.assertEqual(profile_keyterms({'confirmed_terms':['PastGuest'], 'transcription_keyterms':[]}),[])
        self.assertEqual(profile_keyterms({'transcription_keyterms':['Host']}),['Host'])

    def test_exact_end_cannot_drift_while_text_and_start_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); srt=root/'test.srt'; decisions=root/'decisions.json'
            srt.write_text('1\n00:00:00,000 --> 00:00:02,000\n測試\n')
            decision={'exact':[{'start':'00:00:00,000','end':'00:00:01,900','text':'測試'}]}
            decisions.write_text(json.dumps(decision))
            stream=io.StringIO()
            with contextlib.redirect_stdout(stream):
                result=validate(srt,True,decisions,None)
            self.assertNotEqual(result,0)
            self.assertIn('結束時間被改動',stream.getvalue())
            decision['exact'][0]['end']='00:00:02,000'; decisions.write_text(json.dumps(decision))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validate(srt,True,decisions,None),0)

if __name__=='__main__':
    unittest.main()
