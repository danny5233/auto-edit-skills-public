#!/usr/bin/env python3
"""Run XML cut preparation or apply reviewed cuts and verify serialized output."""
import argparse
import json
from pathlib import Path

import json_cut_align_xml as align
from reviewed_cut_release import cut_ledger, release, sha, validate_release


def prepare(srt, alignment, xml, output_dir, sequence_name=None):
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
    review = {'mode': 'reviewed_boundaries', 'reviewer': '', 'evidence': '',
              'input_srt_sha256': sha(srt), 'xml_sha256': sha(xml),
              'alignment_sha256': sha(alignment), 'sequence_name': name,
              'fps_numerator': rate.numerator, 'fps_denominator': rate.denominator,
              'protected_terms': [], 'cuts': []}
    for cut, finding in zip(cuts, findings):
        review['cuts'].append({'frame': round(cut*fps), 'action': 'pending',
                              'reason': '', 'candidate_finding': finding})
    # Candidate adoption is never silently promoted to reviewed adoption.
    (output_dir/'review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    ledger = cut_ledger(cues, cuts, fps)
    result = {'status': 'review-required', 'sequence_name': name,
              'fps_numerator': rate.numerator, 'fps_denominator': rate.denominator,
              'input_srt_sha256': sha(srt), 'xml_sha256': sha(xml),
              'alignment_sha256': sha(alignment), 'input_cut_ledger': ledger,
              'candidate_summary': summary, 'additional_api_calls': 0}
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
