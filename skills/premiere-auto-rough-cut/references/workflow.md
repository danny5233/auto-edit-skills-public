# Non-destructive Premiere rough-cut workflow

## 1. Isolate the job

Inventory only the folder supplied for the current job. Record absolute paths, sizes, and hashes for the source XML, Jianying SRT, and audio files. Create a unique job directory under the current workspace for analysis files and decisions. Do not reuse analysis output from another episode.

Required inputs are a Premiere-exported XMEML/XML, matching Jianying SRT, and matching audio. One full mix is sufficient for a single-protagonist job. Speaker stems are optional evidence, not a universal requirement.

If more than one plausible XML, SRT, or audio version exists and matching cannot be proven, stop and ask which version is authoritative.

## 2. Inspect the Premiere XML before judging content

Determine and record:

- sequence name and duration;
- exact frame rate as a rational number;
- DF or NDF display format and starting timecode;
- populated video/audio tracks, clip coverage, transitions, nested sequences, multicam resources, speed effects, and locked/disabled state;
- `pproTicksIn` and `pproTicksOut` where present;
- existing labels, effects, transforms, and every authored rotation value;
- media references and whether referenced files exist.

Select split tracks from the actual XML plus the active profile. Background music and unrelated overlays normally remain untouched. If a selected track contains transitions, unsupported retiming, collapsed one-frame subframe audio, unresolved references, or another structure that the splitter rejects, do not guess or flatten it.

## 3. Verify SRT and audio synchronization

Strictly parse the SRT. Use it as a text and coarse-time index, not word-accurate truth. Run `srt_analyzer.py` to find candidates, then review their semantics in context.

Compare recognizable waveform or speech landmarks near the beginning, middle, and end. Estimate a fixed offset only when evidence supports it; check drift separately. Map SRT time into sequence time before frame quantization.

For two genuine synchronized mono microphone WAVs, `audio_analyzer.py` can provide review-only silence and relative-activity evidence. Do not duplicate one mix and claim two independent speakers. For a single full mix, use its waveform for timing and semantic context; do not call absent stems a defect when the job has one protagonist.

Background music, overlap, crosstalk, and room tone invalidate naive silence thresholds. A caption gap is never automatically silence.

## 4. Make semantic decisions

Read surrounding cues and listen around every candidate. Decide by function and replacement evidence:

- Red only when invalidity is high-confidence: an explicit discard/restart, production-only countdown or direction, unrelated crew coordination, a failed start with a confirmed later complete take, a replaced duplicate, or verified long silence.
- Yellow when validity, speaker, overlap, intended emphasis, or replacement relationship is uncertain.
- Keep valid dialogue, useful off-camera questions, intentional repetition, completed answers, and alternate takes unless the production context clearly replaces them.

Director/staff identity alone is insufficient for red. Conversely, a protagonist's own failed take can be red.

Do not let keyword matches decide alone. Phrases such as “再來” can be normal discourse. Require nearby recording context for red.

## 5. Refine exact frame boundaries

Use the synchronized waveform to find the raw invalid interval. Convert through the verified SRT/audio-to-sequence offset and exact sequence frame rate.

Represent decisions as half-open intervals `[start_frame, end_frame)`. Apply the configured inward handle after locating the raw boundary:

- ordinary start: `raw_start + handle_frames`;
- ordinary end: `raw_end - handle_frames`;
- sequence edges may remain at frame `0` or `sequence.duration`;
- reject or merge intervals that collapse or create unsafe one-frame subframe-audio pieces.

Merge adjacent findings when the intervening material is also invalid. Red wins over overlapping yellow. Decisions JSON uses exact frames when possible:

```json
{
  "intervals": [
    {
      "start_frame": 100,
      "end_frame": 250,
      "label": "red",
      "reason": "倒數後重錄",
      "text": "準備，三二一"
    }
  ]
}
```

## 6. Generate the XML

Run the splitter with the profile's labels and target tracks:

```bash
python3 scripts/roughcut_xml.py \
  --xml INPUT.xml \
  --decisions decisions.json \
  --output OUTPUT_自動粗剪.xml \
  --sequence-name OUTPUT_SEQUENCE_NAME \
  --red-label Rose \
  --yellow-label Mango \
  --target-tracks V1,A1,A2
```

Do not use `--force` unless the user explicitly authorizes replacement of that exact generated output. Never point `--output` at the source XML.

## 7. Validate before delivery

Require all of the following:

- XML is well formed and retains its declaration and XMEML doctype;
- sequence duration, frame rate, timecode mode, and per-target-track coverage equal the source;
- no gap, overlap, ripple, reorder, or source-span drift was introduced;
- exact Premiere tick spans remain continuous where present;
- clip IDs are unique and every resource reference has exactly one full definition;
- all non-target tracks and empty tracks remain unchanged;
- kept pieces retain their original labels; only classified pieces use the configured red/yellow labels;
- source and output rotation values are exactly identical, including the case where both contain none;
- the source hash is unchanged;
- delivered file equals the validated local artifact.

If practical, import the result as a new sequence in Premiere for a smoke test. Never claim a Premiere GUI import was tested when only structural validation was performed.

## 8. Report in the conversation

Provide:

- a clickable absolute output path;
- counts and durations for red and yellow;
- exact DF/NDF timecode ranges and concise reasons;
- any uncertainty caused by overlap or missing evidence;
- confirmation that duration and content were preserved and rotation was untouched;
- structural test results and whether Premiere itself was or was not used for an import test.

Do not create a separate report file unless the user requests one.
