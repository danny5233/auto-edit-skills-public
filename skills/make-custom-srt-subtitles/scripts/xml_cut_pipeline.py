#!/usr/bin/env python3
"""Run XML cut preparation or apply reviewed cuts and verify serialized output."""
import argparse
import json
from pathlib import Path

import json_cut_align_xml as align
import xml_cut_audio_review as audio_review
from reviewed_cut_release import cut_ledger, release, sha, validate_release


def prepare(srt, alignment, xml, output_dir, sequence_name=None, audio=None, ffmpeg=None):
    output_dir.mkdir(parents=True, exist_ok=False)
    candidate = output_dir / 'candidate.srt'
    report = output_dir / 'candidates.json'
    summary = align.run(align.parse_args([
        '--srt', str(srt), '--alignment', str(alignment), '--xml', str(xml),
        '--output-srt', str(candidate), '--report', str(report),
        *(['--sequence-name', sequence_name] if sequence_name else [])]))
    fps, cuts, name = align.parse_visible_cuts(xml, sequence_name)
    rate = align.xml_rate(xml, name)
    cues, _, _ = align.parse_srt(srt)
    findings = json.loads(report.read_text(encoding='utf-8'))['xml_edit_checks']
    review = {'mode': 'reviewed_boundaries', 'cut_policy': 'split_at_visible_edit',
              'reviewer': '', 'evidence': '',
              'input_srt_sha256': sha(srt), 'xml_sha256': sha(xml),
              'alignment_sha256': sha(alignment), 'sequence_name': name,
              'fps_numerator': rate.numerator, 'fps_denominator': rate.denominator,
              'protected_terms': [], 'cuts': []}
    for cut, finding in zip(cuts, findings):
        spans_cue = any(cue.start < cut < cue.end for cue in cues)
        review['cuts'].append({'frame': round(cut*fps), 'action': 'pending',
                              'reason': '', 'subtitle_spans_cut': spans_cue,
                              'expected_action': 'adopt' if spans_cue else 'inspect',
                              'candidate_finding': finding})
    if audio is not None:
        evidence = audio_review.build(audio, xml, output_dir/'audio_review',
                                      sequence_name=name, ffmpeg=ffmpeg,
                                      srt=srt, alignment=alignment)
        review['audio_review_sha256'] = sha(output_dir/'audio_review/audio_review.json')
        review['audio_sha256'] = evidence['audio_sha256']
        for item, audio_item in zip(review['cuts'], evidence['cuts']):
            if item['frame'] != audio_item['frame']:
                raise ValueError('Audio review cut ledger differs from XML cut ledger')
            item['audio_review_waveform'] = 'audio_review/' + audio_item['waveform']
            item['audio_review_clips'] = ['audio_review/' + path for path in audio_item['channel_clips']]
    # Candidate adoption is never silently promoted to reviewed adoption.
    (output_dir/'review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    ledger = cut_ledger(cues, cuts, fps)
    result = {'status': 'review-required', 'sequence_name': name,
              'fps_numerator': rate.numerator, 'fps_denominator': rate.denominator,
              'input_srt_sha256': sha(srt), 'xml_sha256': sha(xml),
              'alignment_sha256': sha(alignment), 'input_cut_ledger': ledger,
              'candidate_summary': summary, 'additional_api_calls': 0}
    if audio is not None:
        result['audio_review_sha256'] = review['audio_review_sha256']
        result['audio_review_status'] = 'listening_pending'
    (output_dir/'pipeline.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'release', 'verify'):
        p = sub.add_parser(command)
        p.add_argument('--srt', type=Path, required=True)
        p.add_argument('--xml', type=Path, required=True)
        p.add_argument('--sequence-name')
        if command == 'prepare':
            p.add_argument('--alignment', type=Path, required=True)
            p.add_argument('--output-dir', type=Path, required=True)
            p.add_argument('--audio', type=Path)
            p.add_argument('--ffmpeg')
        elif command == 'release':
            p.add_argument('--alignment', type=Path)
            p.add_argument('--review', type=Path, required=True)
            p.add_argument('--output', type=Path, required=True)
            p.add_argument('--report', type=Path, required=True)
        else:
            p.add_argument('--report', type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop('command')
    result = {'prepare': prepare, 'release': release, 'verify': validate_release}[command](**args)
    print(json.dumps({k:v for k,v in result.items() if k not in {'input_cut_ledger','cut_ledger','reviewed_cuts','exact'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
