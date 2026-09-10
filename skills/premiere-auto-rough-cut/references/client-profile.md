# Client and series profile

Use one profile for the client or series represented by the current workspace. A profile stores reusable facts and preferences, not episode decisions.

## Location and scope

Recommended path:

```text
WORKSPACE_ROOT/.codex/auto-rough-cut.json
```

For a workspace containing several series, place a more specific profile in the series folder. Resolution stops at the current workspace root and selects the nearest profile. Do not search the home directory or another project.

## Create and validate

Create a new profile without overwriting an existing file:

```bash
python3 scripts/client_profile.py init \
  --path WORKSPACE_ROOT/.codex/auto-rough-cut.json \
  --profile-id CLIENT-SERIES-ID \
  --client-id CLIENT-ID \
  --client-name CLIENT-NAME \
  --series-id SERIES-ID \
  --series-name SERIES-NAME
```

Omit both series arguments for a client-wide profile. Validate after every edit:

```bash
python3 scripts/client_profile.py validate PROFILE_PATH
```

## Field meanings

- `profile_id`: stable identifier for the profile.
- `client`, `series`: reusable identity only; `series` is optional.
- `premiere.target_tracks`: tracks that receive synchronized splits and labels. Set this from the actual XML. Do not include background music unless the user explicitly wants it split and labeled.
- `premiere.background_music_tracks`: documentation and overlap guard for music tracks left untouched.
- `red_label`, `yellow_label`: Premiere `label2` category names, not RGB values. Calibrate names against the user's Premiere label palette if exact visual color matters.
- `handle_frames`: inward protection retained at verified boundaries.
- `output_suffix`: appended to the source XML stem.
- `audio.layout`: one of `auto`, `full_mix_single_protagonist`, `separate_speakers`, or `mixed_multitrack`.
- `audio.tracks`: filename patterns and confirmed roles. A `timing_authority` track is suitable for onset/offset refinement for that speaker.
- `silence_seconds`: minimum candidate length. Silence still requires waveform verification and must account for music or room sound.
- `classification.director_policy`: normally `semantic_invalid_only`; role alone never decides red.
- `red_signals`, `yellow_signals`, `preserve_signals`: client-specific editorial conventions. They refine but do not replace semantic review.
- `vocabulary.confirmed_terms`: reusable names, brands, and terminology.
- `notes`: short confirmed reusable facts.

## Learning boundary

Safe profile updates include confirmed speaker names, stable microphone filename patterns, recurring director phrasing, label palette names, and confirmed terminology.

Keep these job-local unless the user explicitly generalizes them:

- exact timestamps or frame ranges;
- one episode's audio offset;
- one episode having only one protagonist;
- one failed take or replacement relationship;
- temporary staff or guest identities;
- conclusions inferred from ambiguous audio.

Use [assets/client-profile.template.json](../assets/client-profile.template.json) as the canonical editable example.
