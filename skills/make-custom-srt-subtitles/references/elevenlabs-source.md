# ElevenLabs 來源規格

## 必要輸入

- 保存 API 回傳的原始 JSON，不可只保存 SRT 或純文字。
- 保存對應影音檔、檔案雜湊、時長、模型版本與請求參數。
- 將舊 SRT 視為可選差異來源，不得要求剪映 SRT 才能開始。

## 建議請求

中文正式字幕預設使用：

```text
model_id=scribe_v2
language_code=zho
no_verbatim=false
timestamps_granularity=character
tag_audio_events=true
```

先從系列 profile 的 `confirmed_terms`、人物、品牌與 `common_misrecognitions` 產生 keyterms。Keyterms 只提高辨識傾向，不得取代人工確認。

## 隨附辨識工具

先用 dry-run 檢查來源、模式、系列 keyterms 與輸出名稱，不會讀取 Key、建立輸出或呼叫 API：

```bash
python3 scripts/transcribe_elevenlabs.py <影音檔> \
  --mode diarized \
  --output-dir <本集03_語音辨識原始JSON> \
  --profile <系列ID或profile路徑> \
  --dry-run
```

確認本次 API 費用後，移除 `--dry-run` 並加入 `--confirm-cost`。`--mode` 必須明確指定：單人使用 `single`，多人混音使用 `diarized`，每個聲道已隔離單一講者時才使用 `multichannel`。可用 `--job-id` 固定輸出檔名前綴，並用 `--num-speakers` 提供 diarization 講者數上限。

API Key 依序讀取目前程序的 `ELEVENLABS_API_KEY`、`--env-file` 明確指定檔案、使用者私人設定目錄的 `elevenlabs.env`。工具只記錄 Key 的來源類型，不保存或顯示 Key；輸出檔已存在時直接停止，不覆寫原始證據。

### 單一混音

```text
diarize=true
use_multi_channel=false
```

- 視 `speaker_id` 為候選講者，不視為已確認角色。
- 將同時說話、快速換標、背景短句及不合理講者切換列為警示。
- 混音 diarization 不會產生乾淨的獨立人聲軌；被主聲覆蓋的內容可能遺漏。

### 一人一聲道

```text
use_multi_channel=true
diarize=false
timestamps_granularity=character
```

- 以 `channel_index` 建立分軌文字表。
- 驗證每個聲道確實對應單一主要講者；若同聲道含多人，標為低信心。
- 同時發言可保留為不同事件。正式可見字幕是否同時顯示，仍依字幕風格和主要內容判斷。

### 多個獨立音檔

- 確認所有音檔從相同 `00:00:00` 開始；不一致時先校正同步。
- 可將音檔封裝為一人一聲道的多聲道檔案，或分別辨識後依共同時間軸合併。
- 每個事件保留 `source_track`，不可只保留模型的 `speaker_id`。

## 原始資料與正規化層

每個字詞／事件至少保留：

```text
raw_text
traditional_text
start
end
type
speaker_id
channel_index
source_track
confidence_or_logprob（若回應提供）
```

- 原始字串不可覆寫；繁體轉換、專有名詞修正與人工更正另存新欄位。
- 使用臺灣繁體轉換後，再套用同系列已確認的詞彙與常見誤辨識對照。
- 不按字數比例製造逐字時間。缺少實際詞／字時間時標為待複核。
- 模型時間是候選錨點；精準交付仍以已鎖定 `source_track` 的實際開口為權威。

## 產出與追溯

每集至少保存：

```text
<job_id>_elevenlabs_raw.json
<job_id>_elevenlabs_normalized.json
<job_id>_speaker_events.json
<job_id>_AI基準.srt
<job_id>_人工校對用.srt
<job_id>_job.json
decisions.json
```

在 `<job_id>_job.json` 記錄辨識模式：

- `elevenlabs_single`：單人或不需分講者。
- `elevenlabs_diarized`：混音加說話者分離。
- `elevenlabs_multichannel`：一人一聲道，優先模式。

辨識失敗或需要重跑時保留舊原始 JSON，新增版本；不要靜默覆寫。
