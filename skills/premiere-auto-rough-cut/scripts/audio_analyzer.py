#!/usr/bin/env python3
"""Emit conservative acoustic evidence from two synchronized microphone WAVs.

This module intentionally does not decide whether content is useful, assign a
speaker identity, create red/yellow edit decisions, or edit Premiere XML.  It
measures 20 ms RMS energy, reports dual-mic low-energy spans, attaches energy
features to optional SRT cues, and can estimate a coarse fixed SRT/audio offset
from caption coverage versus acoustic energy.  Every result requires review.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Collection, Sequence

from srt_analyzer import Cue, SrtAnalyzerError, parse_srt


PCM16_FULL_SCALE = 32768.0
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
DEFAULT_FRAME_MS = 20
DEFAULT_DBFS_FLOOR = -120.0
DEFAULT_SILENCE_THRESHOLD_DBFS = -52.0
DEFAULT_MIN_SILENCE_MS = 2_000
DEFAULT_SPIKE_TOLERANCE_MS = 100
DEFAULT_ACTIVITY_THRESHOLD_DBFS = -48.0
DEFAULT_SHARED_MAX_DELTA_DB = 3.0
DEFAULT_SHARED_MIN_ACTIVE_FRACTION = 0.5
DEFAULT_SHARED_MIN_COVERAGE_FRACTION = 0.95
DEFAULT_OFFSET_SEARCH_MS = 1_000
DEFAULT_ENERGY_CEILING_DBFS = -20.0

REVIEW_NOTICE = (
    "Acoustic results are evidence only. Review the matching audio and picture "
    "before assigning a speaker, deciding whether content is useful, creating "
    "red/yellow labels, or selecting edit boundaries. A shared far-field "
    "candidate can also be crosstalk or overlapping speech. The SRT offset is a "
    "coarse coverage/energy estimate, not word-level alignment."
)


class AudioAnalyzerError(ValueError):
    """Raised when an input cannot be analyzed conservatively."""


@dataclass(frozen=True)
class TrackEnergy:
    path: str
    sample_rate: int
    channels: int
    sample_width_bytes: int
    sample_encoding: str
    wav_format_tag: int
    sample_count: int
    duration_seconds: float
    frame_samples: int
    frame_mean_squares: tuple[float, ...]
    frame_dbfs: tuple[float, ...]


@dataclass(frozen=True)
class _WaveStreamInfo:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    sample_encoding: str
    wav_format_tag: int
    sample_count: int
    data_offset: int
    data_size: int


def _require_finite(value: float, name: str) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioAnalyzerError(f"{name} must be a finite number") from exc
    if not math.isfinite(converted):
        raise AudioAnalyzerError(f"{name} must be a finite number")
    return converted


def _dbfs_from_mean_square(mean_square: float, floor_dbfs: float) -> float:
    if mean_square <= 0.0:
        return floor_dbfs
    return max(floor_dbfs, 10.0 * math.log10(mean_square))


def _parse_mono_wave_stream(handle: BinaryIO, source: Path) -> _WaveStreamInfo:
    """Parse a seekable little-endian RIFF/WAVE without decoding its data."""
    header = handle.read(12)
    if len(header) != 12:
        raise AudioAnalyzerError(f"{source} is not a complete RIFF/WAVE file")
    container, riff_size, wave_id = struct.unpack("<4sI4s", header)
    if container == b"RIFX":
        raise AudioAnalyzerError(
            f"{source} uses big-endian RIFX, which is not supported"
        )
    if container != b"RIFF" or wave_id != b"WAVE":
        raise AudioAnalyzerError(f"{source} is not a little-endian RIFF/WAVE file")

    file_size = os.fstat(handle.fileno()).st_size
    riff_end = 8 + riff_size
    if riff_end < 12 or riff_end > file_size:
        raise AudioAnalyzerError(
            f"{source} has a truncated or invalid RIFF size declaration"
        )

    format_payload: bytes | None = None
    data_offset: int | None = None
    data_size: int | None = None
    while handle.tell() < riff_end:
        remaining = riff_end - handle.tell()
        if remaining < 8:
            raise AudioAnalyzerError(
                f"{source} contains a partial RIFF chunk header"
            )
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise AudioAnalyzerError(
                f"{source} contains a truncated RIFF chunk header"
            )
        chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
        chunk_start = handle.tell()
        chunk_end = chunk_start + chunk_size
        padded_end = chunk_end + (chunk_size & 1)
        if padded_end > riff_end:
            raise AudioAnalyzerError(
                f"{source} contains a truncated {chunk_id!r} RIFF chunk"
            )

        if chunk_id == b"fmt ":
            if format_payload is not None:
                raise AudioAnalyzerError(f"{source} contains multiple fmt chunks")
            if chunk_size < 16:
                raise AudioAnalyzerError(f"{source} contains a short fmt chunk")
            # The base fields plus extensible metadata fit in 40 bytes. Reading
            # no more than 64 avoids trusting a malformed, huge fmt allocation.
            format_read_size = min(chunk_size, 64)
            format_payload = handle.read(format_read_size)
            if len(format_payload) != format_read_size:
                raise AudioAnalyzerError(f"{source} contains a truncated fmt chunk")
        elif chunk_id == b"data":
            if data_offset is not None:
                raise AudioAnalyzerError(f"{source} contains multiple data chunks")
            data_offset = chunk_start
            data_size = chunk_size

        handle.seek(padded_end)

    if format_payload is None:
        raise AudioAnalyzerError(f"{source} has no fmt chunk")
    if data_offset is None or data_size is None:
        raise AudioAnalyzerError(f"{source} has no data chunk")

    (
        format_tag,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = struct.unpack("<HHIIHH", format_payload[:16])

    if format_tag == WAVE_FORMAT_EXTENSIBLE:
        raise AudioAnalyzerError(
            f"{source} uses WAVE_FORMAT_EXTENSIBLE (0xFFFE), which is not "
            "supported; provide format-tag 1 PCM16 or format-tag 3 IEEE "
            "float32 WAV"
        )
    if format_tag == WAVE_FORMAT_PCM and bits_per_sample == 16:
        sample_width = 2
        sample_encoding = "pcm_s16le"
    elif format_tag == WAVE_FORMAT_IEEE_FLOAT and bits_per_sample == 32:
        sample_width = 4
        sample_encoding = "ieee_float32_le"
    elif format_tag == WAVE_FORMAT_PCM:
        raise AudioAnalyzerError(
            f"{source} uses unsupported PCM{bits_per_sample}; only PCM16 is "
            "supported"
        )
    elif format_tag == WAVE_FORMAT_IEEE_FLOAT:
        raise AudioAnalyzerError(
            f"{source} uses unsupported IEEE float{bits_per_sample}; only "
            "IEEE float32 is supported"
        )
    else:
        raise AudioAnalyzerError(
            f"{source} uses unsupported WAV format tag {format_tag}; only "
            "PCM16 (1) and IEEE float32 (3) are supported"
        )

    if channels != 1:
        raise AudioAnalyzerError(f"{source} must be mono; found {channels} channels")
    if sample_rate <= 0:
        raise AudioAnalyzerError(f"{source} has invalid sample rate {sample_rate}")
    expected_block_align = channels * sample_width
    if block_align != expected_block_align:
        raise AudioAnalyzerError(
            f"{source} has invalid block alignment {block_align}; expected "
            f"{expected_block_align}"
        )
    if byte_rate != sample_rate * block_align:
        raise AudioAnalyzerError(
            f"{source} has invalid byte rate {byte_rate}; expected "
            f"{sample_rate * block_align}"
        )
    if data_size % block_align:
        raise AudioAnalyzerError(f"{source} contains a partial audio sample")

    return _WaveStreamInfo(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        sample_encoding=sample_encoding,
        wav_format_tag=format_tag,
        sample_count=data_size // block_align,
        data_offset=data_offset,
        data_size=data_size,
    )


def _read_mono_wav_energy(
    path: str | Path,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    floor_dbfs: float = DEFAULT_DBFS_FLOOR,
    allowed_encodings: Collection[str] | None = None,
) -> TrackEnergy:
    """Stream a supported mono WAV into normalized per-frame mean-square energy."""
    source = Path(path)
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or frame_ms <= 0:
        raise AudioAnalyzerError("frame_ms must be a positive integer")
    floor_dbfs = _require_finite(floor_dbfs, "floor_dbfs")
    if floor_dbfs >= 0.0:
        raise AudioAnalyzerError("floor_dbfs must be below 0 dBFS")

    try:
        reader = source.open("rb")
    except OSError as exc:
        raise AudioAnalyzerError(f"cannot open WAV {source}: {exc}") from exc

    try:
        with reader:
            info = _parse_mono_wave_stream(reader, source)
            if (
                allowed_encodings is not None
                and info.sample_encoding not in allowed_encodings
            ):
                raise AudioAnalyzerError(
                    f"{source} must be 16-bit PCM; found {info.sample_encoding}"
                )
            frame_product = info.sample_rate * frame_ms
            if frame_product % 1_000:
                raise AudioAnalyzerError(
                    f"{source} sample rate {info.sample_rate} cannot form exact "
                    f"{frame_ms} ms frames"
                )
            frame_samples = frame_product // 1_000
            mean_squares: list[float] = []
            dbfs_values: list[float] = []
            reader.seek(info.data_offset)
            remaining_samples = info.sample_count
            decoded_sample_count = 0

            while remaining_samples:
                sample_count = min(frame_samples, remaining_samples)
                byte_count = sample_count * info.sample_width_bytes
                raw = reader.read(byte_count)
                if len(raw) != byte_count:
                    raise AudioAnalyzerError(f"{source} contains truncated audio data")

                if info.sample_encoding == "pcm_s16le":
                    samples = array("h")
                    samples.frombytes(raw)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    square_sum = sum(sample * sample for sample in samples)
                    mean_square = square_sum / (
                        len(samples) * PCM16_FULL_SCALE * PCM16_FULL_SCALE
                    )
                else:
                    samples = array("f")
                    if samples.itemsize != 4:
                        raise AudioAnalyzerError(
                            "this Python runtime does not provide 32-bit array('f')"
                        )
                    samples.frombytes(raw)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    square_sum_float = 0.0
                    for index, sample in enumerate(samples):
                        if not math.isfinite(sample):
                            raise AudioAnalyzerError(
                                f"{source} contains a non-finite IEEE float32 "
                                f"sample at index {decoded_sample_count + index}"
                            )
                        square_sum_float += sample * sample
                        if not math.isfinite(square_sum_float):
                            raise AudioAnalyzerError(
                                f"{source} float32 energy overflow near sample "
                                f"{decoded_sample_count + index}"
                            )
                    mean_square = square_sum_float / len(samples)

                mean_squares.append(mean_square)
                dbfs_values.append(_dbfs_from_mean_square(mean_square, floor_dbfs))
                decoded_sample_count += sample_count
                remaining_samples -= sample_count
    except OSError as exc:
        raise AudioAnalyzerError(f"cannot read WAV {source}: {exc}") from exc

    return TrackEnergy(
        path=str(source),
        sample_rate=info.sample_rate,
        channels=info.channels,
        sample_width_bytes=info.sample_width_bytes,
        sample_encoding=info.sample_encoding,
        wav_format_tag=info.wav_format_tag,
        sample_count=info.sample_count,
        duration_seconds=info.sample_count / info.sample_rate,
        frame_samples=frame_samples,
        frame_mean_squares=tuple(mean_squares),
        frame_dbfs=tuple(dbfs_values),
    )


def read_mono_wav_energy(
    path: str | Path,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    floor_dbfs: float = DEFAULT_DBFS_FLOOR,
) -> TrackEnergy:
    """Read mono PCM16 or IEEE float32 RIFF/WAVE acoustic energy."""
    return _read_mono_wav_energy(
        path, frame_ms=frame_ms, floor_dbfs=floor_dbfs
    )


def read_pcm16_mono_energy(
    path: str | Path,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    floor_dbfs: float = DEFAULT_DBFS_FLOOR,
) -> TrackEnergy:
    """Backward-compatible PCM16-only reader."""
    return _read_mono_wav_energy(
        path,
        frame_ms=frame_ms,
        floor_dbfs=floor_dbfs,
        allowed_encodings={"pcm_s16le"},
    )


def _validate_analysis_parameters(
    *,
    frame_ms: int,
    floor_dbfs: float,
    silence_threshold_dbfs: float,
    min_silence_ms: int,
    spike_tolerance_ms: int,
    activity_threshold_dbfs: float,
    shared_max_delta_db: float,
    shared_min_active_fraction: float,
    shared_min_coverage_fraction: float,
    offset_search_ms: int,
    energy_ceiling_dbfs: float,
) -> None:
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or frame_ms <= 0:
        raise AudioAnalyzerError("frame_ms must be a positive integer")
    if (
        isinstance(min_silence_ms, bool)
        or not isinstance(min_silence_ms, int)
        or min_silence_ms <= 0
    ):
        raise AudioAnalyzerError("min_silence_ms must be a positive integer")
    if (
        isinstance(spike_tolerance_ms, bool)
        or not isinstance(spike_tolerance_ms, int)
        or spike_tolerance_ms < 0
    ):
        raise AudioAnalyzerError("spike_tolerance_ms must be a non-negative integer")
    if (
        isinstance(offset_search_ms, bool)
        or not isinstance(offset_search_ms, int)
        or offset_search_ms < 0
    ):
        raise AudioAnalyzerError("offset_search_ms must be a non-negative integer")

    floor_dbfs = _require_finite(floor_dbfs, "floor_dbfs")
    silence_threshold_dbfs = _require_finite(
        silence_threshold_dbfs, "silence_threshold_dbfs"
    )
    activity_threshold_dbfs = _require_finite(
        activity_threshold_dbfs, "activity_threshold_dbfs"
    )
    shared_max_delta_db = _require_finite(
        shared_max_delta_db, "shared_max_delta_db"
    )
    shared_min_active_fraction = _require_finite(
        shared_min_active_fraction, "shared_min_active_fraction"
    )
    shared_min_coverage_fraction = _require_finite(
        shared_min_coverage_fraction, "shared_min_coverage_fraction"
    )
    energy_ceiling_dbfs = _require_finite(
        energy_ceiling_dbfs, "energy_ceiling_dbfs"
    )

    if not floor_dbfs < silence_threshold_dbfs < 0.0:
        raise AudioAnalyzerError(
            "silence_threshold_dbfs must be above floor_dbfs and below 0 dBFS"
        )
    if not silence_threshold_dbfs <= activity_threshold_dbfs < 0.0:
        raise AudioAnalyzerError(
            "activity_threshold_dbfs must be at least the silence threshold and below 0"
        )
    if shared_max_delta_db < 0.0:
        raise AudioAnalyzerError("shared_max_delta_db must be non-negative")
    if not 0.0 <= shared_min_active_fraction <= 1.0:
        raise AudioAnalyzerError(
            "shared_min_active_fraction must be between 0 and 1"
        )
    if not 0.0 <= shared_min_coverage_fraction <= 1.0:
        raise AudioAnalyzerError(
            "shared_min_coverage_fraction must be between 0 and 1"
        )
    if not silence_threshold_dbfs < energy_ceiling_dbfs <= 0.0:
        raise AudioAnalyzerError(
            "energy_ceiling_dbfs must be above the silence threshold and at most 0"
        )


def _bridge_short_false_runs(
    low_energy: Sequence[bool], *, max_frames: int
) -> tuple[list[bool], list[tuple[int, int]]]:
    bridged = list(low_energy)
    spans: list[tuple[int, int]] = []
    if max_frames <= 0:
        return bridged, spans

    index = 0
    while index < len(bridged):
        if bridged[index]:
            index += 1
            continue
        end = index + 1
        while end < len(bridged) and not bridged[end]:
            end += 1
        surrounded = index > 0 and end < len(bridged)
        if surrounded and end - index <= max_frames:
            bridged[index:end] = [True] * (end - index)
            spans.append((index, end))
        index = end
    return bridged, spans


def find_dual_mic_silence(
    track_1_dbfs: Sequence[float],
    track_2_dbfs: Sequence[float],
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
    spike_tolerance_ms: int = DEFAULT_SPIKE_TOLERANCE_MS,
) -> list[dict[str, Any]]:
    """Return half-open dual-mic low-energy runs at 20 ms resolution."""
    if len(track_1_dbfs) != len(track_2_dbfs):
        raise AudioAnalyzerError("dual-mic energy sequences must have equal length")
    threshold_dbfs = _require_finite(threshold_dbfs, "threshold_dbfs")
    min_frames = math.ceil(min_silence_ms / frame_ms)
    max_spike_frames = spike_tolerance_ms // frame_ms
    original_low = [
        first < threshold_dbfs and second < threshold_dbfs
        for first, second in zip(track_1_dbfs, track_2_dbfs)
    ]
    bridged_low, bridged_spans = _bridge_short_false_runs(
        original_low, max_frames=max_spike_frames
    )

    results: list[dict[str, Any]] = []
    index = 0
    while index < len(bridged_low):
        if not bridged_low[index]:
            index += 1
            continue
        end = index + 1
        while end < len(bridged_low) and bridged_low[end]:
            end += 1
        if end - index >= min_frames:
            included_spikes = [
                (spike_start, spike_end)
                for spike_start, spike_end in bridged_spans
                if index <= spike_start and spike_end <= end
            ]
            results.append(
                {
                    "kind": "dual_mic_low_energy_candidate",
                    "start_frame_20ms": index,
                    "end_frame_20ms": end,
                    "start_seconds": round(index * frame_ms / 1_000, 6),
                    "end_seconds": round(end * frame_ms / 1_000, 6),
                    "duration_seconds": round((end - index) * frame_ms / 1_000, 6),
                    "threshold_dbfs": threshold_dbfs,
                    "bridged_spike_count": len(included_spikes),
                    "bridged_spike_duration_ms": sum(
                        (spike_end - spike_start) * frame_ms
                        for spike_start, spike_end in included_spikes
                    ),
                    "review_required": True,
                    "does_not_imply_edit_label": True,
                    "review_notice": (
                        "Low dual-mic RMS is an acoustic candidate only; confirm "
                        "room tone, source integrity, transcript conflicts, and "
                        "picture context before making an edit decision."
                    ),
                }
            )
        index = end
    return results


def _weighted_cue_metrics(
    track: TrackEnergy,
    *,
    start_ms: float,
    end_ms: float,
    frame_ms: int,
    activity_threshold_dbfs: float,
    common_frame_count: int,
    floor_dbfs: float,
) -> tuple[float | None, float, float]:
    common_end_ms = common_frame_count * frame_ms
    clipped_start = max(0.0, start_ms)
    clipped_end = min(float(common_end_ms), end_ms)
    requested_duration = max(0.0, end_ms - start_ms)
    if clipped_end <= clipped_start or requested_duration <= 0.0:
        return None, 0.0, 0.0

    first_frame = max(0, int(math.floor(clipped_start / frame_ms)))
    last_frame = min(
        common_frame_count, int(math.ceil(clipped_end / frame_ms))
    )
    weighted_square = 0.0
    weighted_active_ms = 0.0
    weighted_ms = 0.0
    for frame_index in range(first_frame, last_frame):
        frame_start = frame_index * frame_ms
        frame_end = frame_start + frame_ms
        overlap = min(clipped_end, frame_end) - max(clipped_start, frame_start)
        if overlap <= 0.0:
            continue
        weighted_square += track.frame_mean_squares[frame_index] * overlap
        weighted_ms += overlap
        if track.frame_dbfs[frame_index] >= activity_threshold_dbfs:
            weighted_active_ms += overlap

    if weighted_ms <= 0.0:
        return None, 0.0, 0.0
    dbfs = _dbfs_from_mean_square(weighted_square / weighted_ms, floor_dbfs)
    active_fraction = weighted_active_ms / weighted_ms
    coverage_fraction = weighted_ms / requested_duration
    return dbfs, active_fraction, min(1.0, coverage_fraction)


def cue_energy_features(
    cues: Sequence[Cue],
    track_1: TrackEnergy,
    track_2: TrackEnergy,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    cue_offset_ms: float = 0.0,
    activity_threshold_dbfs: float = DEFAULT_ACTIVITY_THRESHOLD_DBFS,
    shared_max_delta_db: float = DEFAULT_SHARED_MAX_DELTA_DB,
    shared_min_active_fraction: float = DEFAULT_SHARED_MIN_ACTIVE_FRACTION,
    shared_min_coverage_fraction: float = DEFAULT_SHARED_MIN_COVERAGE_FRACTION,
    floor_dbfs: float = DEFAULT_DBFS_FLOOR,
    track_1_name: str = "track_1",
    track_2_name: str = "track_2",
) -> list[dict[str, Any]]:
    """Attach dual-mic RMS evidence to each cue without assigning a speaker."""
    common_frame_count = min(
        len(track_1.frame_dbfs), len(track_2.frame_dbfs)
    )
    cue_offset_ms = _require_finite(cue_offset_ms, "cue_offset_ms")
    features: list[dict[str, Any]] = []
    for cue in cues:
        start_ms = cue.start_ms + cue_offset_ms
        end_ms = cue.end_ms + cue_offset_ms
        first_dbfs, first_active, first_coverage = _weighted_cue_metrics(
            track_1,
            start_ms=start_ms,
            end_ms=end_ms,
            frame_ms=frame_ms,
            activity_threshold_dbfs=activity_threshold_dbfs,
            common_frame_count=common_frame_count,
            floor_dbfs=floor_dbfs,
        )
        second_dbfs, second_active, second_coverage = _weighted_cue_metrics(
            track_2,
            start_ms=start_ms,
            end_ms=end_ms,
            frame_ms=frame_ms,
            activity_threshold_dbfs=activity_threshold_dbfs,
            common_frame_count=common_frame_count,
            floor_dbfs=floor_dbfs,
        )
        delta = (
            first_dbfs - second_dbfs
            if first_dbfs is not None and second_dbfs is not None
            else None
        )
        enough_level = bool(
            first_dbfs is not None
            and second_dbfs is not None
            and first_dbfs >= activity_threshold_dbfs
            and second_dbfs >= activity_threshold_dbfs
        )
        enough_activity = (
            first_active >= shared_min_active_fraction
            and second_active >= shared_min_active_fraction
        )
        enough_coverage = (
            first_coverage >= shared_min_coverage_fraction
            and second_coverage >= shared_min_coverage_fraction
        )
        balanced = delta is not None and abs(delta) <= shared_max_delta_db
        shared_candidate = bool(
            enough_level and enough_activity and enough_coverage and balanced
        )

        features.append(
            {
                "cue_id": cue.cue_id,
                "srt_start_seconds": round(cue.start_ms / 1_000, 6),
                "srt_end_seconds": round(cue.end_ms / 1_000, 6),
                "analysis_start_seconds": round(start_ms / 1_000, 6),
                "analysis_end_seconds": round(end_ms / 1_000, 6),
                "cue_offset_applied_ms": round(cue_offset_ms, 6),
                "text": cue.text,
                "track_1": {
                    "name": track_1_name,
                    "dbfs": round(first_dbfs, 6) if first_dbfs is not None else None,
                    "active_fraction": round(first_active, 6),
                    "coverage_fraction": round(first_coverage, 6),
                },
                "track_2": {
                    "name": track_2_name,
                    "dbfs": round(second_dbfs, 6) if second_dbfs is not None else None,
                    "active_fraction": round(second_active, 6),
                    "coverage_fraction": round(second_coverage, 6),
                },
                "delta_db_track_1_minus_track_2": (
                    round(delta, 6) if delta is not None else None
                ),
                "shared_far_field_candidate": shared_candidate,
                "shared_far_field_criteria": {
                    "both_tracks_at_or_above_activity_threshold": enough_level,
                    "both_tracks_meet_min_active_fraction": enough_activity,
                    "both_tracks_meet_min_coverage_fraction": enough_coverage,
                    "absolute_delta_within_limit": balanced,
                    "activity_threshold_dbfs": activity_threshold_dbfs,
                    "min_active_fraction": shared_min_active_fraction,
                    "min_coverage_fraction": shared_min_coverage_fraction,
                    "max_absolute_delta_db": shared_max_delta_db,
                },
                "review_required": True,
                "review_notice": (
                    "Balanced dual-mic level can also be crosstalk, overlap, room "
                    "noise, or music; it does not identify a speaker or edit label."
                ),
                "speaker_identity": None,
                "edit_label": None,
            }
        )
    return features


def _coverage_mask(cues: Sequence[Cue], frame_count: int, frame_ms: int) -> list[int]:
    mask = [0] * frame_count
    for cue in cues:
        start = max(0, int(math.floor(cue.start_ms / frame_ms)))
        end = min(frame_count, int(math.ceil(cue.end_ms / frame_ms)))
        if start < end:
            mask[start:end] = [1] * (end - start)
    return mask


def _pearson_binary_energy(mask: Sequence[int], energy: Sequence[float]) -> float | None:
    count = len(mask)
    if count != len(energy) or count < 2:
        return None
    sum_x = float(sum(mask))
    sum_y = math.fsum(energy)
    sum_xx = sum_x
    sum_yy = math.fsum(value * value for value in energy)
    sum_xy = math.fsum(value for bit, value in zip(mask, energy) if bit)
    numerator = count * sum_xy - sum_x * sum_y
    denominator_x = count * sum_xx - sum_x * sum_x
    denominator_y = count * sum_yy - sum_y * sum_y
    if denominator_x <= 0.0 or denominator_y <= 0.0:
        return None
    return numerator / math.sqrt(denominator_x * denominator_y)


def estimate_srt_energy_offset(
    cues: Sequence[Cue],
    track_1_dbfs: Sequence[float],
    track_2_dbfs: Sequence[float],
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    search_ms: int = DEFAULT_OFFSET_SEARCH_MS,
    silence_threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
    energy_ceiling_dbfs: float = DEFAULT_ENERGY_CEILING_DBFS,
) -> dict[str, Any]:
    """Estimate a coarse fixed SRT-to-audio shift from coverage and energy.

    The returned offset is added to SRT timestamps to compare them with audio.
    It must not be treated as a word or phoneme alignment.
    """
    frame_count = min(len(track_1_dbfs), len(track_2_dbfs))
    base: dict[str, Any] = {
        "method": "srt_coverage_mask_vs_dual_mic_energy_pearson",
        "offset_definition": "add best_offset_ms to SRT timestamps to compare with audio",
        "resolution_ms": frame_ms,
        "search_range_ms": search_ms,
        "word_level_alignment": False,
        "fixed_offset_only": True,
        "drift_established": False,
        "automatic_application_allowed": False,
        "review_required": True,
        "review_notice": (
            "SRT coverage shifted against energy can suggest only one coarse "
            "fixed offset. It does not establish drift or word-level timing."
        ),
    }
    if not cues:
        return {**base, "available": False, "reason": "no SRT cues supplied"}
    if frame_count < 2:
        return {**base, "available": False, "reason": "audio is too short"}

    mask = _coverage_mask(cues, frame_count, frame_ms)
    if not any(mask) or all(mask):
        return {
            **base,
            "available": False,
            "reason": "SRT coverage mask has no usable visible/blank variation",
        }

    scale = energy_ceiling_dbfs - silence_threshold_dbfs
    energy = [
        min(
            1.0,
            max(
                0.0,
                (max(first, second) - silence_threshold_dbfs) / scale,
            ),
        )
        for first, second in zip(track_1_dbfs[:frame_count], track_2_dbfs[:frame_count])
    ]
    if max(energy) - min(energy) <= 1e-12:
        return {
            **base,
            "available": False,
            "reason": "dual-mic energy has no usable variation",
        }

    max_shift = search_ms // frame_ms
    scored: list[tuple[float, int]] = []
    for shift in range(-max_shift, max_shift + 1):
        if shift < 0:
            aligned_mask = mask[-shift:]
            aligned_energy = energy[: frame_count + shift]
        elif shift > 0:
            aligned_mask = mask[: frame_count - shift]
            aligned_energy = energy[shift:]
        else:
            aligned_mask = mask
            aligned_energy = energy
        correlation = _pearson_binary_energy(aligned_mask, aligned_energy)
        if correlation is not None:
            scored.append((correlation, shift))

    if not scored:
        return {
            **base,
            "available": False,
            "reason": "no valid correlation could be calculated",
        }
    # Prefer the smallest absolute offset when correlation ties at floating-point
    # precision. This avoids inventing a shift from a flat plateau.
    scored.sort(key=lambda item: (-item[0], abs(item[1]), item[1]))
    best_correlation, best_shift = scored[0]
    zero_score = next((score for score, shift in scored if shift == 0), None)
    peak_exclusion_frames = max(1, math.ceil(100 / frame_ms))
    runner_candidates = [
        (score, shift)
        for score, shift in scored[1:]
        if abs(shift - best_shift) > peak_exclusion_frames
    ]
    runner_up = runner_candidates[0] if runner_candidates else None
    coverage_fraction = sum(mask) / len(mask)
    boundary_hit = abs(best_shift) == max_shift and max_shift > 0
    runner_margin = (
        best_correlation - runner_up[0] if runner_up is not None else None
    )
    reliable = bool(
        not boundary_hit
        and 0.05 <= coverage_fraction <= 0.9
        and best_correlation >= 0.5
        and runner_margin is not None
        and runner_margin >= 0.02
    )
    return {
        **base,
        "available": True,
        "best_offset_ms": best_shift * frame_ms,
        "best_offset_seconds": round(best_shift * frame_ms / 1_000, 6),
        "correlation": round(best_correlation, 9),
        "zero_offset_correlation": (
            round(zero_score, 9) if zero_score is not None else None
        ),
        "correlation_gain_over_zero": (
            round(best_correlation - zero_score, 9)
            if zero_score is not None
            else None
        ),
        "srt_coverage_fraction": round(coverage_fraction, 9),
        "runner_up_offset_ms": (
            runner_up[1] * frame_ms if runner_up is not None else None
        ),
        "runner_up_correlation": (
            round(runner_up[0], 9) if runner_up is not None else None
        ),
        "best_to_runner_up_margin": (
            round(runner_margin, 9) if runner_margin is not None else None
        ),
        "search_boundary_hit": boundary_hit,
        "reliable_fixed_offset_candidate": reliable,
        "reliability_criteria": {
            "coverage_fraction_between_0_05_and_0_90": (
                0.05 <= coverage_fraction <= 0.9
            ),
            "correlation_at_least_0_5": best_correlation >= 0.5,
            "runner_up_margin_at_least_0_02": (
                runner_margin is not None and runner_margin >= 0.02
            ),
            "search_boundary_not_hit": not boundary_hit,
        },
        "interpretation": (
            "coarse fixed-offset candidate only; this is not word-level "
            "alignment; verify against recognizable speech before use"
        ),
    }


def analyze_audio(
    track_1_path: str | Path,
    track_2_path: str | Path,
    *,
    srt_text: str | None = None,
    source_srt: str | None = None,
    frame_ms: int = DEFAULT_FRAME_MS,
    floor_dbfs: float = DEFAULT_DBFS_FLOOR,
    silence_threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
    spike_tolerance_ms: int = DEFAULT_SPIKE_TOLERANCE_MS,
    activity_threshold_dbfs: float = DEFAULT_ACTIVITY_THRESHOLD_DBFS,
    shared_max_delta_db: float = DEFAULT_SHARED_MAX_DELTA_DB,
    shared_min_active_fraction: float = DEFAULT_SHARED_MIN_ACTIVE_FRACTION,
    shared_min_coverage_fraction: float = DEFAULT_SHARED_MIN_COVERAGE_FRACTION,
    cue_offset_ms: float = 0.0,
    offset_search_ms: int = DEFAULT_OFFSET_SEARCH_MS,
    energy_ceiling_dbfs: float = DEFAULT_ENERGY_CEILING_DBFS,
    track_1_name: str = "track_1",
    track_2_name: str = "track_2",
) -> dict[str, Any]:
    """Analyze two synchronized mic WAVs and return review-only JSON evidence."""
    _validate_analysis_parameters(
        frame_ms=frame_ms,
        floor_dbfs=floor_dbfs,
        silence_threshold_dbfs=silence_threshold_dbfs,
        min_silence_ms=min_silence_ms,
        spike_tolerance_ms=spike_tolerance_ms,
        activity_threshold_dbfs=activity_threshold_dbfs,
        shared_max_delta_db=shared_max_delta_db,
        shared_min_active_fraction=shared_min_active_fraction,
        shared_min_coverage_fraction=shared_min_coverage_fraction,
        offset_search_ms=offset_search_ms,
        energy_ceiling_dbfs=energy_ceiling_dbfs,
    )
    cue_offset_ms = _require_finite(cue_offset_ms, "cue_offset_ms")
    if not track_1_name or not track_2_name:
        raise AudioAnalyzerError("track names must not be empty")

    track_1 = read_mono_wav_energy(
        track_1_path, frame_ms=frame_ms, floor_dbfs=floor_dbfs
    )
    track_2 = read_mono_wav_energy(
        track_2_path, frame_ms=frame_ms, floor_dbfs=floor_dbfs
    )
    if track_1.sample_rate != track_2.sample_rate:
        raise AudioAnalyzerError(
            "microphone WAVs must have the same sample rate: "
            f"{track_1.sample_rate} != {track_2.sample_rate}"
        )
    if track_1.frame_samples != track_2.frame_samples:
        raise AudioAnalyzerError("microphone WAVs produced incompatible analysis frames")

    common_frame_count = min(
        len(track_1.frame_dbfs), len(track_2.frame_dbfs)
    )
    first_dbfs = track_1.frame_dbfs[:common_frame_count]
    second_dbfs = track_2.frame_dbfs[:common_frame_count]
    cues: list[Cue] = []
    if srt_text is not None:
        try:
            cues = parse_srt(srt_text)
        except SrtAnalyzerError as exc:
            raise AudioAnalyzerError(f"invalid SRT: {exc}") from exc

    silence = find_dual_mic_silence(
        first_dbfs,
        second_dbfs,
        frame_ms=frame_ms,
        threshold_dbfs=silence_threshold_dbfs,
        min_silence_ms=min_silence_ms,
        spike_tolerance_ms=spike_tolerance_ms,
    )
    for candidate in silence:
        start_ms = candidate["start_seconds"] * 1_000
        end_ms = candidate["end_seconds"] * 1_000
        overlapping_cues = [
            cue.cue_id
            for cue in cues
            if cue.end_ms + cue_offset_ms > start_ms
            and cue.start_ms + cue_offset_ms < end_ms
        ]
        candidate["srt_overlap_cue_ids"] = overlapping_cues
        candidate["srt_overlap_present"] = bool(overlapping_cues)
    features = cue_energy_features(
        cues,
        track_1,
        track_2,
        frame_ms=frame_ms,
        cue_offset_ms=cue_offset_ms,
        activity_threshold_dbfs=activity_threshold_dbfs,
        shared_max_delta_db=shared_max_delta_db,
        shared_min_active_fraction=shared_min_active_fraction,
        shared_min_coverage_fraction=shared_min_coverage_fraction,
        floor_dbfs=floor_dbfs,
        track_1_name=track_1_name,
        track_2_name=track_2_name,
    )
    offset = estimate_srt_energy_offset(
        cues,
        first_dbfs,
        second_dbfs,
        frame_ms=frame_ms,
        search_ms=offset_search_ms,
        silence_threshold_dbfs=silence_threshold_dbfs,
        energy_ceiling_dbfs=energy_ceiling_dbfs,
    )
    duration_difference = abs(track_1.duration_seconds - track_2.duration_seconds)
    shared_count = sum(
        bool(feature["shared_far_field_candidate"]) for feature in features
    )

    return {
        "schema_version": 1,
        "analysis_type": "dual_mic_acoustic_evidence",
        "review_required": True,
        "review_notice": REVIEW_NOTICE,
        "does_not_assign_speaker": True,
        "does_not_assign_edit_label": True,
        "word_level_alignment": False,
        "limitations": {
            "word_aligned": False,
            "speaker_identity_inferred": False,
            "edit_label_inferred": False,
            "fixed_offset_only": True,
            "drift_established": False,
        },
        "parameters": {
            "frame_ms": frame_ms,
            "dbfs_floor": floor_dbfs,
            "silence_threshold_dbfs": silence_threshold_dbfs,
            "min_silence_ms": min_silence_ms,
            "spike_tolerance_ms": spike_tolerance_ms,
            "activity_threshold_dbfs": activity_threshold_dbfs,
            "shared_max_absolute_delta_db": shared_max_delta_db,
            "shared_min_active_fraction": shared_min_active_fraction,
            "shared_min_coverage_fraction": shared_min_coverage_fraction,
            "cue_offset_applied_ms": cue_offset_ms,
            "offset_search_ms": offset_search_ms,
            "offset_energy_ceiling_dbfs": energy_ceiling_dbfs,
        },
        "tracks": {
            "track_1": {
                "name": track_1_name,
                "path": track_1.path,
                "sample_rate": track_1.sample_rate,
                "channels": track_1.channels,
                "sample_width_bits": track_1.sample_width_bytes * 8,
                "sample_encoding": track_1.sample_encoding,
                "wav_format_tag": track_1.wav_format_tag,
                "sample_count": track_1.sample_count,
                "duration_seconds": round(track_1.duration_seconds, 9),
            },
            "track_2": {
                "name": track_2_name,
                "path": track_2.path,
                "sample_rate": track_2.sample_rate,
                "channels": track_2.channels,
                "sample_width_bits": track_2.sample_width_bytes * 8,
                "sample_encoding": track_2.sample_encoding,
                "wav_format_tag": track_2.wav_format_tag,
                "sample_count": track_2.sample_count,
                "duration_seconds": round(track_2.duration_seconds, 9),
            },
        },
        "synchronization": {
            "same_sample_rate": True,
            "common_analysis_frames": common_frame_count,
            "common_duration_seconds": round(
                common_frame_count * frame_ms / 1_000, 9
            ),
            "duration_difference_seconds": round(duration_difference, 9),
            "sample_alignment_verified": False,
            "review_required": True,
        },
        "srt": {
            "supplied": srt_text is not None,
            "path": source_srt,
            "cue_count": len(cues),
            "cue_features_time_basis": (
                "SRT timestamps plus explicit cue_offset_applied_ms; the coarse "
                "estimated offset is not auto-applied"
            ),
            "review_required": True,
        },
        "true_silence_candidates": silence,
        "cue_features": features,
        "fixed_offset_estimate": offset,
        "summary": {
            "true_silence_candidate_count": len(silence),
            "cue_count": len(features),
            "shared_far_field_candidate_count": shared_count,
            "fixed_offset_estimate_available": bool(offset.get("available")),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure review-only acoustic evidence from two synchronized mono "
            "PCM16 or IEEE float32 WAVs. This command never assigns red/yellow "
            "labels or edits XML."
        )
    )
    parser.add_argument(
        "track_1", type=Path, help="first synchronized mono PCM16/float32 WAV"
    )
    parser.add_argument(
        "track_2", type=Path, help="second synchronized mono PCM16/float32 WAV"
    )
    parser.add_argument("--srt", type=Path, help="optional UTF-8 SRT")
    parser.add_argument("--track-1-name", default="track_1")
    parser.add_argument("--track-2-name", default="track_2")
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    parser.add_argument(
        "--silence-threshold-dbfs",
        type=float,
        default=DEFAULT_SILENCE_THRESHOLD_DBFS,
    )
    parser.add_argument("--min-silence-ms", type=int, default=DEFAULT_MIN_SILENCE_MS)
    parser.add_argument(
        "--spike-tolerance-ms", type=int, default=DEFAULT_SPIKE_TOLERANCE_MS
    )
    parser.add_argument(
        "--activity-threshold-dbfs",
        type=float,
        default=DEFAULT_ACTIVITY_THRESHOLD_DBFS,
    )
    parser.add_argument(
        "--shared-max-delta-db", type=float, default=DEFAULT_SHARED_MAX_DELTA_DB
    )
    parser.add_argument(
        "--shared-min-active-fraction",
        type=float,
        default=DEFAULT_SHARED_MIN_ACTIVE_FRACTION,
    )
    parser.add_argument(
        "--shared-min-coverage-fraction",
        type=float,
        default=DEFAULT_SHARED_MIN_COVERAGE_FRACTION,
    )
    parser.add_argument(
        "--cue-offset-ms",
        type=float,
        default=0.0,
        help="explicit offset added to SRT cue times for cue features (default: 0)",
    )
    parser.add_argument(
        "--offset-search-ms", type=int, default=DEFAULT_OFFSET_SEARCH_MS
    )
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    parser.add_argument("--force", action="store_true", help="replace JSON output")
    parser.add_argument("--indent", type=int, default=2)
    return parser


def _write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.indent < 0:
            raise AudioAnalyzerError("indent must be non-negative")
        srt_text: str | None = None
        if args.srt is not None:
            try:
                srt_text = args.srt.read_bytes().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise AudioAnalyzerError(f"input SRT is not valid UTF-8: {exc}") from exc

        report = analyze_audio(
            args.track_1,
            args.track_2,
            srt_text=srt_text,
            source_srt=str(args.srt) if args.srt is not None else None,
            frame_ms=args.frame_ms,
            silence_threshold_dbfs=args.silence_threshold_dbfs,
            min_silence_ms=args.min_silence_ms,
            spike_tolerance_ms=args.spike_tolerance_ms,
            activity_threshold_dbfs=args.activity_threshold_dbfs,
            shared_max_delta_db=args.shared_max_delta_db,
            shared_min_active_fraction=args.shared_min_active_fraction,
            shared_min_coverage_fraction=args.shared_min_coverage_fraction,
            cue_offset_ms=args.cue_offset_ms,
            offset_search_ms=args.offset_search_ms,
            track_1_name=args.track_1_name,
            track_2_name=args.track_2_name,
        )
        output_text = (
            json.dumps(
                report, ensure_ascii=False, indent=args.indent, allow_nan=False
            )
            + "\n"
        )
        if args.output is None:
            sys.stdout.write(output_text)
        else:
            output_resolved = args.output.resolve()
            input_paths = {args.track_1.resolve(), args.track_2.resolve()}
            if args.srt is not None:
                input_paths.add(args.srt.resolve())
            if output_resolved in input_paths:
                raise AudioAnalyzerError("JSON output path must differ from all inputs")
            if args.output.exists() and not args.force:
                raise AudioAnalyzerError(
                    f"output already exists: {args.output} (use --force to replace it)"
                )
            _write_text_atomically(args.output, output_text)
        return 0
    except (OSError, AudioAnalyzerError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
