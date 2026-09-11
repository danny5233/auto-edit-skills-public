#!/usr/bin/env python3
"""Analyze or validate SRT files against the user's subtitle style."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")
FORBIDDEN_PUNCT = re.compile(r"[，。！？；：,.!?;:「」『』（）()〔〕【】《》〈〉﹁﹂﹃﹄…]")
NUMERIC_RATIO_COLON = re.compile(r"(?<=\d):(?=\d)")
KNOWN_TYPOS = {
    "説": "說",
    "時後": "時候",
}
GLOBAL_TAIL_FILL_THRESHOLD_MS = 2000
USER_VISUAL_ANCHOR_MODE = "user_confirmed_visual_anchor"
LOCKED_PREFIX_WAVEFORM_XML_MODE = (
    "source_track_xml_after_locked_text_prefix_waveform_reconciliation"
)
JOB_DECISION_SUBDIRECTORIES = frozenset({"05_draft", "05_delivery"})
VISIBLE_TEXT_REFERENCE_BASENAME = "editorial_snap_candidate_v3.srt"
FONT_TAG = re.compile(r"<[^>]+>")
LINEBREAK_MARKER = re.compile(r"(?:\\[Nn]|<br\s*/?>)", re.IGNORECASE)
MIXED_SPACING = re.compile(
    r"(?<=[\u3400-\u9fff])\s+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])\s+(?=[\u3400-\u9fff])"
)
STANDALONE_LAUGH = re.compile(
    r"^(?:哈+|呵+|嘿+|(?:ha){2,}|(?:he){2,})$", re.IGNORECASE
)
STANDALONE_FILLER = re.compile(
    r"^(?:嗯+|恩+|呃+|額+|啊+|喔+|哦+|欸+|哎+|咳+)$",
    re.IGNORECASE,
)
ATTACHED_FILLER = re.compile(
    r"^(?:嗯+|恩+|呃+|額+|喔+|哦+|欸+|哎+)(?=.)|"
    r"(?<=.)(?:嗯+|恩+|喔+|哦+|欸+|哎+)$",
    re.IGNORECASE,
)
GLUED_REACTION = re.compile(
    r"(?:對啊|沒有|好|OK)(?:嗯+|恩+|喔+|哦+|欸+|哎+)(?=.)",
    re.IGNORECASE,
)
SUSPICIOUS_ENGLISH_COLLAPSE = {
    "Letsgo": "Let's go",
    "Let'sgo": "Let's go",
    "problemsolved": "problem solved",
    "finalanswer": "final answer",
    "MichaelJordan": "Michael Jordan",
    "LeBronJames": "LeBron James",
    "StephenCurry": "Stephen Curry",
    "ofcourse": "of course",
    "Dreamgirl": "Dream girl",
    "ElonMusk": "Elon Musk",
}
SUSPICIOUS_LEFT_BOUNDARIES = (
    "因為",
    "如果",
    "但是",
    "而且",
    "然後",
    "就是",
    "可以",
    "應該",
    "的",
    "得",
    "地",
    "把",
    "被",
    "跟",
    "和",
    "與",
    "或",
    "不",
    "沒",
    "很",
    "更",
    "最",
    "要",
    "會",
    "能",
)


@dataclass
class Cue:
    number: int
    start_ms: int
    end_ms: int
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(line.strip() for line in self.lines if line.strip())

    @property
    def visible_text(self) -> str:
        return FONT_TAG.sub("", self.text)

    @property
    def char_count(self) -> int:
        return len(re.sub(r"\s", "", self.visible_text))

    @property
    def duration_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000

    @property
    def cps(self) -> float:
        return self.char_count / self.duration_s if self.duration_s > 0 else float("inf")


def parse_timestamp(value: str) -> int:
    match = TIME_RE.match(value)
    if not match:
        raise ValueError(f"invalid timestamp: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def format_timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def nominal_frame(timestamp_ms: int, fps_numerator: int, fps_denominator: int) -> int:
    """Return the Premiere nominal frame containing an SRT millisecond stamp."""
    return timestamp_ms * fps_numerator // (1000 * fps_denominator)


def serialized_frame_timestamp_ms(
    frame: int, fps_numerator: int, fps_denominator: int
) -> int:
    """Return the ceil-ms SRT stamp that round-trips to a nominal frame."""
    numerator = frame * 1000 * fps_denominator
    return (numerator + fps_numerator - 1) // fps_numerator


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_evidence_job_root(decisions_root: Path) -> Path | None:
    """Infer one unambiguous job root from the decisions file directory."""
    resolved_root = decisions_root.resolve()
    candidates: list[Path] = []
    if resolved_root.name in JOB_DECISION_SUBDIRECTORIES:
        candidates.append(resolved_root.parent)
    if (resolved_root / "05_draft").is_dir() and (
        resolved_root / "04_analysis"
    ).is_dir():
        candidates.append(resolved_root)
    unique_candidates = list(dict.fromkeys(candidate.resolve() for candidate in candidates))
    if len(unique_candidates) != 1:
        return None
    return unique_candidates[0]


def job_aware_evidence_fallback(
    raw: str, label: str, decisions_root: Path
) -> Path | None:
    """Resolve only explicitly allowed evidence inside the same inferred job."""
    job_root = infer_evidence_job_root(decisions_root)
    if job_root is None:
        return None

    normalized = raw.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute():
        return None

    if label == "visible text reference_srt":
        if relative.parts != (VISIBLE_TEXT_REFERENCE_BASENAME,):
            return None
        allowed_root = (job_root / "05_draft").resolve()
        candidate = (allowed_root / VISIBLE_TEXT_REFERENCE_BASENAME).resolve()
    elif label == "xml cut audit path":
        if (
            len(relative.parts) < 3
            or relative.parts[:2] != ("..", "04_analysis")
            or any(part in {"", ".", ".."} for part in relative.parts[2:])
        ):
            return None
        allowed_root = (job_root / "04_analysis").resolve()
        candidate = allowed_root.joinpath(*relative.parts[2:]).resolve()
    else:
        return None

    if not allowed_root.is_relative_to(job_root):
        return None
    if not candidate.is_relative_to(allowed_root) or not candidate.is_file():
        return None
    return candidate


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(payload)


def visible_text_stream(cues: list[Cue]) -> str:
    """Join visible cue text without a delimiter so resegmentation is neutral."""
    return "".join(cue.visible_text for cue in cues)


def strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def aliased_value(
    item: dict[str, object], canonical: str, alias: str, label: str
) -> object:
    has_canonical = canonical in item
    has_alias = alias in item
    if not has_canonical and not has_alias:
        raise ValueError(f"{label} requires {canonical}")
    if has_canonical and has_alias and item[canonical] != item[alias]:
        raise ValueError(f"{canonical} and {alias} must match")
    return item[canonical] if has_canonical else item[alias]


def parse_srt(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not raw:
        raise ValueError("empty SRT")
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", raw):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or " --> " not in lines[1]:
            raise ValueError(f"malformed SRT block: {block[:100]!r}")
        start, end = lines[1].split(" --> ", 1)
        cues.append(Cue(int(lines[0]), parse_timestamp(start), parse_timestamp(end), lines[2:]))
    return cues


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(ratio * len(ordered)))]


def analyze(paths: list[Path], as_json: bool) -> int:
    results = []
    for path in paths:
        cues = parse_srt(path)
        lengths = [cue.char_count for cue in cues]
        speeds = [cue.cps for cue in cues if cue.duration_s > 0]
        gaps = [cues[i + 1].start_ms - cues[i].end_ms for i in range(len(cues) - 1)]
        result = {
            "file": str(path),
            "segments": len(cues),
            "length": {
                "median": statistics.median(lengths),
                "mean": round(statistics.mean(lengths), 2),
                "p90": percentile(lengths, 0.90),
                "max": max(lengths),
            },
            "cps": {
                "median": round(statistics.median(speeds), 2),
                "p90": round(percentile(speeds, 0.90), 2),
                "max": round(max(speeds), 2),
            },
            "timing": {
                "contiguous": sum(gap == 0 for gap in gaps),
                "positive_gap_under_1s": sum(0 < gap < 1000 for gap in gaps),
                "gap_at_least_1s": sum(gap >= 1000 for gap in gaps),
                "overlap": sum(gap < 0 for gap in gaps),
            },
        }
        results.append(result)

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(result["file"])
            print(f"  segments: {result['segments']}")
            print(f"  length: {result['length']}")
            print(f"  cps: {result['cps']}")
            print(f"  timing: {result['timing']}")
    return 0


def flatten(input_path: Path, output_path: Path) -> int:
    cues = parse_srt(input_path)
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.number}\n"
            f"{format_timestamp(cue.start_ms)} --> {format_timestamp(cue.end_ms)}\n"
            f"{cue.text}"
        )
    output_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    print(f"{output_path}: {len(cues)} 段 已轉為單行字幕")
    return 0


def load_decisions(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("decisions JSON must be an object")
    return data


def load_style_policy(profile_path: Path | None, profile_data: dict | None = None) -> dict[str, object]:
    policy: dict[str, object] = {
        "scope": "global",
        "series_id": None,
        "tail_fill_threshold_ms": GLOBAL_TAIL_FILL_THRESHOLD_MS,
    }
    if profile_path is None and profile_data is None:
        return policy
    data = profile_data if profile_data is not None else json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("series profile JSON must be an object")
    preferences = data.get("subtitle_preferences", {})
    if not isinstance(preferences, dict):
        raise ValueError("series profile subtitle_preferences must be an object")
    threshold = preferences.get(
        "tail_fill_threshold_ms", GLOBAL_TAIL_FILL_THRESHOLD_MS
    )
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("tail_fill_threshold_ms must be a positive integer")
    policy.update(
        {
            "scope": "series",
            "series_id": data.get("series_id"),
            "tail_fill_threshold_ms": threshold,
        }
    )
    return policy


def validate(
    path: Path,
    as_json: bool,
    decisions_path: Path | None,
    profile_path: Path | None,
    *, profile_data: dict | None = None,
) -> int:
    cues = parse_srt(path)
    issues: list[dict[str, object]] = []
    decisions = load_decisions(decisions_path)
    style_policy = load_style_policy(profile_path, profile_data)
    tail_fill_threshold_ms = int(style_policy["tail_fill_threshold_ms"])
    allowed_terms = [
        str(term) for term in decisions.get("allowed_official_terms", [])
    ]
    protected_terms = list(
        dict.fromkeys(
            term
            for term in (
                allowed_terms
                + [str(term) for term in decisions.get("protected_terms", [])]
            )
            if term
        )
    )

    def add(level: str, cue: Cue | None, message: str) -> None:
        issues.append(
            {"level": level, "cue": cue.number if cue is not None else 0, "message": message}
        )

    episode_policy_raw = decisions.get("episode_timing_policy")
    episode_policy: dict[str, object] | None = None
    if episode_policy_raw is not None:
        if not isinstance(episode_policy_raw, dict):
            raise ValueError("episode_timing_policy must be an object")
        policy_required = {
            "policy_id",
            "scope",
            "authority",
            "instruction",
            "status",
            "max_visual_lead_frames",
        }
        if not policy_required.issubset(episode_policy_raw):
            raise ValueError(
                "episode_timing_policy requires policy_id scope authority "
                "instruction status fps_numerator fps_denominator and "
                "max_visual_lead_frames"
            )
        policy_id = str(episode_policy_raw["policy_id"]).strip()
        instruction = str(episode_policy_raw["instruction"]).strip()
        if not policy_id or not instruction:
            raise ValueError(
                "episode_timing_policy policy_id and instruction must not be empty"
            )
        scope = episode_policy_raw["scope"]
        if not isinstance(scope, dict):
            raise ValueError("episode_timing_policy scope must be an object")
        if "job_id" not in scope or "cross_episode_reuse" not in scope:
            raise ValueError(
                "episode_timing_policy scope requires job_id and cross_episode_reuse"
            )
        scope_job_id = str(scope["job_id"]).strip()
        if not scope_job_id:
            raise ValueError("episode_timing_policy scope job_id must not be empty")
        if scope["cross_episode_reuse"] is not False:
            add("error", None, "本集視覺時間政策不得跨集沿用")
        decisions_job_id = decisions.get("job_id")
        if decisions_job_id is not None and str(decisions_job_id) != scope_job_id:
            add("error", None, "本集視覺時間政策的 job_id 與 decisions 不符")
        if str(episode_policy_raw["authority"]).strip() != (
            "latest_explicit_user_instruction"
        ):
            add("error", None, "本集視覺時間政策必須來自使用者最新明確指示")
        if str(episode_policy_raw["status"]).lower().strip() != "user_confirmed":
            add("error", None, "本集視覺時間政策尚未標記為 user_confirmed")
        fps_numerator = strict_int(
            aliased_value(
                episode_policy_raw,
                "fps_numerator",
                "fps_num",
                "episode_timing_policy",
            ),
            "episode_timing_policy fps_numerator",
        )
        fps_denominator = strict_int(
            aliased_value(
                episode_policy_raw,
                "fps_denominator",
                "fps_den",
                "episode_timing_policy",
            ),
            "episode_timing_policy fps_denominator",
        )
        if fps_numerator <= 0 or fps_denominator <= 0:
            raise ValueError(
                "episode_timing_policy fps numerator and denominator must be positive"
            )
        max_visual_lead_frames = strict_int(
            episode_policy_raw["max_visual_lead_frames"],
            "episode_timing_policy max_visual_lead_frames",
        )
        if max_visual_lead_frames != 5:
            add("error", None, "本集視覺時間政策的最大提前量必須固定為 5 幀")
        sequence_head_anchor_ms: int | None = None
        if "sequence_head_anchor" in episode_policy_raw:
            sequence_head_anchor_ms = parse_timestamp(
                str(episode_policy_raw["sequence_head_anchor"])
            )
            if sequence_head_anchor_ms != 0:
                add("error", None, "片頭回貼政策只能指定序列 00:00:00,000")
        allow_hidden_visual_overlap = episode_policy_raw.get(
            "allow_hidden_visual_overlap", False
        )
        if not isinstance(allow_hidden_visual_overlap, bool):
            raise ValueError(
                "episode_timing_policy allow_hidden_visual_overlap must be a boolean"
            )
        episode_policy = {
            "policy_id": policy_id,
            "fps_numerator": fps_numerator,
            "fps_denominator": fps_denominator,
            "max_visual_lead_frames": max_visual_lead_frames,
            "sequence_head_anchor_ms": sequence_head_anchor_ms,
            "allow_hidden_visual_overlap": allow_hidden_visual_overlap,
        }

    exact_items = decisions.get("exact", [])
    if not isinstance(exact_items, list):
        raise ValueError("decisions.exact must be a list")
    source_timing_items = decisions.get("source_timing_checks", [])
    if not isinstance(source_timing_items, list):
        raise ValueError("source_timing_checks must be a list")
    xml_edit_items = decisions.get("xml_edit_checks", [])
    if not isinstance(xml_edit_items, list):
        raise ValueError("xml_edit_checks must be a list")

    decisions_root = decisions_path.parent if decisions_path is not None else None

    def evidence_path(value: object, label: str) -> Path:
        raw = str(value).strip()
        if not raw:
            raise ValueError(f"{label} must not be empty")
        path = Path(raw)
        if not path.is_absolute():
            if decisions_root is None:
                raise ValueError(f"{label} requires a decisions file path")
            direct_path = (decisions_root / path).resolve()
            if direct_path.exists():
                return direct_path
            fallback_path = job_aware_evidence_fallback(
                raw, label, decisions_root
            )
            if fallback_path is not None:
                return fallback_path
            return direct_path
        return path.resolve()

    text_stream_lock = decisions.get("visible_text_stream_lock")
    text_stream_lock_hash: str | None = None
    if text_stream_lock is not None:
        text_lock_required = {
            "reference_srt",
            "reference_srt_sha256",
            "reference_visible_text_stream_sha256",
            "candidate_visible_text_stream_sha256",
            "join_method",
            "conserved",
            "status",
        }
        if not isinstance(text_stream_lock, dict) or not text_lock_required.issubset(
            text_stream_lock
        ):
            raise ValueError(
                "visible_text_stream_lock requires reference_srt "
                "reference_srt_sha256 reference_visible_text_stream_sha256 "
                "candidate_visible_text_stream_sha256 join_method conserved and status"
            )
        reference_srt = evidence_path(
            text_stream_lock["reference_srt"], "visible text reference_srt"
        )
        reference_file_hash = sha256_file(reference_srt)
        declared_file_hash = str(text_stream_lock["reference_srt_sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", declared_file_hash):
            raise ValueError("visible text reference_srt_sha256 must be lowercase SHA-256")
        if reference_file_hash != declared_file_hash:
            add("error", None, "可見文字鎖定的參考 SRT SHA-256 不符")
        reference_cues = parse_srt(reference_srt)
        reference_stream_hash = sha256_bytes(
            visible_text_stream(reference_cues).encode("utf-8")
        )
        candidate_stream_hash = sha256_bytes(
            visible_text_stream(cues).encode("utf-8")
        )
        declared_reference_hash = str(
            text_stream_lock["reference_visible_text_stream_sha256"]
        )
        declared_candidate_hash = str(
            text_stream_lock["candidate_visible_text_stream_sha256"]
        )
        for label, value in (
            ("reference_visible_text_stream_sha256", declared_reference_hash),
            ("candidate_visible_text_stream_sha256", declared_candidate_hash),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"visible text {label} must be lowercase SHA-256")
        if declared_reference_hash != reference_stream_hash:
            add("error", None, "參考 SRT 的可見文字串 SHA-256 不符")
        if declared_candidate_hash != candidate_stream_hash:
            add("error", None, "候選 SRT 的可見文字串 SHA-256 不符")
        if candidate_stream_hash != reference_stream_hash:
            add("error", None, "候選 SRT 的全域可見文字串已改動")
        if text_stream_lock["join_method"] != (
            "concatenate_visible_cue_text_without_delimiter"
        ):
            raise ValueError("visible text stream join_method is unsupported")
        if text_stream_lock["conserved"] is not True:
            add("error", None, "可見文字串未標記為完整守恆")
        if str(text_stream_lock["status"]).lower().strip() != "locked":
            add("error", None, "可見文字串鎖定狀態必須為 locked")
        text_stream_lock_hash = candidate_stream_hash

    xml_audit_lock = decisions.get("xml_cut_audit_lock")
    formal_audit_by_id: dict[str, dict[str, object]] = {}
    formal_audit_by_frame: dict[int, dict[str, object]] = {}
    formal_audit_frames: set[int] = set()
    formal_audit_sha256: str | None = None
    if xml_audit_lock is not None:
        audit_lock_required = {
            "path",
            "sha256",
            "schema_version",
            "expected_hard_cut_count",
            "status",
        }
        if not isinstance(xml_audit_lock, dict) or not audit_lock_required.issubset(
            xml_audit_lock
        ):
            raise ValueError(
                "xml_cut_audit_lock requires path sha256 schema_version "
                "expected_hard_cut_count and status"
            )
        audit_path = evidence_path(xml_audit_lock["path"], "xml cut audit path")
        declared_audit_hash = str(xml_audit_lock["sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", declared_audit_hash):
            raise ValueError("xml cut audit sha256 must be lowercase SHA-256")
        if sha256_file(audit_path) != declared_audit_hash:
            add("error", None, "正式 XML 全剪輯點 audit SHA-256 不符")
        formal_audit_sha256 = declared_audit_hash
        audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
        if not isinstance(audit_data, dict):
            raise ValueError("formal XML cut audit must be an object")
        expected_cut_count = strict_int(
            xml_audit_lock["expected_hard_cut_count"],
            "xml cut audit expected_hard_cut_count",
        )
        if expected_cut_count != 316:
            add("error", None, "本集正式 XML audit 必須完整覆蓋 316 個 hard cuts")
        audit_records = audit_data.get("records")
        if not isinstance(audit_records, list):
            raise ValueError("formal XML cut audit records must be a list")
        for record in audit_records:
            if not isinstance(record, dict) or "cut_id" not in record or "frame" not in record:
                raise ValueError("each formal XML cut audit record requires cut_id and frame")
            cut_id = str(record["cut_id"])
            frame = strict_int(record["frame"], "formal XML cut audit frame")
            if cut_id in formal_audit_by_id or frame in formal_audit_frames:
                add("error", None, "正式 XML audit 含重複 cut_id 或 frame")
            formal_audit_by_id[cut_id] = record
            formal_audit_by_frame[frame] = record
            formal_audit_frames.add(frame)
        declared_schema = str(xml_audit_lock["schema_version"])
        if str(audit_data.get("schema_version", "")) != declared_schema:
            add("error", None, "正式 XML audit schema_version 與鎖定不符")
        if decisions.get("job_id") is not None and str(
            audit_data.get("job_id", "")
        ) != str(decisions["job_id"]):
            add("error", None, "正式 XML audit job_id 與 decisions 不符")
        for label in ("hard_cut_count", "record_count"):
            if strict_int(audit_data.get(label), f"formal XML audit {label}") != 316:
                add("error", None, f"正式 XML audit {label} 必須為 316")
        if len(audit_records) != 316 or len(formal_audit_frames) != 316:
            add("error", None, "正式 XML audit records 必須恰有 316 個唯一剪輯幀")
        if audit_data.get("all_cuts_accounted_for") is not True:
            add("error", None, "正式 XML audit 未證明 all_cuts_accounted_for")
        audit_fps_numerator = strict_int(
            audit_data.get("fps_numerator"), "formal XML audit fps_numerator"
        )
        audit_fps_denominator = strict_int(
            audit_data.get("fps_denominator"), "formal XML audit fps_denominator"
        )
        if audit_fps_numerator <= 0 or audit_fps_denominator <= 0:
            raise ValueError("formal XML audit fps must be positive")
        if episode_policy is not None and (
            audit_fps_numerator != episode_policy["fps_numerator"]
            or audit_fps_denominator != episode_policy["fps_denominator"]
        ):
            add("error", None, "正式 XML audit 幀率與本集畫面政策不符")
        if strict_int(
            audit_data.get("snap_window_frames"),
            "formal XML audit snap_window_frames",
        ) != 5:
            add("error", None, "正式 XML audit 吸附窗必須固定為 5 幀")
        if str(xml_audit_lock["status"]).lower().strip() != "locked":
            add("error", None, "正式 XML audit lock 狀態必須為 locked")
        decision_frames: list[int] = []
        for item in xml_edit_items:
            if not isinstance(item, dict) or "xml_frame" not in item:
                raise ValueError(
                    "316-complete xml_edit_checks each require an xml_frame"
                )
            decision_frames.append(
                strict_int(item["xml_frame"], "316-complete xml edit xml_frame")
            )
        if (
            len(decision_frames) != 316
            or len(set(decision_frames)) != 316
            or set(decision_frames) != formal_audit_frames
        ):
            add(
                "error",
                None,
                "decisions.xml_edit_checks 必須與正式 audit 的 316 個 hard cuts 一對一",
            )

    for index, cue in enumerate(cues):
        if cue.number != index + 1:
            add("error", cue, f"編號應為 {index + 1}")
        if cue.end_ms <= cue.start_ms:
            add("error", cue, "結束時間必須晚於開始時間")
        if len(cue.lines) != 1:
            add("error", cue, "每個字幕區塊只能有一行")
        if LINEBREAK_MARKER.search(cue.text):
            add("error", cue, "不可含有 \\N 或 <br> 等強制換行標記")
        if FONT_TAG.search(cue.text):
            add("error", cue, "不可含有字型或顏色標籤")
        punctuation_text = cue.visible_text
        for term in allowed_terms:
            punctuation_text = punctuation_text.replace(term, "")
        if FORBIDDEN_PUNCT.search(punctuation_text):
            # Decimal points and half-width colons used between digits are allowed.
            stripped = re.sub(r"(?<=\d)\.(?=\d)", "", punctuation_text)
            stripped = NUMERIC_RATIO_COLON.sub("", stripped)
            if FORBIDDEN_PUNCT.search(stripped):
                add("error", cue, "含有不允許的標點或括號")
        stripped_text = cue.visible_text.strip()
        for typo, correction in KNOWN_TYPOS.items():
            if typo in stripped_text:
                add("error", cue, f"疑似錯字 {typo!r} 應檢查為 {correction!r}")
        if STANDALONE_LAUGH.fullmatch(stripped_text):
            if cue.duration_s < 2:
                add("error", cue, "獨立笑聲未滿 2 秒不應上字幕")
            elif stripped_text != "哈哈哈哈":
                add("warning", cue, "持續 2 秒以上的獨立笑聲建議統一為 哈哈哈哈")
        elif STANDALONE_FILLER.fullmatch(stripped_text):
            if cue.duration_s < 1:
                add("error", cue, "未滿 1 秒的獨立填充音通常不應上字幕")
            else:
                add("warning", cue, "獨立反應達 1 秒可保留 請回聽確認實際發聲長度與反應作用")
        elif ATTACHED_FILLER.search(stripped_text):
            add("warning", cue, "句首或句尾有反應詞 請回聽其本身是否拉長約 1 秒 不可依文字位置直接刪除")
        if GLUED_REACTION.search(stripped_text):
            add("warning", cue, "可能有兩個話語單位被黏合 請回聽並以半形空格保留邊界")
        if MIXED_SPACING.search(cue.visible_text):
            add("warning", cue, "中文與英文或數字交界有空格 請確認它是話語邊界而非排版空格")
        for collapsed, intended in SUSPICIOUS_ENGLISH_COLLAPSE.items():
            if collapsed.lower() in stripped_text.lower():
                add("error", cue, f"英文多字詞疑似被黏合 應檢查 {intended}")
        if cue.char_count > 21:
            add("error", cue, f"字幕長度 {cue.char_count} 字超過硬上限 21")
        elif cue.char_count > 15:
            add("warning", cue, f"字幕長度 {cue.char_count} 字超過軟上限 15")
        if cue.cps > 8:
            add("warning", cue, f"閱讀速度 {cue.cps:.2f} 字/秒超過 8")

        if index + 1 < len(cues):
            next_cue = cues[index + 1]
            gap = next_cue.start_ms - cue.end_ms
            if gap < 0:
                add("error", cue, f"與下一段重疊 {-gap}ms")
            elif 0 < gap <= tail_fill_threshold_ms:
                add(
                    "warning",
                    cue,
                    f"與下一段空白 {gap}ms 在尾端延續門檻 "
                    f"{tail_fill_threshold_ms}ms 內 應延續到下一段開始",
                )
            elif gap > tail_fill_threshold_ms:
                add(
                    "warning",
                    cue,
                    f"與下一段空白 {gap}ms 超過尾端延續門檻 "
                    f"{tail_fill_threshold_ms}ms 請回聽確認保留長空白",
                )

            left_text = cue.visible_text.strip()
            right_text = next_cue.visible_text.strip()
            split_protected_term = False
            for term in protected_terms:
                for split_at in range(1, len(term)):
                    if left_text.endswith(term[:split_at]) and right_text.startswith(
                        term[split_at:]
                    ):
                        add(
                            "error",
                            cue,
                            f"受保護詞 {term!r} 被切到下一段",
                        )
                        split_protected_term = True
                        break
                if split_protected_term:
                    break

            if (
                not split_protected_term
                and left_text
                and right_text
                and left_text[-1].isascii()
                and left_text[-1].isalpha()
                and right_text[0].isascii()
                and right_text[0].isalpha()
            ):
                add(
                    "warning",
                    cue,
                    "兩段交界皆為英文字母 請確認是正常換詞而非單字被切開",
                )
            elif left_text.endswith(SUSPICIOUS_LEFT_BOUNDARIES):
                add(
                    "warning",
                    cue,
                    "切句疑似停在未完成詞組或功能詞後 "
                    "請查看前後至少 3～5 字並回聽自然斷點",
                )

    cue_by_start = {format_timestamp(cue.start_ms): cue for cue in cues}
    for item in exact_items:
        if not isinstance(item, dict) or "start" not in item or "text" not in item:
            raise ValueError("each decisions.exact item requires start and text")
        start = str(item["start"])
        expected = str(item["text"])
        cue = cue_by_start.get(start)
        if cue is None:
            add("error", None, f"已確認時間點 {start} 不存在")
        elif cue.visible_text != expected:
            add(
                "error",
                cue,
                f"已確認字幕被改動 預期 {expected!r} 實際 {cue.visible_text!r}",
            )
        if cue is not None and "end" in item:
            expected_end = parse_timestamp(str(item["end"]))
            if cue.end_ms != expected_end:
                add("error", cue, f"已確認字幕結束時間被改動 預期 {item['end']} 實際 {format_timestamp(cue.end_ms)}")
    full_text = "\n".join(cue.visible_text for cue in cues)
    for term in decisions.get("forbid", []):
        if str(term) in full_text:
            add("error", None, f"禁止詞仍存在 {term!r}")
    for term in decisions.get("require", []):
        if str(term) not in full_text:
            add("error", None, f"必要詞缺失 {term!r}")
    for item in decisions.get("speaker_checks", []):
        required = {"start", "end", "type", "result", "evidence", "status"}
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.speaker_checks item requires "
                "start end type result evidence and status"
            )
        start = parse_timestamp(str(item["start"]))
        end = parse_timestamp(str(item["end"]))
        if end <= start:
            raise ValueError("speaker check must satisfy start < end")
        status = str(item["status"]).lower()
        if status in {"pending", "unresolved", "待確認", "未處理"}:
            add(
                "error",
                None,
                f"講者警示尚未完成 {format_timestamp(start)} "
                f"{item['type']!r}",
            )
        elif status not in {"resolved", "confirmed", "reviewed", "已確認", "已處理"}:
            raise ValueError(
                "speaker check status must be resolved confirmed reviewed 已確認 or 已處理"
            )
    speaker_coverage_required = decisions.get("speaker_coverage_required", False)
    if not isinstance(speaker_coverage_required, bool):
        raise ValueError("speaker_coverage_required must be a boolean")
    visible_cue_speakers = decisions.get("visible_cue_speakers", [])
    if not isinstance(visible_cue_speakers, list):
        raise ValueError("visible_cue_speakers must be a list")
    covered_starts: set[str] = set()
    for item in visible_cue_speakers:
        required = {
            "start",
            "text",
            "speaker",
            "source_track",
            "speaker_count",
            "status",
            "evidence",
        }
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.visible_cue_speakers item requires start text "
                "speaker source_track speaker_count status and evidence"
            )
        start = str(item["start"])
        cue = cue_by_start.get(start)
        speaker = str(item["speaker"]).strip()
        source_track = str(item["source_track"]).strip()
        evidence = str(item["evidence"]).strip()
        status = str(item["status"]).lower().strip()
        try:
            speaker_count = int(item["speaker_count"])
        except (TypeError, ValueError) as error:
            raise ValueError("visible cue speaker_count must be an integer") from error
        if start in covered_starts:
            add("error", cue, f"同一字幕起點 {start} 有重複講者覆蓋紀錄")
        covered_starts.add(start)
        if cue is None:
            add("error", None, f"講者覆蓋起點 {start} 不是任何字幕的起點")
        elif cue.visible_text != str(item["text"]):
            add(
                "error",
                cue,
                f"講者覆蓋字幕不符 預期 {item['text']!r} "
                f"實際 {cue.visible_text!r}",
            )
        if not speaker or not source_track or not evidence:
            raise ValueError(
                "visible cue speaker source_track and evidence must not be empty"
            )
        if speaker_count != 1:
            add(
                "error",
                cue,
                f"單一字幕區塊只能有一位講者 目前記錄為 {speaker_count} 位",
            )
        if status in {"pending", "unresolved", "待確認", "未處理"}:
            add("error", cue, f"字幕講者尚未確認 {start}")
        elif status not in {"resolved", "confirmed", "reviewed", "已確認", "已處理"}:
            raise ValueError(
                "visible cue speaker status must be resolved confirmed reviewed "
                "已確認 or 已處理"
            )
    if speaker_coverage_required:
        for cue in cues:
            start = format_timestamp(cue.start_ms)
            if start not in covered_starts:
                add("error", cue, "收到同步分軌但此字幕缺少唯一講者覆蓋")
    visible_speaker_by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for item in visible_cue_speakers:
        if isinstance(item, dict):
            visible_speaker_by_key.setdefault(
                (str(item.get("start", "")), str(item.get("text", ""))), []
            ).append(item)

    custom_source_records: list[dict[str, object]] = []
    reconciled_source_records: list[dict[str, object]] = []
    for item in source_timing_items:
        required = {
            "subtitle_start",
            "cue_text",
            "speaker",
            "source_track",
            "timing_authority",
            "waveform_onset",
            "other_tracks_role",
            "mode",
            "status",
            "evidence",
        }
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.source_timing_checks item requires subtitle_start "
                "cue_text speaker source_track timing_authority waveform_onset "
                "other_tracks_role mode status and evidence"
            )
        subtitle_start = parse_timestamp(str(item["subtitle_start"]))
        waveform_onset = parse_timestamp(str(item["waveform_onset"]))
        cue_text = str(item["cue_text"])
        speaker = str(item["speaker"]).strip()
        source_track = str(item["source_track"]).strip()
        timing_authority = str(item["timing_authority"]).strip()
        other_tracks_role = str(item["other_tracks_role"]).lower().strip()
        mode = str(item["mode"]).lower().strip()
        status = str(item["status"]).lower().strip()
        evidence = str(item["evidence"]).strip()
        if not speaker or not source_track or not timing_authority or not evidence:
            raise ValueError(
                "source timing check speaker source_track timing_authority "
                "and evidence must not be empty"
            )
        if status in {"pending", "unresolved", "待確認", "未處理"}:
            add(
                "error",
                None,
                f"主聲源時間校正尚未完成 {format_timestamp(subtitle_start)}",
            )
        elif mode == USER_VISUAL_ANCHOR_MODE and status not in {
            "confirmed",
            "user_confirmed",
            "已確認",
        }:
            raise ValueError(
                "user_confirmed_visual_anchor source timing status must be "
                "confirmed user_confirmed or 已確認"
            )
        elif mode != USER_VISUAL_ANCHOR_MODE and status not in {
            "resolved",
            "confirmed",
            "reviewed",
            "已確認",
            "已處理",
        }:
            raise ValueError(
                "source timing check status must be resolved confirmed reviewed "
                "已確認 or 已處理"
            )
        cue_at_start = cue_by_start.get(format_timestamp(subtitle_start))
        if cue_at_start is None:
            add(
                "error",
                None,
                f"主聲源校正起點 {format_timestamp(subtitle_start)} "
                "不是任何字幕的起點",
            )
        elif cue_at_start.visible_text != cue_text:
            add(
                "error",
                cue_at_start,
                f"主聲源校正字幕不符 預期 {cue_text!r} "
                f"實際 {cue_at_start.visible_text!r}",
            )
        if mode in {
            "source_track",
            "source_track_xml",
            LOCKED_PREFIX_WAVEFORM_XML_MODE,
        }:
            if episode_policy is not None and mode == "source_track_xml":
                timing_policy_id = item.get("timing_policy_id")
                if timing_authority == episode_policy["policy_id"] or timing_policy_id not in {
                    None,
                    "",
                }:
                    add(
                        "error",
                        cue_at_start,
                        "本集 user-confirmed 畫面例外不得偽裝為標準 source_track_xml",
                    )
                if xml_audit_lock is None:
                    add(
                        "error",
                        cue_at_start,
                        "與本集畫面例外並存的標準 source_track_xml 必須由正式全剪輯點 audit 鎖定",
                    )
            if timing_authority != source_track:
                add(
                    "error",
                    cue_at_start,
                    "字幕時間權威必須等於已鎖定的主講者音軌",
                )
            if other_tracks_role != "speaker_overlap_only":
                add(
                    "error",
                    cue_at_start,
                    "其他音軌只能作為講者 串音 重疊與換人證據 "
                    "不得參與字幕起點計算",
                )
            if mode == "source_track" and abs(subtitle_start - waveform_onset) > 1:
                add(
                    "error",
                    cue_at_start,
                    "字幕起點未對齊主講者音軌第一個保留字的波形頭",
                )
            if mode == "source_track_xml":
                xml_required = {"xml_cut", "fps", "max_snap_frames"}
                if not xml_required.issubset(item):
                    raise ValueError(
                        "source_track_xml timing check requires xml_cut fps "
                        "and max_snap_frames"
                    )
                xml_cut = parse_timestamp(str(item["xml_cut"]))
                fps = float(item["fps"])
                max_snap_frames = int(item["max_snap_frames"])
                if fps <= 0:
                    raise ValueError(
                        "source_track_xml timing check fps must be positive"
                    )
                if max_snap_frames != 5:
                    add(
                        "error",
                        cue_at_start,
                        "主聲源與 XML 的吸附上限必須固定為前後 5 幀",
                    )
                if abs(subtitle_start - xml_cut) > 1:
                    add(
                        "error",
                        cue_at_start,
                        "主聲源 XML 模式的字幕起點必須精確對齊剪輯點",
                    )
                exact_frame_fields = {
                    "xml_frame",
                    "fps_numerator",
                    "fps_denominator",
                    "refined_onset_frame",
                    "refined_onset_delta_frames",
                    "serialized_cut_roundtrip",
                    "serialized_cut_timestamp",
                }
                if formal_audit_sha256 is not None:
                    if not exact_frame_fields.issubset(item):
                        raise ValueError(
                            "audit-locked source_track_xml requires exact nominal-frame "
                            "and ceil-ms roundtrip evidence"
                        )
                    fps_numerator = strict_int(
                        item["fps_numerator"], "source_track_xml fps_numerator"
                    )
                    fps_denominator = strict_int(
                        item["fps_denominator"], "source_track_xml fps_denominator"
                    )
                    xml_frame = strict_int(
                        item["xml_frame"], "source_track_xml xml_frame"
                    )
                    refined_frame = nominal_frame(
                        waveform_onset, fps_numerator, fps_denominator
                    )
                    declared_refined_frame = strict_int(
                        item["refined_onset_frame"],
                        "source_track_xml refined_onset_frame",
                    )
                    declared_delta = strict_int(
                        item["refined_onset_delta_frames"],
                        "source_track_xml refined_onset_delta_frames",
                    )
                    if (
                        item["serialized_cut_roundtrip"] is not True
                        or str(item["serialized_cut_timestamp"])
                        != format_timestamp(xml_cut)
                        or xml_cut
                        != serialized_frame_timestamp_ms(
                            xml_frame, fps_numerator, fps_denominator
                        )
                        or nominal_frame(
                            xml_cut, fps_numerator, fps_denominator
                        )
                        != xml_frame
                    ):
                        add("error", cue_at_start, "標準 source_track_xml 未通過 ceil-ms round-trip")
                    if declared_refined_frame != refined_frame:
                        add("error", cue_at_start, "標準 source_track_xml refined frame 不符")
                    if declared_delta != refined_frame - xml_frame:
                        add("error", cue_at_start, "標準 source_track_xml nominal frame delta 不符")
                    if not 0 <= refined_frame - xml_frame <= 5:
                        add(
                            "error",
                            cue_at_start,
                            "標準 source_track_xml 主軌開口必須位於 cut 後 0 至 5 幀",
                        )
                    if str(item.get("full_cut_audit_sha256", "")) != (
                        formal_audit_sha256
                    ):
                        add("error", cue_at_start, "標準 source_track_xml audit SHA-256 不符")
                    audit_record = formal_audit_by_frame.get(xml_frame)
                    selected = (
                        audit_record.get("selected_candidate")
                        if isinstance(audit_record, dict)
                        else None
                    )
                    if not isinstance(selected, dict) or selected.get("accepted") is not True:
                        add("error", cue_at_start, "正式 audit 未採用此標準 source_track_xml")
                    elif (
                        selected.get("speaker") != speaker
                        or selected.get("source_track") != source_track
                        or selected.get("roundtrip_safe_srt_timestamp")
                        not in {None, format_timestamp(xml_cut)}
                    ):
                        add("error", cue_at_start, "標準 source_track_xml speaker/source/cut 與 audit 不符")
                    if item.get("post_snap_audio_check") != "passed":
                        add("error", cue_at_start, "標準 source_track_xml post-snap 音訊回查未通過")
                    if item.get("hidden_event_conflict") is not False or item.get(
                        "hidden_event_ids"
                    ) not in (None, []):
                        add("error", cue_at_start, "標準 source_track_xml hidden truth 不安全")
                    if item.get("speaker_boundary_preserved") is not True:
                        add("error", cue_at_start, "標準 source_track_xml 未保留講者邊界")
                else:
                    max_delta_ms = max_snap_frames * 1000 / fps
                    onset_delta_ms = abs(waveform_onset - xml_cut)
                    if onset_delta_ms > max_delta_ms + 1:
                        add(
                            "error",
                            cue_at_start,
                            f"主講者波形頭距離 XML 剪輯點 {onset_delta_ms:.0f}ms "
                            f"超過 5 幀上限 {max_delta_ms:.1f}ms",
                        )
            elif mode == LOCKED_PREFIX_WAVEFORM_XML_MODE:
                reconciled_required = {
                    "xml_cut",
                    "xml_frame",
                    "fps",
                    "fps_numerator",
                    "fps_denominator",
                    "max_snap_frames",
                    "refined_onset_frame",
                    "cut_to_refined_onset_frames",
                    "serialized_cut_roundtrip_passed",
                    "reconciliation_method",
                    "raw_alignment_mismatch",
                    "raw_alignment_mismatch_present",
                    "raw_alignment_mismatch_sha256",
                    "raw_alignment_mismatch_preserved",
                    "raw_token_onset",
                    "locked_visible_prefix",
                    "locked_visible_prefix_sha256",
                    "locked_visible_text_stream_sha256",
                    "formal_audit_cut_id",
                    "formal_audit_record_sha256",
                    "outgoing_speech_end",
                    "outgoing_speaker",
                    "outgoing_source_track",
                    "cross_speaker",
                    "timing_source_role",
                    "outgoing_track_used_for_timing",
                    "source_track_dominance_passed",
                    "speaker_boundary_preserved",
                }
                if not reconciled_required.issubset(item):
                    raise ValueError(
                        f"{LOCKED_PREFIX_WAVEFORM_XML_MODE} source timing check "
                        "requires exact-frame refined-onset raw-mismatch text-lock "
                        "formal-audit and incoming-source evidence"
                    )
                xml_cut = parse_timestamp(str(item["xml_cut"]))
                xml_frame = strict_int(
                    item["xml_frame"], "reconciled source timing xml_frame"
                )
                fps_numerator = strict_int(
                    item["fps_numerator"],
                    "reconciled source timing fps_numerator",
                )
                fps_denominator = strict_int(
                    item["fps_denominator"],
                    "reconciled source timing fps_denominator",
                )
                if fps_numerator <= 0 or fps_denominator <= 0:
                    raise ValueError("reconciled source timing fps must be positive")
                fps = float(item["fps"])
                if abs(fps - fps_numerator / fps_denominator) > 1e-9:
                    add("error", cue_at_start, "refined onset 的 fps 摘要與精確幀率不符")
                max_snap_frames = strict_int(
                    item["max_snap_frames"],
                    "reconciled source timing max_snap_frames",
                )
                if max_snap_frames != 5:
                    add("error", cue_at_start, "refined onset XML 吸附窗必須固定為 5 幀")
                if subtitle_start != xml_cut:
                    add("error", cue_at_start, "refined onset 字幕起點必須等於 XML cut")
                expected_cut_ms = serialized_frame_timestamp_ms(
                    xml_frame, fps_numerator, fps_denominator
                )
                if (
                    item["serialized_cut_roundtrip_passed"] is not True
                    or xml_cut != expected_cut_ms
                    or nominal_frame(
                        xml_cut, fps_numerator, fps_denominator
                    )
                    != xml_frame
                ):
                    add(
                        "error",
                        cue_at_start,
                        "refined onset XML cut 未通過 ceil-ms nominal-frame round-trip",
                    )
                refined_onset_frame = strict_int(
                    item["refined_onset_frame"],
                    "reconciled source timing refined_onset_frame",
                )
                calculated_onset_frame = nominal_frame(
                    waveform_onset, fps_numerator, fps_denominator
                )
                frame_delta = calculated_onset_frame - xml_frame
                declared_frame_delta = strict_int(
                    item["cut_to_refined_onset_frames"],
                    "reconciled source timing cut_to_refined_onset_frames",
                )
                if refined_onset_frame != calculated_onset_frame:
                    add("error", cue_at_start, "refined onset frame 計算不符")
                if declared_frame_delta != frame_delta:
                    add("error", cue_at_start, "cut 到 refined onset 的 nominal frame 差不符")
                if not 0 <= frame_delta <= 5:
                    add(
                        "error",
                        cue_at_start,
                        "refined onset 必須位於 XML cut 後 0 至 5 個 nominal frames",
                    )
                if text_stream_lock_hash is None:
                    add("error", cue_at_start, "refined onset 模式缺少全域可見文字串鎖定")
                elif str(item["locked_visible_text_stream_sha256"]) != (
                    text_stream_lock_hash
                ):
                    add("error", cue_at_start, "refined onset 的全域可見文字串鎖定不符")
                mismatch = item["raw_alignment_mismatch"]
                if mismatch is not None and not isinstance(mismatch, dict):
                    raise ValueError("raw_alignment_mismatch must be an object or null")
                mismatch_present = item["raw_alignment_mismatch_present"]
                if not isinstance(mismatch_present, bool):
                    raise ValueError("raw_alignment_mismatch_present must be a boolean")
                if mismatch_present != (mismatch is not None):
                    add("error", cue_at_start, "raw alignment mismatch presence 指標不符")
                if item["raw_alignment_mismatch_preserved"] is not True:
                    add("error", cue_at_start, "raw alignment mismatch 未完整保留")
                mismatch_hash = str(item["raw_alignment_mismatch_sha256"])
                if mismatch_hash != canonical_json_sha256(mismatch):
                    add("error", cue_at_start, "raw alignment mismatch SHA-256 不符")
                raw_token_onset = parse_timestamp(str(item["raw_token_onset"]))
                if raw_token_onset < waveform_onset - 1:
                    add("error", cue_at_start, "raw token onset 不得早於 refined waveform onset")
                locked_prefix = str(item["locked_visible_prefix"])
                if not locked_prefix or not cue_text.startswith(locked_prefix):
                    add("error", cue_at_start, "locked visible prefix 不是候選字幕句首")
                if str(item["locked_visible_prefix_sha256"]) != sha256_bytes(
                    locked_prefix.encode("utf-8")
                ):
                    add("error", cue_at_start, "locked visible prefix SHA-256 不符")
                if not str(item["reconciliation_method"]).strip():
                    raise ValueError("reconciliation_method must not be empty")
                if str(item["timing_source_role"]) != (
                    "incoming_locked_source_track_only"
                ):
                    add("error", cue_at_start, "refined onset 只能取自 incoming locked source track")
                if item["outgoing_track_used_for_timing"] is not False:
                    add("error", cue_at_start, "不得使用 outgoing 或其他軌決定 refined onset")
                outgoing_speech_end = parse_timestamp(str(item["outgoing_speech_end"]))
                if outgoing_speech_end > xml_cut + 1:
                    add("error", cue_at_start, "outgoing speaker 尚未在 XML cut 前說完")
                if not isinstance(item["cross_speaker"], bool):
                    raise ValueError("cross_speaker must be a boolean")
                if item["source_track_dominance_passed"] is not True:
                    add("error", cue_at_start, "incoming locked source track 主聲源檢查未通過")
                if item["speaker_boundary_preserved"] is not True:
                    add("error", cue_at_start, "refined onset 未保留講者邊界")
                if xml_audit_lock is None:
                    add("error", cue_at_start, "refined onset 模式缺少正式 316-cut audit lock")
                audit_cut_id = str(item["formal_audit_cut_id"])
                audit_record = formal_audit_by_id.get(audit_cut_id)
                if audit_record is None:
                    add("error", cue_at_start, "refined onset 找不到正式 audit cut record")
                else:
                    if str(item["formal_audit_record_sha256"]) != (
                        canonical_json_sha256(audit_record)
                    ):
                        add("error", cue_at_start, "正式 audit cut record SHA-256 不符")
                    if strict_int(
                        audit_record.get("frame"), "formal audit reconciled frame"
                    ) != xml_frame:
                        add("error", cue_at_start, "refined onset 的 audit frame 不符")
                    if audit_record.get("disposition") != "adopt":
                        add("error", cue_at_start, "refined onset audit record 並非 adopt")
                    selected = audit_record.get("selected_candidate")
                    if not isinstance(selected, dict) or selected.get("accepted") is not True:
                        add("error", cue_at_start, "refined onset audit 未選定安全候選")
                    else:
                        prefix_evidence = selected.get("prefix_reconciliation")
                        raw_gap = selected.get("raw_gap")
                        if not isinstance(prefix_evidence, dict) or not isinstance(
                            raw_gap, dict
                        ):
                            add("error", cue_at_start, "refined onset audit 缺少 prefix/raw-gap 證據")
                        else:
                            if prefix_evidence.get("selected_onset") != format_timestamp(
                                waveform_onset
                            ):
                                add("error", cue_at_start, "refined waveform onset 與 audit 不符")
                            if prefix_evidence.get("prefix_mismatch") != mismatch:
                                add("error", cue_at_start, "保留的 raw mismatch 與 audit 不符")
                            first_token = prefix_evidence.get("first_retained_token")
                            if not isinstance(first_token, dict) or first_token.get(
                                "start"
                            ) != format_timestamp(raw_token_onset):
                                add("error", cue_at_start, "raw retained token onset 與 audit 不符")
                            if str(item["reconciliation_method"]) != str(
                                prefix_evidence.get("selection_mode", "")
                            ):
                                add("error", cue_at_start, "reconciliation method 與 audit 不符")
                            dominance = prefix_evidence.get("dominance")
                            if not isinstance(dominance, dict) or dominance.get(
                                "passed"
                            ) is not True:
                                add("error", cue_at_start, "audit 未證明 incoming source dominance")
                        if isinstance(raw_gap, dict) and raw_gap.get(
                            "previous_speech_end"
                        ) != format_timestamp(outgoing_speech_end):
                            add("error", cue_at_start, "outgoing speech end 與 audit 不符")
                        if (
                            selected.get("proposed_cut_frame") != xml_frame
                            or selected.get("proposed_srt_start")
                            != format_timestamp(xml_cut)
                            or selected.get("speaker") != speaker
                            or selected.get("source_track") != source_track
                        ):
                            add("error", cue_at_start, "refined onset 的 cut/speaker/source 與 audit 不符")
                        post_check = selected.get("post_check")
                        if not isinstance(post_check, dict) or any(
                            post_check.get(key) is not True
                            for key in (
                                "onset_within_0_to_5_frames",
                                "hidden_clear",
                                "protected_clear",
                                "speaker_boundary_preserved",
                                "following_timing_authority_is_own_locked_source",
                            )
                        ):
                            add("error", cue_at_start, "refined onset audit post-check 未全數通過")
                speaker_matches = visible_speaker_by_key.get(
                    (format_timestamp(subtitle_start), cue_text), []
                )
                if len(speaker_matches) != 1:
                    add("error", cue_at_start, "refined onset 缺少唯一 incoming 講者覆蓋")
                elif (
                    str(speaker_matches[0].get("speaker", "")) != speaker
                    or str(speaker_matches[0].get("source_track", ""))
                    != source_track
                    or speaker_matches[0].get("speaker_count") != 1
                ):
                    add("error", cue_at_start, "refined onset 的 incoming speaker/source lock 不符")
                if cue_at_start is None or cue_at_start.number <= 1:
                    add("error", cue_at_start, "refined onset XML cut 缺少 outgoing 字幕")
                else:
                    previous_cue = cues[cue_at_start.number - 2]
                    previous_matches = visible_speaker_by_key.get(
                        (
                            format_timestamp(previous_cue.start_ms),
                            previous_cue.visible_text,
                        ),
                        [],
                    )
                    outgoing_speaker = str(item["outgoing_speaker"])
                    outgoing_source_track = str(item["outgoing_source_track"])
                    if len(previous_matches) != 1:
                        add("error", cue_at_start, "refined onset 缺少唯一 outgoing 講者覆蓋")
                    elif (
                        str(previous_matches[0].get("speaker", ""))
                        != outgoing_speaker
                        or str(previous_matches[0].get("source_track", ""))
                        != outgoing_source_track
                    ):
                        add("error", cue_at_start, "refined onset 的 outgoing speaker/source lock 不符")
                    calculated_cross_speaker = (
                        outgoing_speaker != speaker
                        or outgoing_source_track != source_track
                    )
                    if item["cross_speaker"] != calculated_cross_speaker:
                        add("error", cue_at_start, "cross_speaker 指標與兩側 speaker/source 不符")
                reconciled_source_records.append(
                    {
                        "item": item,
                        "cue": cue_at_start,
                        "subtitle_start": subtitle_start,
                        "cue_text": cue_text,
                        "speaker": speaker,
                        "source_track": source_track,
                        "waveform_onset": waveform_onset,
                        "xml_cut": xml_cut,
                        "xml_frame": xml_frame,
                        "raw_alignment_mismatch": mismatch,
                        "raw_token_onset": raw_token_onset,
                        "formal_audit_cut_id": audit_cut_id,
                        "cross_speaker": item["cross_speaker"],
                        "xml_match_count": 0,
                    }
                )
        elif mode == "fallback_after_review":
            if "reason" not in item or not str(item["reason"]).strip():
                raise ValueError(
                    "fallback_after_review source timing check requires reason"
                )
            if episode_policy is not None and (
                timing_authority == episode_policy["policy_id"]
                or "timing_policy_id" in item
                or "policy_id" in item
            ):
                add(
                    "error",
                    cue_at_start,
                    "使用者確認的畫面錨點不得偽裝為 fallback_after_review",
                )
        elif mode == USER_VISUAL_ANCHOR_MODE:
            custom_required = {
                "anchor_kind",
                "visual_anchor",
                "xml_frame",
                "max_visual_lead_frames",
                "reference_boundary",
                "reference_frame",
                "visual_lead_frames",
                "pre_anchor_subtitle_start",
                "pre_anchor_display_frame",
                "source_track_role",
                "allow_hidden_visual_overlap",
            }
            if not custom_required.issubset(item):
                raise ValueError(
                    "user_confirmed_visual_anchor source timing check requires "
                    "timing_policy_id anchor_kind visual_anchor xml_frame "
                    "fps_numerator fps_denominator max_visual_lead_frames "
                    "reference_boundary reference_frame visual_lead_frames "
                    "pre_anchor_subtitle_start pre_anchor_display_frame "
                    "source_track_role and allow_hidden_visual_overlap"
                )
            timing_policy_id = str(
                aliased_value(
                    item,
                    "timing_policy_id",
                    "policy_id",
                    "user_confirmed_visual_anchor source timing check",
                )
            ).strip()
            if not timing_policy_id:
                raise ValueError("custom source timing policy id must not be empty")
            record_fps_numerator = strict_int(
                aliased_value(
                    item,
                    "fps_numerator",
                    "fps_num",
                    "user_confirmed_visual_anchor source timing check",
                ),
                "custom source timing fps_numerator",
            )
            record_fps_denominator = strict_int(
                aliased_value(
                    item,
                    "fps_denominator",
                    "fps_den",
                    "user_confirmed_visual_anchor source timing check",
                ),
                "custom source timing fps_denominator",
            )
            if record_fps_numerator <= 0 or record_fps_denominator <= 0:
                raise ValueError("custom source timing fps must be positive")
            anchor_kind = str(item["anchor_kind"]).lower().strip()
            if anchor_kind not in {"xml_hard_cut", "sequence_zero"}:
                raise ValueError(
                    "custom source timing anchor_kind must be xml_hard_cut or sequence_zero"
                )
            visual_anchor = parse_timestamp(str(item["visual_anchor"]))
            xml_frame = strict_int(item["xml_frame"], "custom source timing xml_frame")
            reference_boundary = parse_timestamp(str(item["reference_boundary"]))
            reference_frame = strict_int(
                item["reference_frame"], "custom source timing reference_frame"
            )
            visual_lead_frames = strict_int(
                item["visual_lead_frames"],
                "custom source timing visual_lead_frames",
            )
            record_max_frames = strict_int(
                item["max_visual_lead_frames"],
                "custom source timing max_visual_lead_frames",
            )
            pre_anchor_start = parse_timestamp(
                str(item["pre_anchor_subtitle_start"])
            )
            pre_anchor_display_frame = strict_int(
                item["pre_anchor_display_frame"],
                "custom source timing pre_anchor_display_frame",
            )
            allow_hidden_overlap = item["allow_hidden_visual_overlap"]
            if not isinstance(allow_hidden_overlap, bool):
                raise ValueError(
                    "custom source timing allow_hidden_visual_overlap must be a boolean"
                )
            if episode_policy is None:
                add(
                    "error",
                    cue_at_start,
                    "user_confirmed_visual_anchor 缺少 episode_timing_policy",
                )
            else:
                if timing_policy_id != episode_policy["policy_id"]:
                    add("error", cue_at_start, "畫面錨點的 timing_policy_id 不符")
                if (
                    record_fps_numerator != episode_policy["fps_numerator"]
                    or record_fps_denominator != episode_policy["fps_denominator"]
                ):
                    add("error", cue_at_start, "畫面錨點幀率與本集政策不符")
                if record_max_frames != episode_policy["max_visual_lead_frames"]:
                    add("error", cue_at_start, "畫面錨點提前幀數上限與本集政策不符")
            if timing_authority != timing_policy_id:
                add(
                    "error",
                    cue_at_start,
                    "使用者畫面錨點的 timing_authority 必須等於 timing_policy_id",
                )
            if other_tracks_role != "speaker_overlap_only":
                add(
                    "error",
                    cue_at_start,
                    "其他音軌只能作為講者 串音 重疊與換人證據",
                )
            if str(item["source_track_role"]).strip() != (
                "speaker_and_waveform_evidence_not_display_timing_authority"
            ):
                add(
                    "error",
                    cue_at_start,
                    "custom 模式仍須明列主聲源只作講者與真實開口證據",
                )
            if subtitle_start != visual_anchor:
                add("error", cue_at_start, "字幕起點必須等於使用者確認的畫面錨點")
            anchor_frame = nominal_frame(
                visual_anchor, record_fps_numerator, record_fps_denominator
            )
            calculated_reference_frame = nominal_frame(
                waveform_onset, record_fps_numerator, record_fps_denominator
            )
            calculated_pre_anchor_frame = nominal_frame(
                pre_anchor_start, record_fps_numerator, record_fps_denominator
            )
            calculated_lead_frames = calculated_reference_frame - anchor_frame
            if anchor_frame != xml_frame:
                add(
                    "error",
                    cue_at_start,
                    "畫面錨點 SRT 毫秒反算後未落在指定 xml_frame",
                )
            if reference_boundary != waveform_onset:
                add(
                    "error",
                    cue_at_start,
                    "reference_boundary 必須保留真實 waveform_onset",
                )
            if reference_frame != calculated_reference_frame:
                add("error", cue_at_start, "reference_frame 與真實開口幀不符")
            if pre_anchor_display_frame != calculated_pre_anchor_frame:
                add("error", cue_at_start, "pre_anchor_display_frame 計算不符")
            if visual_lead_frames != calculated_lead_frames:
                add("error", cue_at_start, "visual_lead_frames 計算不符")
            if not 0 <= calculated_lead_frames <= 5:
                add(
                    "error",
                    cue_at_start,
                    "畫面錨點只能比真實開口早 0 至 5 個 nominal frames",
                )
            if record_max_frames != 5:
                add("error", cue_at_start, "畫面錨點最大提前量必須固定為 5 幀")
            if anchor_kind == "sequence_zero":
                if (
                    subtitle_start != 0
                    or visual_anchor != 0
                    or xml_frame != 0
                    or cue_at_start is None
                    or cue_at_start.number != 1
                ):
                    add(
                        "error",
                        cue_at_start,
                        "sequence_zero 只能套用 cue 1 且起點必須為 00:00:00,000",
                    )
                if (
                    episode_policy is None
                    or episode_policy["sequence_head_anchor_ms"] != 0
                ):
                    add("error", cue_at_start, "sequence_zero 缺少本集片頭回貼授權")
            speaker_matches = visible_speaker_by_key.get(
                (format_timestamp(subtitle_start), cue_text), []
            )
            if visible_cue_speakers:
                if len(speaker_matches) != 1:
                    add("error", cue_at_start, "custom 畫面錨點缺少唯一講者覆蓋")
                elif (
                    str(speaker_matches[0].get("speaker", "")) != speaker
                    or str(speaker_matches[0].get("source_track", ""))
                    != source_track
                ):
                    add("error", cue_at_start, "custom 畫面錨點的講者或主聲源不符")
            custom_source_records.append(
                {
                    "item": item,
                    "cue": cue_at_start,
                    "subtitle_start": subtitle_start,
                    "cue_text": cue_text,
                    "speaker": speaker,
                    "source_track": source_track,
                    "waveform_onset": waveform_onset,
                    "timing_policy_id": timing_policy_id,
                    "anchor_kind": anchor_kind,
                    "xml_frame": xml_frame,
                    "reference_frame": reference_frame,
                    "visual_lead_frames": visual_lead_frames,
                    "pre_anchor_start": pre_anchor_start,
                    "allow_hidden_visual_overlap": allow_hidden_overlap,
                    "hidden_overlap_ids": [],
                    "xml_match_count": 0,
                }
            )
        else:
            raise ValueError(
                "source timing check mode must be source_track "
                "source_track_xml fallback_after_review or "
                "user_confirmed_visual_anchor or "
                f"{LOCKED_PREFIX_WAVEFORM_XML_MODE}"
            )

    custom_exact_locks: list[dict[str, object]] = []
    for item in exact_items:
        if not isinstance(item, dict) or item.get("lock_kind") != USER_VISUAL_ANCHOR_MODE:
            continue
        custom_lock_required = {
            "start",
            "text",
            "timing_policy_id",
            "anchor_kind",
            "xml_frame",
            "status",
        }
        if not custom_lock_required.issubset(item):
            raise ValueError(
                "user_confirmed_visual_anchor exact lock requires start text "
                "timing_policy_id anchor_kind xml_frame and status"
            )
        if str(item["status"]).lower().strip() != "user_confirmed":
            add("error", cue_by_start.get(str(item["start"])), "視覺錨點 exact lock 尚未確認")
        if episode_policy is None or str(item["timing_policy_id"]) != str(
            episode_policy["policy_id"] if episode_policy is not None else ""
        ):
            add("error", cue_by_start.get(str(item["start"])), "視覺錨點 exact lock 政策不符")
        strict_int(item["xml_frame"], "visual anchor exact lock xml_frame")
        custom_exact_locks.append(item)

    for record in custom_source_records:
        matches = [
            lock
            for lock in custom_exact_locks
            if str(lock["start"]) == format_timestamp(int(record["subtitle_start"]))
            and str(lock["text"]) == record["cue_text"]
            and str(lock["timing_policy_id"]) == record["timing_policy_id"]
            and str(lock["anchor_kind"]).lower().strip() == record["anchor_kind"]
            and lock["xml_frame"] == record["xml_frame"]
        ]
        if len(matches) != 1:
            add(
                "error",
                record["cue"] if isinstance(record["cue"], Cue) else None,
                "每個使用者畫面錨點必須恰有一筆對應 exact lock",
            )
    for lock in custom_exact_locks:
        matches = [
            record
            for record in custom_source_records
            if str(lock["start"]) == format_timestamp(int(record["subtitle_start"]))
            and str(lock["text"]) == record["cue_text"]
            and str(lock["timing_policy_id"]) == record["timing_policy_id"]
            and str(lock["anchor_kind"]).lower().strip() == record["anchor_kind"]
            and lock["xml_frame"] == record["xml_frame"]
        ]
        if len(matches) != 1:
            add(
                "error",
                cue_by_start.get(str(lock["start"])),
                "每個視覺錨點 exact lock 必須恰有一筆 custom source timing record",
            )

    sequence_zero_records = [
        record
        for record in custom_source_records
        if record["anchor_kind"] == "sequence_zero"
    ]
    if episode_policy is not None and episode_policy["sequence_head_anchor_ms"] == 0:
        if len(sequence_zero_records) != 1:
            add("error", None, "片頭回貼政策必須恰有一筆 sequence_zero timing record")
    elif sequence_zero_records:
        add("error", None, "未授權的 sequence_zero timing record")

    hidden_items = decisions.get("hidden_events", [])
    if not isinstance(hidden_items, list):
        raise ValueError("hidden_events must be a list")
    hidden_rebind_items = decisions.get("hidden_event_visual_rebinds", [])
    if not isinstance(hidden_rebind_items, list):
        raise ValueError("hidden_event_visual_rebinds must be a list")
    used_hidden_rebind_indexes: set[int] = set()
    policy_allows_hidden_overlap = bool(
        episode_policy is not None
        and episode_policy["allow_hidden_visual_overlap"] is True
    )
    for item in hidden_items:
        required = {"start", "end", "next_visible_start", "next_text"}
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.hidden_events item requires "
                "start end next_visible_start and next_text"
            )
        hidden_start = parse_timestamp(str(item["start"]))
        hidden_end = parse_timestamp(str(item["end"]))
        audio_start = parse_timestamp(str(item.get("audio_start", item["start"])))
        audio_end = parse_timestamp(str(item.get("audio_end", item["end"])))
        next_visible_start = parse_timestamp(str(item["next_visible_start"]))
        next_text = str(item["next_text"])
        if not hidden_start < hidden_end <= next_visible_start:
            raise ValueError(
                "hidden event must satisfy start < end <= next_visible_start"
            )
        if not audio_start < audio_end:
            raise ValueError("hidden event audio interval must satisfy audio_start < audio_end")

        matching_records = [
            record
            for record in custom_source_records
            if record["waveform_onset"] == next_visible_start
            and record["cue_text"] == next_text
        ]
        hidden_label = str(
            item.get(
                "id",
                f"{format_timestamp(audio_start)}-{format_timestamp(audio_end)}",
            )
        )
        matching_rebinds: list[tuple[int, dict[str, object], dict[str, object]]] = []
        for rebind_index, rebind in enumerate(hidden_rebind_items):
            rebind_required = {
                "hidden_event_id",
                "audio_next_visible_start",
                "visual_anchor_start",
                "next_text",
                "visual_anchor_inside_hidden_interval",
                "timing_policy_id",
                "visual_lead_interval_start",
                "visual_lead_interval_end",
                "visual_lead_interval_overlaps_hidden_audio",
                "allow_hidden_visual_overlap",
            }
            if not isinstance(rebind, dict) or not rebind_required.issubset(rebind):
                raise ValueError(
                    "each hidden_event_visual_rebinds item requires hidden_event_id "
                    "audio_next_visible_start visual_anchor_start next_text "
                    "visual_anchor_inside_hidden_interval timing_policy_id "
                    "visual_lead_interval_start visual_lead_interval_end "
                    "visual_lead_interval_overlaps_hidden_audio and "
                    "allow_hidden_visual_overlap"
                )
            if str(rebind["hidden_event_id"]) != hidden_label:
                continue
            if (
                parse_timestamp(str(rebind["audio_next_visible_start"]))
                != next_visible_start
                or str(rebind["next_text"]) != next_text
            ):
                add("error", None, "hidden visual rebind 的舊開口點或文字不符")
                continue
            visual_anchor_start = parse_timestamp(str(rebind["visual_anchor_start"]))
            source_matches = [
                record
                for record in matching_records
                if record["subtitle_start"] == visual_anchor_start
            ]
            if len(source_matches) != 1:
                add(
                    "error",
                    cue_by_start.get(format_timestamp(visual_anchor_start)),
                    "hidden visual rebind 必須恰有一筆對應 custom source timing record",
                )
                continue
            record = source_matches[0]
            if str(rebind["timing_policy_id"]) != record["timing_policy_id"]:
                add("error", record["cue"] if isinstance(record["cue"], Cue) else None, "hidden visual rebind 政策不符")
            lead_start = parse_timestamp(str(rebind["visual_lead_interval_start"]))
            lead_end = parse_timestamp(str(rebind["visual_lead_interval_end"]))
            if lead_start != visual_anchor_start or lead_end != next_visible_start:
                add("error", record["cue"] if isinstance(record["cue"], Cue) else None, "hidden visual rebind 的提前顯示區間不符")
            calculated_overlap = max(visual_anchor_start, audio_start) < min(
                next_visible_start, audio_end
            )
            declared_overlap = rebind["visual_lead_interval_overlaps_hidden_audio"]
            declared_allow = rebind["allow_hidden_visual_overlap"]
            inside_clipped_hidden = hidden_start <= visual_anchor_start < hidden_end
            if not isinstance(declared_overlap, bool) or not isinstance(
                declared_allow, bool
            ):
                raise ValueError(
                    "hidden visual rebind overlap and allow fields must be booleans"
                )
            if not isinstance(rebind["visual_anchor_inside_hidden_interval"], bool):
                raise ValueError(
                    "hidden visual rebind visual_anchor_inside_hidden_interval "
                    "must be a boolean"
                )
            if rebind["visual_anchor_inside_hidden_interval"] != inside_clipped_hidden:
                add("error", record["cue"] if isinstance(record["cue"], Cue) else None, "hidden visual rebind 的 clipped interval 指標不符")
            if declared_overlap != calculated_overlap or declared_allow != calculated_overlap:
                add("error", record["cue"] if isinstance(record["cue"], Cue) else None, "hidden visual rebind 的 overlap／allow 衍生值不符")
            if calculated_overlap:
                overlap_ids = record["hidden_overlap_ids"]
                if isinstance(overlap_ids, list) and hidden_label not in overlap_ids:
                    overlap_ids.append(hidden_label)
            matching_rebinds.append((rebind_index, rebind, record))

        if len(matching_rebinds) > 1:
            add("error", None, "同一 hidden event 有重複 visual rebind")
        if len(matching_rebinds) == 1:
            used_hidden_rebind_indexes.add(matching_rebinds[0][0])

        for record in matching_records:
            new_start = int(record["subtitle_start"])
            lead_interval_overlaps_hidden = max(new_start, audio_start) < min(
                next_visible_start, audio_end
            )
            if lead_interval_overlaps_hidden:
                overlap_ids = record["hidden_overlap_ids"]
                if isinstance(overlap_ids, list) and hidden_label not in overlap_ids:
                    overlap_ids.append(hidden_label)
                if not (
                    policy_allows_hidden_overlap
                    and record["allow_hidden_visual_overlap"] is True
                ):
                    add(
                        "error",
                        record["cue"] if isinstance(record["cue"], Cue) else None,
                        "視覺錨點提前顯示區間與 hidden audio 相交但未取得明示授權",
                    )

        for cue in cues:
            if hidden_start <= cue.start_ms < hidden_end:
                allowed_cue = any(
                    int(record["subtitle_start"]) == cue.start_ms
                    and record["cue_text"] == cue.visible_text
                    and policy_allows_hidden_overlap
                    and record["allow_hidden_visual_overlap"] is True
                    for record in matching_records
                )
                if not allowed_cue:
                    add(
                        "error",
                        cue,
                        "字幕起點落在被省略聲音期間 後句可能被提前",
                    )
        expected_cue = cue_by_start.get(format_timestamp(next_visible_start))
        if expected_cue is None:
            if len(matching_rebinds) != 1:
                add(
                    "error",
                    None,
                    f"被省略聲音後的實際開口點 "
                    f"{format_timestamp(next_visible_start)} 不存在",
                )
        elif expected_cue.visible_text != next_text:
            add(
                "error",
                expected_cue,
                f"被省略聲音後字幕不符 預期 {next_text!r} "
                f"實際 {expected_cue.visible_text!r}",
            )

    for record in custom_source_records:
        has_hidden_overlap = bool(record["hidden_overlap_ids"])
        if bool(record["allow_hidden_visual_overlap"]) != has_hidden_overlap:
            add(
                "error",
                record["cue"] if isinstance(record["cue"], Cue) else None,
                "custom source 的 allow_hidden_visual_overlap 必須等於實際 hidden audio overlap",
            )
        if has_hidden_overlap and not policy_allows_hidden_overlap:
            add(
                "error",
                record["cue"] if isinstance(record["cue"], Cue) else None,
                "本集政策未允許 hidden audio overlap",
            )
    for rebind_index, rebind in enumerate(hidden_rebind_items):
        if rebind_index not in used_hidden_rebind_indexes:
            add("error", None, f"hidden visual rebind 沒有對應事件 {rebind.get('hidden_event_id')!r}")

    xml_post_snap_review_required = decisions.get(
        "xml_post_snap_review_required", False
    )
    if not isinstance(xml_post_snap_review_required, bool):
        raise ValueError("xml_post_snap_review_required must be a boolean")
    for item in xml_edit_items:
        required = {
            "cut",
            "original_boundary",
            "final_boundary",
            "mode",
            "status",
            "evidence",
        }
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.xml_edit_checks item requires cut original_boundary "
                "final_boundary mode status and evidence"
            )
        cut = parse_timestamp(str(item["cut"]))
        original_boundary = parse_timestamp(str(item["original_boundary"]))
        final_boundary = parse_timestamp(str(item["final_boundary"]))
        mode = str(item["mode"]).lower()
        status = str(item["status"]).lower()
        evidence = str(item["evidence"]).strip()
        adopted_xml_modes = {
            "word_aware",
            "timing_only",
            USER_VISUAL_ANCHOR_MODE,
            LOCKED_PREFIX_WAVEFORM_XML_MODE,
        }
        fps: float | None = None
        max_snap_frames: int | None = None
        if mode in adopted_xml_modes:
            if "fps" not in item or "max_snap_frames" not in item:
                raise ValueError(
                    "adopted xml edit check requires fps and max_snap_frames"
                )
            fps = float(item["fps"])
            max_snap_frames = int(item["max_snap_frames"])
            if fps <= 0:
                raise ValueError("xml edit check fps must be positive")
            if max_snap_frames != 5:
                add(
                    "error",
                    None,
                    "Premiere XML 吸附上限必須固定為前後 5 幀 "
                    f"目前設定為 {max_snap_frames} 幀",
                )
        if not evidence:
            raise ValueError("xml edit check evidence must not be empty")
        if status in {"pending", "unresolved", "待確認", "未處理"}:
            add(
                "error",
                None,
                f"XML 剪輯點尚未完成 {format_timestamp(cut)}",
            )
        elif status not in {
            "resolved",
            "confirmed",
            "reviewed",
            "not_used",
            "已確認",
            "已處理",
            "未採用",
        }:
            raise ValueError(
                "xml edit check status must be resolved confirmed reviewed "
                "not_used 已確認 已處理 or 未採用"
            )

        custom_xml_context: dict[str, object] | None = None
        if mode == USER_VISUAL_ANCHOR_MODE:
            custom_required = {
                "anchor_kind",
                "xml_frame",
                "reference_boundary",
                "reference_frame",
                "visual_lead_frames",
                "before_text",
                "after_text",
                "speaker",
                "source_track",
                "waveform_onset",
                "serialized_cut_roundtrip_passed",
                "allow_hidden_visual_overlap",
                "manual_listening_claimed",
                "post_snap_audio_check",
                "hidden_event_conflict",
                "hidden_event_ids",
                "speaker_boundary_preserved",
            }
            if not custom_required.issubset(item):
                raise ValueError(
                    "user_confirmed_visual_anchor xml edit check requires "
                    "timing_policy_id anchor_kind xml_frame fps_numerator "
                    "fps_denominator reference_boundary reference_frame "
                    "visual_lead_frames before_text after_text speaker source_track "
                    "waveform_onset serialized_cut_roundtrip_passed "
                    "allow_hidden_visual_overlap manual_listening_claimed "
                    "post_snap_audio_check hidden_event_conflict hidden_event_ids "
                    "and speaker_boundary_preserved"
                )
            if status not in {"confirmed", "已確認"}:
                add("error", None, "custom XML edit check 尚未確認")
            strict_int(item["max_snap_frames"], "custom xml max_snap_frames")
            timing_policy_id = str(
                aliased_value(
                    item,
                    "timing_policy_id",
                    "policy_id",
                    "user_confirmed_visual_anchor xml edit check",
                )
            ).strip()
            record_fps_numerator = strict_int(
                aliased_value(
                    item,
                    "fps_numerator",
                    "fps_num",
                    "user_confirmed_visual_anchor xml edit check",
                ),
                "custom xml fps_numerator",
            )
            record_fps_denominator = strict_int(
                aliased_value(
                    item,
                    "fps_denominator",
                    "fps_den",
                    "user_confirmed_visual_anchor xml edit check",
                ),
                "custom xml fps_denominator",
            )
            if record_fps_numerator <= 0 or record_fps_denominator <= 0:
                raise ValueError("custom xml fps must be positive")
            if str(item["anchor_kind"]).lower().strip() != "xml_hard_cut":
                raise ValueError(
                    "custom xml edit check anchor_kind must be xml_hard_cut"
                )
            xml_frame = strict_int(item["xml_frame"], "custom xml xml_frame")
            reference_boundary = parse_timestamp(str(item["reference_boundary"]))
            reference_frame = strict_int(
                item["reference_frame"], "custom xml reference_frame"
            )
            visual_lead_frames = strict_int(
                item["visual_lead_frames"], "custom xml visual_lead_frames"
            )
            waveform_onset = parse_timestamp(str(item["waveform_onset"]))
            source_track = str(item["source_track"]).strip()
            speaker = str(item["speaker"]).strip()
            before_text = str(item["before_text"])
            after_text = str(item["after_text"])
            allow_hidden_overlap = item["allow_hidden_visual_overlap"]
            if not isinstance(allow_hidden_overlap, bool):
                raise ValueError(
                    "custom xml allow_hidden_visual_overlap must be a boolean"
                )
            if not isinstance(item["hidden_event_ids"], list):
                raise ValueError("custom xml hidden_event_ids must be a list")
            if item["serialized_cut_roundtrip_passed"] is not True:
                add("error", None, "custom XML 剪輯點未通過 SRT 毫秒 round-trip")
            if item["manual_listening_claimed"] is not False:
                add("error", None, "自動驗證不得虛構人工回聽已完成")
            if not xml_post_snap_review_required:
                add("error", None, "custom XML 模式必須啟用 xml_post_snap_review_required")
            if episode_policy is None:
                add("error", None, "custom XML 模式缺少 episode_timing_policy")
            else:
                if timing_policy_id != episode_policy["policy_id"]:
                    add("error", None, "custom XML timing_policy_id 與本集政策不符")
                if (
                    record_fps_numerator != episode_policy["fps_numerator"]
                    or record_fps_denominator != episode_policy["fps_denominator"]
                ):
                    add("error", None, "custom XML 幀率與本集政策不符")
            if abs(fps - record_fps_numerator / record_fps_denominator) > 1e-9:
                add("error", None, "custom XML fps 浮點摘要與精確幀率不符")
            cut_frame = nominal_frame(
                cut, record_fps_numerator, record_fps_denominator
            )
            calculated_reference_frame = nominal_frame(
                waveform_onset, record_fps_numerator, record_fps_denominator
            )
            calculated_lead_frames = calculated_reference_frame - cut_frame
            if cut_frame != xml_frame:
                add("error", None, "custom XML 的 SRT cut 毫秒反算未落在 xml_frame")
            if reference_boundary != waveform_onset:
                add("error", None, "custom XML reference_boundary 必須等於 waveform_onset")
            if reference_frame != calculated_reference_frame:
                add("error", None, "custom XML reference_frame 計算不符")
            if visual_lead_frames != calculated_lead_frames:
                add("error", None, "custom XML visual_lead_frames 計算不符")
            if not 0 <= calculated_lead_frames <= 5:
                add(
                    "error",
                    None,
                    "custom XML 畫面錨點只能比真實開口早 0 至 5 個 nominal frames",
                )
            source_matches = [
                record
                for record in custom_source_records
                if record["anchor_kind"] == "xml_hard_cut"
                and record["subtitle_start"] == cut
                and record["cue_text"] == after_text
                and record["speaker"] == speaker
                and record["source_track"] == source_track
                and record["waveform_onset"] == waveform_onset
                and record["timing_policy_id"] == timing_policy_id
                and record["xml_frame"] == xml_frame
                and record["reference_frame"] == reference_frame
                and record["visual_lead_frames"] == visual_lead_frames
            ]
            if len(source_matches) != 1:
                add(
                    "error",
                    cue_by_start.get(format_timestamp(cut)),
                    "每筆 custom XML edit check 必須恰有一筆對應 source timing record",
                )
                source_match = None
            else:
                source_match = source_matches[0]
                source_match["xml_match_count"] = int(source_match["xml_match_count"]) + 1
            custom_xml_context = {
                "source_match": source_match,
                "before_text": before_text,
                "after_text": after_text,
                "allow_hidden_visual_overlap": allow_hidden_overlap,
            }
        elif mode == LOCKED_PREFIX_WAVEFORM_XML_MODE:
            reconciled_xml_required = {
                "xml_frame",
                "fps_numerator",
                "fps_denominator",
                "speech_boundary",
                "before_text",
                "after_text",
                "pre_resegment_text_stream",
                "post_resegment_text_stream",
                "text_conserved",
                "speaker",
                "source_track",
                "waveform_onset",
                "refined_onset_frame",
                "cut_to_refined_onset_frames",
                "serialized_cut_roundtrip_passed",
                "reconciliation_method",
                "raw_alignment_mismatch",
                "raw_alignment_mismatch_present",
                "raw_alignment_mismatch_sha256",
                "raw_alignment_mismatch_preserved",
                "raw_token_onset",
                "locked_visible_prefix",
                "locked_visible_prefix_sha256",
                "locked_visible_text_stream_sha256",
                "formal_audit_cut_id",
                "formal_audit_record_sha256",
                "outgoing_speech_end",
                "outgoing_speaker",
                "outgoing_source_track",
                "cross_speaker",
                "timing_source_role",
                "outgoing_track_used_for_timing",
                "source_track_dominance_passed",
                "post_snap_audio_check",
                "hidden_event_conflict",
                "hidden_event_ids",
                "speaker_boundary_preserved",
            }
            if not reconciled_xml_required.issubset(item):
                raise ValueError(
                    f"{LOCKED_PREFIX_WAVEFORM_XML_MODE} xml edit check requires "
                    "resegmentation text refined onset raw mismatch source lock "
                    "post-snap hidden truth and formal audit evidence"
                )
            record_fps_numerator = strict_int(
                item["fps_numerator"], "reconciled xml fps_numerator"
            )
            record_fps_denominator = strict_int(
                item["fps_denominator"], "reconciled xml fps_denominator"
            )
            xml_frame = strict_int(item["xml_frame"], "reconciled xml xml_frame")
            waveform_onset = parse_timestamp(str(item["waveform_onset"]))
            speech_boundary = parse_timestamp(str(item["speech_boundary"]))
            raw_token_onset = parse_timestamp(str(item["raw_token_onset"]))
            speaker = str(item["speaker"])
            source_track = str(item["source_track"])
            before_text = str(item["before_text"])
            after_text = str(item["after_text"])
            mismatch = item["raw_alignment_mismatch"]
            audit_cut_id = str(item["formal_audit_cut_id"])
            if record_fps_numerator <= 0 or record_fps_denominator <= 0:
                raise ValueError("reconciled xml fps must be positive")
            if abs(
                float(item["fps"])
                - record_fps_numerator / record_fps_denominator
            ) > 1e-9:
                add("error", None, "refined XML fps 摘要與精確幀率不符")
            expected_cut_ms = serialized_frame_timestamp_ms(
                xml_frame, record_fps_numerator, record_fps_denominator
            )
            if (
                item["serialized_cut_roundtrip_passed"] is not True
                or cut != expected_cut_ms
                or nominal_frame(
                    cut, record_fps_numerator, record_fps_denominator
                )
                != xml_frame
            ):
                add("error", None, "refined XML cut 未通過 ceil-ms nominal-frame round-trip")
            refined_frame = nominal_frame(
                waveform_onset, record_fps_numerator, record_fps_denominator
            )
            declared_refined_frame = strict_int(
                item["refined_onset_frame"], "reconciled xml refined_onset_frame"
            )
            declared_delta = strict_int(
                item["cut_to_refined_onset_frames"],
                "reconciled xml cut_to_refined_onset_frames",
            )
            if declared_refined_frame != refined_frame:
                add("error", None, "refined XML onset frame 計算不符")
            if declared_delta != refined_frame - xml_frame:
                add("error", None, "refined XML nominal frame delta 計算不符")
            if not 0 <= refined_frame - xml_frame <= 5:
                add("error", None, "refined XML onset 必須位於 cut 後 0 至 5 幀")
            if speech_boundary != waveform_onset:
                add("error", None, "refined XML speech_boundary 必須等於 waveform_onset")
            pre_stream = str(item["pre_resegment_text_stream"])
            post_stream = str(item["post_resegment_text_stream"])
            if item["text_conserved"] is not True or pre_stream != post_stream:
                add("error", None, "refined XML 重分段前後文字未守恆")
            if post_stream != before_text + after_text:
                add("error", None, "refined XML post-resegment stream 與兩側字幕不符")
            if text_stream_lock_hash is None or str(
                item["locked_visible_text_stream_sha256"]
            ) != text_stream_lock_hash:
                add("error", None, "refined XML 缺少一致的全域可見文字串鎖定")
            if item["raw_alignment_mismatch_present"] != (mismatch is not None):
                add("error", None, "refined XML raw mismatch presence 指標不符")
            if item["raw_alignment_mismatch_preserved"] is not True:
                add("error", None, "refined XML 未保留 raw alignment mismatch")
            if str(item["raw_alignment_mismatch_sha256"]) != canonical_json_sha256(
                mismatch
            ):
                add("error", None, "refined XML raw mismatch SHA-256 不符")
            locked_prefix = str(item["locked_visible_prefix"])
            if not locked_prefix or not after_text.startswith(locked_prefix):
                add("error", None, "refined XML locked prefix 不是後段句首")
            if str(item["locked_visible_prefix_sha256"]) != sha256_bytes(
                locked_prefix.encode("utf-8")
            ):
                add("error", None, "refined XML locked prefix SHA-256 不符")
            if item["timing_source_role"] != "incoming_locked_source_track_only":
                add("error", None, "refined XML 只能使用 incoming locked source track")
            if item["outgoing_track_used_for_timing"] is not False:
                add("error", None, "refined XML 不得使用 outgoing 或他軌決定時間")
            if parse_timestamp(str(item["outgoing_speech_end"])) > cut + 1:
                add("error", None, "refined XML outgoing speech 尚未在 cut 前結束")
            if item["source_track_dominance_passed"] is not True:
                add("error", None, "refined XML incoming source dominance 未通過")
            if not isinstance(item["hidden_event_ids"], list):
                raise ValueError("reconciled XML hidden_event_ids must be a list")
            actual_hidden_ids: list[str] = []
            for hidden_item in hidden_items:
                if not isinstance(hidden_item, dict):
                    continue
                audio_start = parse_timestamp(
                    str(hidden_item.get("audio_start", hidden_item.get("start")))
                )
                audio_end = parse_timestamp(
                    str(hidden_item.get("audio_end", hidden_item.get("end")))
                )
                if max(cut, audio_start) < min(waveform_onset, audio_end):
                    actual_hidden_ids.append(
                        str(
                            hidden_item.get(
                                "id",
                                f"{format_timestamp(audio_start)}-"
                                f"{format_timestamp(audio_end)}",
                            )
                        )
                    )
            if sorted(str(value) for value in item["hidden_event_ids"]) != sorted(
                actual_hidden_ids
            ) or bool(actual_hidden_ids) != item["hidden_event_conflict"]:
                add("error", None, "refined XML hidden conflict 衍生值不如實")
            if item["hidden_event_conflict"] is not False or item["hidden_event_ids"]:
                add("error", None, "refined XML adopted cut 必須如實證明沒有 hidden conflict")
            source_matches = [
                record
                for record in reconciled_source_records
                if record["subtitle_start"] == cut
                and record["cue_text"] == after_text
                and record["speaker"] == speaker
                and record["source_track"] == source_track
                and record["waveform_onset"] == waveform_onset
                and record["xml_frame"] == xml_frame
                and record["raw_alignment_mismatch"] == mismatch
                and record["raw_token_onset"] == raw_token_onset
                and record["formal_audit_cut_id"] == audit_cut_id
                and record["cross_speaker"] == item["cross_speaker"]
            ]
            if len(source_matches) != 1:
                add(
                    "error",
                    cue_by_start.get(format_timestamp(cut)),
                    "每筆 refined XML check 必須恰有一筆對應 source timing record",
                )
            else:
                source_match = source_matches[0]
                source_match["xml_match_count"] = int(source_match["xml_match_count"]) + 1
                source_item = source_match["item"]
                assert isinstance(source_item, dict)
                mirrored_fields = {
                    "formal_audit_record_sha256",
                    "reconciliation_method",
                    "raw_alignment_mismatch_sha256",
                    "raw_alignment_mismatch_preserved",
                    "locked_visible_prefix",
                    "locked_visible_prefix_sha256",
                    "locked_visible_text_stream_sha256",
                    "outgoing_speech_end",
                    "outgoing_speaker",
                    "outgoing_source_track",
                    "timing_source_role",
                    "outgoing_track_used_for_timing",
                    "source_track_dominance_passed",
                    "speaker_boundary_preserved",
                }
                for field in mirrored_fields:
                    if source_item.get(field) != item.get(field):
                        add("error", None, f"refined XML/source 欄位不一致 {field}")
        elif episode_policy is not None and mode in {"word_aware", "timing_only"}:
            if item.get("timing_policy_id") not in {None, ""}:
                add(
                    "error",
                    None,
                    "本集 user-confirmed 畫面例外不得偽裝為標準 XML adopted mode",
                )
            if xml_audit_lock is None:
                add(
                    "error",
                    None,
                    "與本集畫面例外並存的標準 XML adopted mode 必須由正式 audit 鎖定",
                )

        if mode in {
            "word_aware",
            "timing_only",
            USER_VISUAL_ANCHOR_MODE,
            LOCKED_PREFIX_WAVEFORM_XML_MODE,
        }:
            if xml_post_snap_review_required:
                post_required = {
                    "post_snap_audio_check",
                    "hidden_event_conflict",
                    "speaker_boundary_preserved",
                }
                if not post_required.issubset(item):
                    raise ValueError(
                        "adopted xml edit check requires post_snap_audio_check "
                        "hidden_event_conflict and speaker_boundary_preserved"
                    )
                post_snap_audio_check = str(
                    item["post_snap_audio_check"]
                ).lower().strip()
                hidden_event_conflict = item["hidden_event_conflict"]
                speaker_boundary_preserved = item["speaker_boundary_preserved"]
                if not isinstance(hidden_event_conflict, bool):
                    raise ValueError("hidden_event_conflict must be a boolean")
                if not isinstance(speaker_boundary_preserved, bool):
                    raise ValueError("speaker_boundary_preserved must be a boolean")
                if (
                    mode in {
                        USER_VISUAL_ANCHOR_MODE,
                        LOCKED_PREFIX_WAVEFORM_XML_MODE,
                    }
                    and post_snap_audio_check != "passed"
                ) or (
                    mode
                    not in {
                        USER_VISUAL_ANCHOR_MODE,
                        LOCKED_PREFIX_WAVEFORM_XML_MODE,
                    }
                    and post_snap_audio_check
                    not in {
                        "passed",
                        "resolved",
                        "reviewed",
                        "通過",
                        "已確認",
                    }
                ):
                    add(
                        "error",
                        None,
                        f"XML 吸附後尚未完成原音回查 {format_timestamp(cut)}",
                    )
                if hidden_event_conflict:
                    if mode == USER_VISUAL_ANCHOR_MODE:
                        source_match = (
                            custom_xml_context.get("source_match")
                            if custom_xml_context is not None
                            else None
                        )
                        actual_hidden_ids = (
                            source_match.get("hidden_overlap_ids", [])
                            if isinstance(source_match, dict)
                            else []
                        )
                        declared_hidden_ids = item.get("hidden_event_ids", [])
                        if (
                            not isinstance(declared_hidden_ids, list)
                            or sorted(str(value) for value in declared_hidden_ids)
                            != sorted(str(value) for value in actual_hidden_ids)
                            or not actual_hidden_ids
                            or not policy_allows_hidden_overlap
                            or not isinstance(source_match, dict)
                            or source_match.get("allow_hidden_visual_overlap") is not True
                            or custom_xml_context.get("allow_hidden_visual_overlap")
                            is not True
                        ):
                            add(
                                "error",
                                None,
                                f"custom XML hidden overlap 缺少一致的明示授權 "
                                f"{format_timestamp(cut)}",
                            )
                    else:
                        add(
                            "error",
                            None,
                            f"XML 吸附落入被省略聲音事件 {format_timestamp(cut)}",
                        )
                elif mode == USER_VISUAL_ANCHOR_MODE:
                    source_match = (
                        custom_xml_context.get("source_match")
                        if custom_xml_context is not None
                        else None
                    )
                    actual_hidden_ids = (
                        source_match.get("hidden_overlap_ids", [])
                        if isinstance(source_match, dict)
                        else []
                    )
                    if (
                        actual_hidden_ids
                        or item.get("hidden_event_ids")
                        or custom_xml_context.get("allow_hidden_visual_overlap") is True
                    ):
                        add(
                            "error",
                            None,
                            f"custom XML hidden overlap 衍生欄位不一致 "
                            f"{format_timestamp(cut)}",
                        )
                if not speaker_boundary_preserved:
                    add(
                        "error",
                        None,
                        f"XML 吸附破壞講者邊界 {format_timestamp(cut)}",
                    )
            max_delta_ms = max_snap_frames * 1000 / fps
            if (
                mode == USER_VISUAL_ANCHOR_MODE
                and final_boundary != cut
            ) or (
                mode != USER_VISUAL_ANCHOR_MODE
                and abs(final_boundary - cut) > 1
            ):
                add(
                    "error",
                    None,
                    "已採用的 XML 字幕邊界必須精確對齊剪輯點",
                )
            boundary_cue = cue_by_start.get(format_timestamp(final_boundary))
            if boundary_cue is None:
                add(
                    "error",
                    None,
                    f"XML 對齊邊界 {format_timestamp(final_boundary)} "
                    "不是任何字幕的起點",
                )
            if mode == "word_aware":
                if (
                    "speech_boundary" not in item
                    or "before_text" not in item
                    or "after_text" not in item
                ):
                    raise ValueError(
                        "word_aware xml edit check requires speech_boundary "
                        "before_text and after_text"
                    )
                speech_boundary = parse_timestamp(str(item["speech_boundary"]))
                if formal_audit_sha256 is not None:
                    exact_required = {
                        "xml_frame",
                        "fps_numerator",
                        "fps_denominator",
                        "refined_onset_frame",
                        "refined_onset_delta_frames",
                        "serialized_cut_roundtrip",
                        "serialized_cut_timestamp",
                        "full_cut_audit_sha256",
                    }
                    if not exact_required.issubset(item):
                        raise ValueError(
                            "audit-locked word_aware XML requires exact nominal-frame "
                            "ceil-ms and audit evidence"
                        )
                    record_fps_numerator = strict_int(
                        item["fps_numerator"], "word_aware fps_numerator"
                    )
                    record_fps_denominator = strict_int(
                        item["fps_denominator"], "word_aware fps_denominator"
                    )
                    xml_frame = strict_int(item["xml_frame"], "word_aware xml_frame")
                    refined_frame = nominal_frame(
                        speech_boundary,
                        record_fps_numerator,
                        record_fps_denominator,
                    )
                    if strict_int(
                        item["refined_onset_frame"],
                        "word_aware refined_onset_frame",
                    ) != refined_frame:
                        add("error", None, "word_aware refined onset frame 不符")
                    if strict_int(
                        item["refined_onset_delta_frames"],
                        "word_aware refined_onset_delta_frames",
                    ) != refined_frame - xml_frame:
                        add("error", None, "word_aware refined onset delta 不符")
                    if not 0 <= refined_frame - xml_frame <= 5:
                        add("error", None, "word_aware 主軌開口必須位於 cut 後 0 至 5 幀")
                    if (
                        item["serialized_cut_roundtrip"] is not True
                        or str(item["serialized_cut_timestamp"])
                        != format_timestamp(cut)
                        or cut
                        != serialized_frame_timestamp_ms(
                            xml_frame,
                            record_fps_numerator,
                            record_fps_denominator,
                        )
                    ):
                        add("error", None, "word_aware XML 未通過 ceil-ms round-trip")
                    if str(item["full_cut_audit_sha256"]) != formal_audit_sha256:
                        add("error", None, "word_aware XML audit SHA-256 不符")
                else:
                    speech_delta_ms = abs(speech_boundary - cut)
                    if speech_delta_ms > max_delta_ms + 1:
                        add(
                            "error",
                            None,
                            f"真實詞彙邊界距離 XML 剪輯點 {speech_delta_ms:.0f}ms "
                            f"超過 5 幀上限 {max_delta_ms:.1f}ms",
                        )
                before_text = str(item["before_text"])
                after_text = str(item["after_text"])
                boundary_index = next(
                    (
                        index
                        for index, cue in enumerate(cues)
                        if cue.start_ms == final_boundary
                    ),
                    None,
                )
                if boundary_index is None or boundary_index == 0:
                    add(
                        "error",
                        None,
                        "詞級 XML 對齊找不到剪輯點前後兩段字幕",
                    )
                else:
                    before_cue = cues[boundary_index - 1]
                    after_cue = cues[boundary_index]
                    if before_cue.end_ms != final_boundary:
                        add(
                            "error",
                            before_cue,
                            "詞級 XML 對齊後前段未延續到同一剪輯點",
                        )
                    if before_cue.visible_text != before_text:
                        add(
                            "error",
                            before_cue,
                            f"XML 剪輯點前文字不符 預期 {before_text!r} "
                            f"實際 {before_cue.visible_text!r}",
                        )
                    if after_cue.visible_text != after_text:
                        add(
                            "error",
                            after_cue,
                            f"XML 剪輯點後文字不符 預期 {after_text!r} "
                            f"實際 {after_cue.visible_text!r}",
                        )
            elif mode == LOCKED_PREFIX_WAVEFORM_XML_MODE:
                before_text = str(item["before_text"])
                after_text = str(item["after_text"])
                boundary_index = next(
                    (
                        index
                        for index, cue in enumerate(cues)
                        if cue.start_ms == final_boundary
                    ),
                    None,
                )
                if boundary_index is None or boundary_index == 0:
                    add(
                        "error",
                        None,
                        "refined XML 對齊找不到剪輯點前後兩段字幕",
                    )
                else:
                    before_cue = cues[boundary_index - 1]
                    after_cue = cues[boundary_index]
                    if before_cue.end_ms > final_boundary:
                        add("error", before_cue, "refined XML 前段不得越過剪輯點")
                    if before_cue.visible_text != before_text:
                        add(
                            "error",
                            before_cue,
                            f"refined XML 剪輯點前文字不符 預期 {before_text!r} "
                            f"實際 {before_cue.visible_text!r}",
                        )
                    if after_cue.visible_text != after_text:
                        add(
                            "error",
                            after_cue,
                            f"refined XML 剪輯點後文字不符 預期 {after_text!r} "
                            f"實際 {after_cue.visible_text!r}",
                        )
            elif mode == USER_VISUAL_ANCHOR_MODE:
                assert custom_xml_context is not None
                before_text = str(custom_xml_context["before_text"])
                after_text = str(custom_xml_context["after_text"])
                boundary_index = next(
                    (
                        index
                        for index, cue in enumerate(cues)
                        if cue.start_ms == final_boundary
                    ),
                    None,
                )
                if boundary_index is None or boundary_index == 0:
                    add(
                        "error",
                        None,
                        "custom XML 對齊找不到剪輯點前後兩段字幕",
                    )
                else:
                    before_cue = cues[boundary_index - 1]
                    after_cue = cues[boundary_index]
                    if before_cue.end_ms > final_boundary:
                        add(
                            "error",
                            before_cue,
                            "custom XML 對齊後前段不得越過畫面剪輯點",
                        )
                    if before_cue.visible_text != before_text:
                        add(
                            "error",
                            before_cue,
                            f"custom XML 剪輯點前文字不符 預期 {before_text!r} "
                            f"實際 {before_cue.visible_text!r}",
                        )
                    if after_cue.visible_text != after_text:
                        add(
                            "error",
                            after_cue,
                            f"custom XML 剪輯點後文字不符 預期 {after_text!r} "
                            f"實際 {after_cue.visible_text!r}",
                        )
            else:
                original_delta_ms = abs(original_boundary - cut)
                if original_delta_ms > max_delta_ms + 1:
                    add(
                        "error",
                        None,
                        f"原字幕邊界距離 XML 剪輯點 {original_delta_ms:.0f}ms "
                        f"超過 5 幀上限 {max_delta_ms:.1f}ms",
                    )
        elif mode == "not_used":
            if "reason" not in item or not str(item["reason"]).strip():
                raise ValueError("not_used xml edit check requires reason")
            if final_boundary != original_boundary:
                add(
                    "error",
                    None,
                    "未採用的 XML 候選必須保留原本的聲音與語意邊界",
                )
        else:
            raise ValueError(
                "xml edit check mode must be word_aware timing_only "
                "user_confirmed_visual_anchor "
                f"{LOCKED_PREFIX_WAVEFORM_XML_MODE} or not_used"
            )

    for record in custom_source_records:
        if record["anchor_kind"] == "xml_hard_cut" and record["xml_match_count"] != 1:
            add(
                "error",
                record["cue"] if isinstance(record["cue"], Cue) else None,
                "每個 XML hard-cut custom source record 必須恰有一筆 custom XML check",
            )

    for record in reconciled_source_records:
        if record["xml_match_count"] != 1:
            add(
                "error",
                record["cue"] if isinstance(record["cue"], Cue) else None,
                "每個 refined prefix-waveform source record 必須恰有一筆 XML check",
            )

    summary = {
        "file": str(path),
        "segments": len(cues),
        "errors": sum(issue["level"] == "error" for issue in issues),
        "warnings": sum(issue["level"] == "warning" for issue in issues),
        "style_policy": style_policy,
        "issues": issues,
    }
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"{path}: {summary['segments']} 段")
        for issue in issues:
            print(f"{issue['level'].upper()} cue {issue['cue']}: {issue['message']}")
        print(f"errors={summary['errors']} warnings={summary['warnings']}")
    return 1 if summary["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("files", type=Path, nargs="+")
    analyze_parser.add_argument("--json", action="store_true")

    flatten_parser = subparsers.add_parser("flatten")
    flatten_parser.add_argument("input", type=Path)
    flatten_parser.add_argument("output", type=Path)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("file", type=Path)
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.add_argument("--decisions", type=Path)
    validate_parser.add_argument("--profile", type=Path)
    validate_parser.add_argument("--workspace", type=Path)
    validate_parser.add_argument("--context", type=Path)
    validate_parser.add_argument("--bindings", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "analyze":
            return analyze(args.files, args.json)
        if args.command == "flatten":
            return flatten(args.input, args.output)
        if args.workspace:
            from subtitle_context import resolve_context
            if not args.context:
                raise ValueError("--workspace requires --context")
            if args.profile or args.decisions:
                raise ValueError("Use upstream profile and pinned decisions with --workspace")
            context = resolve_context(args.workspace, args.context, args.bindings)
            candidate = args.file.expanduser().resolve()
            if not candidate.is_relative_to(context["evidence_dir"]):
                from subtitle_context import inside, digest
                registered = [row for row in context["job"].get("deliverables", [])
                              if row.get("job_id") == context["job"]["job_id"]
                              and row.get("role") in ("final_srt", "review_srt", "ai_baseline")
                              and inside(context["delivery_dir"], row.get("path")) == candidate
                              and row.get("sha256") == digest(candidate)]
                if len(registered) != 1:
                    raise ValueError("SRT is not in this job's evidence directory or pinned deliverables")
            return validate(candidate, args.json, context["decisions_path"],
                            context["profile_path"], profile_data=context["profile"])
        if args.context or args.bindings:
            raise ValueError("--context and --bindings require --workspace")
        return validate(args.file, args.json, args.decisions, args.profile)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
