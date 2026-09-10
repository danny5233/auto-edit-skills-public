#!/usr/bin/env python3
"""Analyze or validate SRT files against the user's subtitle style."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path


TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")
FORBIDDEN_PUNCT = re.compile(r"[，。！？；：,.!?;:「」『』（）()〔〕【】《》〈〉﹁﹂﹃﹄…]")
NUMERIC_RATIO_COLON = re.compile(r"(?<=\d):(?=\d)")
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


def validate(path: Path, as_json: bool, decisions_path: Path | None) -> int:
    cues = parse_srt(path)
    issues: list[dict[str, object]] = []
    decisions = load_decisions(decisions_path)
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
            elif 0 < gap < 1000:
                add("warning", cue, f"與下一段空白 {gap}ms 可能造成閃爍")
            elif gap >= 1000:
                add("warning", cue, f"與下一段空白 {gap}ms 請回聽確認停頓與收尾時間")

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
    for item in decisions.get("exact", []):
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
    for item in decisions.get("hidden_events", []):
        required = {"start", "end", "next_visible_start", "next_text"}
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.hidden_events item requires "
                "start end next_visible_start and next_text"
            )
        hidden_start = parse_timestamp(str(item["start"]))
        hidden_end = parse_timestamp(str(item["end"]))
        next_visible_start = parse_timestamp(str(item["next_visible_start"]))
        next_text = str(item["next_text"])
        if not hidden_start < hidden_end <= next_visible_start:
            raise ValueError(
                "hidden event must satisfy start < end <= next_visible_start"
            )
        for cue in cues:
            if hidden_start <= cue.start_ms < hidden_end:
                add(
                    "error",
                    cue,
                    "字幕起點落在被省略聲音期間 後句可能被提前",
                )
        expected_cue = cue_by_start.get(format_timestamp(next_visible_start))
        if expected_cue is None:
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
    for item in decisions.get("source_timing_checks", []):
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
        elif status not in {"resolved", "confirmed", "reviewed", "已確認", "已處理"}:
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
        if mode in {"source_track", "source_track_xml"}:
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
                max_delta_ms = max_snap_frames * 1000 / fps
                onset_delta_ms = abs(waveform_onset - xml_cut)
                if onset_delta_ms > max_delta_ms + 1:
                    add(
                        "error",
                        cue_at_start,
                        f"主講者波形頭距離 XML 剪輯點 {onset_delta_ms:.0f}ms "
                        f"超過 5 幀上限 {max_delta_ms:.1f}ms",
                    )
        elif mode == "fallback_after_review":
            if "reason" not in item or not str(item["reason"]).strip():
                raise ValueError(
                    "fallback_after_review source timing check requires reason"
                )
        else:
            raise ValueError(
                "source timing check mode must be source_track "
                "source_track_xml or fallback_after_review"
            )
    xml_post_snap_review_required = decisions.get(
        "xml_post_snap_review_required", False
    )
    if not isinstance(xml_post_snap_review_required, bool):
        raise ValueError("xml_post_snap_review_required must be a boolean")
    for item in decisions.get("xml_edit_checks", []):
        required = {
            "cut",
            "fps",
            "max_snap_frames",
            "original_boundary",
            "final_boundary",
            "mode",
            "status",
            "evidence",
        }
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(
                "each decisions.xml_edit_checks item requires cut fps "
                "max_snap_frames original_boundary final_boundary mode status and evidence"
            )
        cut = parse_timestamp(str(item["cut"]))
        original_boundary = parse_timestamp(str(item["original_boundary"]))
        final_boundary = parse_timestamp(str(item["final_boundary"]))
        fps = float(item["fps"])
        max_snap_frames = int(item["max_snap_frames"])
        mode = str(item["mode"]).lower()
        status = str(item["status"]).lower()
        evidence = str(item["evidence"]).strip()
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

        if mode in {"word_aware", "timing_only"}:
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
                if post_snap_audio_check not in {
                    "passed",
                    "resolved",
                    "reviewed",
                    "通過",
                    "已確認",
                }:
                    add(
                        "error",
                        None,
                        f"XML 吸附後尚未完成原音回查 {format_timestamp(cut)}",
                    )
                if hidden_event_conflict:
                    add(
                        "error",
                        None,
                        f"XML 吸附落入被省略聲音事件 {format_timestamp(cut)}",
                    )
                if not speaker_boundary_preserved:
                    add(
                        "error",
                        None,
                        f"XML 吸附破壞講者邊界 {format_timestamp(cut)}",
                    )
            max_delta_ms = max_snap_frames * 1000 / fps
            if abs(final_boundary - cut) > 1:
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
        else:
            raise ValueError(
                "xml edit check mode must be word_aware timing_only or not_used"
            )

    summary = {
        "file": str(path),
        "segments": len(cues),
        "errors": sum(issue["level"] == "error" for issue in issues),
        "warnings": sum(issue["level"] == "warning" for issue in issues),
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

    args = parser.parse_args()
    try:
        if args.command == "analyze":
            return analyze(args.files, args.json)
        if args.command == "flatten":
            return flatten(args.input, args.output)
        return validate(args.file, args.json, args.decisions)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
