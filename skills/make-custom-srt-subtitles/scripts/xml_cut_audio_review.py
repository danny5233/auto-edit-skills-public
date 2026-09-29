#!/usr/bin/env python3
"""Build sequence-time, per-channel waveform and listening evidence for XML cuts.

PCM WAV needs only Python. Other audio formats require an available FFmpeg binary.
The output is evidence for review, never an automatic speech or speaker verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

from json_cut_align_xml import format_time, parse_visible_cuts, xml_rate


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def pcm_wav(path: Path, ffmpeg: str | None, temporary: Path) -> Path:
    try:
        with wave.open(str(path), 'rb') as source:
            if source.getcomptype() == 'NONE' and source.getsampwidth() == 2:
                return path
    except (wave.Error, EOFError):
        pass
    executable = ffmpeg or shutil.which('ffmpeg')
    if not executable:
        raise ValueError('Audio must be 16-bit PCM WAV, or FFmpeg must be available')
    decoded = temporary / 'decoded.wav'
    subprocess.run([executable, '-nostdin', '-v', 'error', '-y', '-i', str(path),
                    '-map', '0:a:0', '-vn', '-c:a', 'pcm_s16le', str(decoded)],
                   check=True, capture_output=True, text=True)
    return decoded


def svg_waveform(channels: list[list[float]], cut_fraction: float, width: int = 1000) -> str:
    height_per = 86
    height = height_per * len(channels)
    center_x = round(width * cut_fraction, 2)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="分軌聲波">',
             '<rect width="100%" height="100%" fill="#111827"/>']
    for index, bins in enumerate(channels):
        middle = index * height_per + height_per / 2
        parts.append(f'<text x="8" y="{index * height_per + 17}" fill="#d1d5db" font-size="13">聲道 {index + 1}</text>')
        parts.append(f'<line x1="0" y1="{middle}" x2="{width}" y2="{middle}" stroke="#475569"/>')
        for bin_index, value in enumerate(bins):
            x = (bin_index + .5) * width / max(1, len(bins))
            extent = max(.5, min(1., value) * (height_per / 2 - 9))
            parts.append(f'<line x1="{x:.2f}" y1="{middle - extent:.2f}" x2="{x:.2f}" y2="{middle + extent:.2f}" stroke="#38bdf8" stroke-width="{max(1, width / max(1, len(bins)) * .7):.2f}"/>')
    parts.append(f'<line x1="{center_x}" y1="0" x2="{center_x}" y2="{height}" stroke="#f97316" stroke-width="2"/>')
    parts.append('</svg>')
    return ''.join(parts)


def build(audio: Path, xml: Path, output_dir: Path, sequence_name: str | None = None,
          window: float = 1.0, bin_ms: int = 10, from_seconds: float = 0,
          to_seconds: float | None = None, ffmpeg: str | None = None,
          srt: Path | None = None, alignment: Path | None = None) -> dict:
    if output_dir.exists():
        raise FileExistsError(f'Refusing to overwrite: {output_dir}')
    if not math.isfinite(window) or window <= 0 or not 1 <= bin_ms <= 100:
        raise ValueError('Window must be positive and bin_ms must be 1–100')
    if not math.isfinite(from_seconds) or from_seconds < 0 or (
            to_seconds is not None and (not math.isfinite(to_seconds) or to_seconds <= from_seconds)):
        raise ValueError('Invalid cut time range')
    fps, cuts, name = parse_visible_cuts(xml, sequence_name)
    rate = xml_rate(xml, name)
    frames = [(round(cut * fps), cut) for cut in cuts if cut >= from_seconds and (
        to_seconds is None or cut <= to_seconds)]
    with tempfile.TemporaryDirectory() as temporary_name:
        source = pcm_wav(audio, ffmpeg, Path(temporary_name))
        with wave.open(str(source), 'rb') as reader:
            channels, sample_rate, total_frames = reader.getnchannels(), reader.getframerate(), reader.getnframes()
            if reader.getsampwidth() != 2 or channels < 1 or sample_rate < 1:
                raise ValueError('Expected 16-bit PCM audio with at least one channel')
            duration = total_frames / sample_rate
            if frames and frames[-1][1] > duration:
                raise ValueError('Sequence cuts exceed audio duration; verify sequence-time sync')
            output_dir.mkdir(parents=True)
            records = []
            cards = []
            for frame, cut in frames:
                start = max(0, cut - window)
                end = min(duration, cut + window)
                first = max(0, round(start * sample_rate))
                last = min(total_frames, round(end * sample_rate))
                reader.setpos(first)
                samples = array('h')
                samples.frombytes(reader.readframes(last - first))
                if not samples:
                    raise ValueError(f'Empty audio at cut frame {frame}')
                if sys.byteorder != 'little':
                    samples.byteswap()
                count = len(samples) // channels
                bin_size = max(1, round(sample_rate * bin_ms / 1000))
                levels = [[] for _ in range(channels)]
                for offset in range(0, count, bin_size):
                    stop = min(count, offset + bin_size)
                    for channel in range(channels):
                        values = samples[offset * channels + channel:stop * channels:channels]
                        rms = math.sqrt(sum(value * value for value in values) / len(values)) / 32768
                        levels[channel].append(round(rms, 5))
                stem = f'cut_{frame:07d}'
                svg_name = stem + '.svg'
                (output_dir / svg_name).write_text(svg_waveform(levels, (cut - first / sample_rate) / (count / sample_rate)), encoding='utf-8')
                clip_names = []
                for channel in range(channels):
                    clip_name = f'{stem}_ch{channel + 1}.wav'
                    mono = array('h', samples[channel::channels])
                    if sys.byteorder != 'little':
                        mono.byteswap()
                    with wave.open(str(output_dir / clip_name), 'wb') as target:
                        target.setnchannels(1)
                        target.setsampwidth(2)
                        target.setframerate(sample_rate)
                        target.writeframes(mono.tobytes())
                    clip_names.append(clip_name)
                records.append({'frame': frame, 'cut_seconds': round(cut, 6),
                                'cut_time': format_time(cut), 'clip_start_seconds': round(first / sample_rate, 6),
                                'clip_end_seconds': round(last / sample_rate, 6), 'waveform': svg_name,
                                'channel_clips': clip_names,
                                'channel_clip_sha256': [sha(output_dir / path) for path in clip_names],
                                'waveform_sha256': sha(output_dir / svg_name),
                                'channel_peak_rms': [max(x) for x in levels],
                                'review_status': 'pending'})
                links = ''.join(f'<label>聲道 {i + 1}<audio controls preload="none" src="{html.escape(filename, quote=True)}"></audio></label>'
                                for i, filename in enumerate(clip_names))
                cards.append(f'<article><h2>影格 {frame} · {html.escape(format_time(cut))}</h2>'
                             f'<img src="{svg_name}" alt="切點 {frame} 的分軌聲波">{links}</article>')
    result = {'schema_version': 1, 'status': 'listening_pending', 'audio_sha256': sha(audio),
              'xml_sha256': sha(xml), 'input_srt_sha256': sha(srt) if srt else None,
              'alignment_sha256': sha(alignment) if alignment else None,
              'sequence_name': name, 'fps_numerator': rate.numerator,
              'fps_denominator': rate.denominator, 'sample_rate': sample_rate,
              'channels': channels, 'window_seconds': window, 'bin_ms': bin_ms,
              'sequence_time_audio_required': True, 'cuts': records}
    (output_dir / 'audio_review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    page = ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>XML 切點聲波審查</title>'
            '<style>body{font:16px sans-serif;background:#0f172a;color:#f8fafc;max-width:1100px;margin:auto;padding:20px}'
            'article{background:#1e293b;padding:18px;margin:22px 0;border-radius:10px}img{width:100%}'
            'audio{display:block;width:100%;margin:8px 0 18px}</style><h1>XML 切點聲波審查</h1>'
            '<p>橘線為 XML 切點。請逐軌回聽並另記講者、字詞、停頓及被省略聲音；聲量不等於講者身分。</p>'
            + ''.join(cards) + '</html>')
    (output_dir / 'index.html').write_text(page, encoding='utf-8')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('audio', 'xml', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sequence-name')
    parser.add_argument('--window', type=float, default=1.0)
    parser.add_argument('--bin-ms', type=int, default=10)
    parser.add_argument('--from-seconds', type=float, default=0)
    parser.add_argument('--to-seconds', type=float)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--srt', type=Path)
    parser.add_argument('--alignment', type=Path)
    result = build(**{key.replace('-', '_'): value for key, value in vars(parser.parse_args()).items()})
    print(json.dumps({'status': result['status'], 'cuts': len(result['cuts']), 'channels': result['channels']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
