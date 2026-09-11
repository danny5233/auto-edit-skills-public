# 接收上游已確認案件

`auto-edit` 負責確認客戶、剪輯類型與案件，並選定適用的字幕 profile。`make-custom-srt-subtitles` 接收這份結果，**不建立客戶／系列索引、不查詢分類 catalog、不重新篩選客戶，也不重問已回答的分類**。

`subtitle_context.py` 共用上游 `case_context.py` 的身份、既有確認及來源版本檢查，沒有呼叫其 `load_layers` 分類選擇器。這是核對已傳入的資料，不是再次分類。若記錄不一致，回報哪份案件或來源不符，先回查上游；只有上游真的缺少必要資訊時才补充，不讓使用者重答已有確認。

## 上游傳入什麼

沿用原 `auto-edit` case JSON 的 `case_id`、`client_id`、`type_id`、`episode_id`、`version`、已確認的 `classification` 與 `sources` 白名單。字幕子工作在 `scope.stages` 中為 `text`；既有授權可依原訊息填入，不再要求一次批准。`classification.confirmation` 原樣沿用，不捏造新回答。

上游完成類型層與 profile 適用範圍判斷後，在該 case 加入 `subtitle` 擴充；這是本案的已解析結果，不是另一份客戶清單：

```json
{
  "subtitle": {
    "job_id": "example-ep01",
    "series_id": "example-series",
    "subtitle_content_type": "long_form",
    "profile": {
      "root": "skill", "path": "references/series-profiles/example-series.json",
      "sha256": "填入實際SHA256"
    },
    "learning_registry": {
      "root": "workspace", "path": ".subtitles/learning-registry.json"
    },
    "delivery": {
      "directory": {"root": "media", "path": "ep01/Ai字幕"}
    }
  }
}
```

例名與路徑僅示範結構；公開版不附客戶 profile。上游可指定工作區 `clients/<client_id>/<series_id>.json`，或引用原主要 profile 位置。profile 有 `client_id`／`type_id` 時核對相符；舊 profile 只有 `series_id` 時依上游明確綁定，不另外搜尋別名。

`subtitle_content_type` 是字幕短／長格式，與剪輯 `type_id` 分開。上游有值就直接沿用；省略或 null 時用共用及原系列規則，報告標示格式尚未指定，不按秒數、方向或剪輯類型猜測。只有確實影響本次處理的缺項才集中補充。

没有專屬 profile 可明確設定 `profile: null`，使用通用預設；沒有系列可省略 `series_id`。不能因同品牌或相似片名借用其他系列。新系列設定放私人工作區；既有 profile 保留原位及原作用範圍。

## 字幕端只保存單集處理狀態

在本集 job 目錄新增 `subtitle-context.json`，不建立 `.subtitle-workspace.json`：

```json
{
  "schema_version": 1,
  "case_id": "example-case-01",
  "job_id": "example-ep01",
  "upstream_case": {
    "root": "workspace", "path": ".editing/cases/example-case-01/case.json",
    "sha256": "填入實際SHA256"
  },
  "source_ids": ["main-audio", "scribe-original"],
  "decisions": {
    "root": "workspace", "path": "jobs/example-ep01/decisions.json",
    "sha256": "填入實際SHA256"
  },
  "evidence_dir": {"root": "workspace", "path": "jobs/example-ep01/evidence"}
}
```

`source_ids` 只引用上游白名單 ID，不重複維護来源路徑與版本。至少包含本案 audio/media；正式製作仍要求 Scribe 原始 JSON（上游角色 `transcript`）與同版影音。收件時可尚無 Scribe；API 完成後把原始 JSON、來源依據及 hash 登記上游，確認差異後更新 context 的 upstream hash。XML 為 `timeline`。主影音、Scribe、XML 的剪輯版本必須一致；case 的管理版號可以獨立遞增。

`case_id` 與 `job_id` 用來核對交接身份；客戶、類型、系列、集數、交付位置皆直接讀上游。context 不另設 profile、客戶清單或交付覆蓋。舊 context 若有重複身份欄位，必須與上游一致。

每集保留獨立 evidence_dir 和 decisions，不沿用前集。可選 `legacy_job` 使用 root/path/sha256 結構引用原 `*_job.json`；核對該檔已有身份，不搬移、不改名、不改原人工鎖定。舊案沒有新式交接資料時維持原 `--profile`／`--decisions` 指令；有真實上游確認才補建交接，不為了通過工具偽造分類答案。

首次 decisions 可為空物件；不代表人工與品質檢查已完成。人工回填後先保留原檔與 AI 基準、核對全部既有 exact，再更新 decisions hash。profile、來源或 case 改版亦先回看差異，再更新上游或本集 pins，不能批次重算 hash 掩蓋變動。

## 路徑與跨裝置

`workspace` 是命令指定的專案工作區；`skill` 是已安裝的本字幕 Skill，僅供讀取 profile。外接素材或舊工作目錄使用私人 `--bindings device-roots.json`，例如 `{"media":"/absolute/client-project","profiles":"/absolute/private-profiles"}`。bindings 不可覆寫 workspace／skill。來源使用上游相同 bindings。

所有 path 為相對具名 root 的路徑，不能含 `..`、磁碟機字首或越界 symlink。可變 context、decisions、學習紀錄及成品不放已安裝 Skill。GitHub 同步不搬運影音、單集字幕或私人工作區。

## 共用指令

```bash
python3 scripts/subtitle_context.py check <subtitle-context.json> --workspace <工作區> --bindings <裝置根目錄.json>
python3 scripts/transcribe_elevenlabs.py <同版音檔> --mode diarized --workspace <工作區> --context <subtitle-context.json> --bindings <裝置根目錄.json> --dry-run
python3 scripts/srt_style.py validate <本案候選.srt> --workspace <工作區> --context <subtitle-context.json> --bindings <裝置根目錄.json>
```

沒有外部 root 時省略 bindings。兩支既有工具使用同一解析器；交接模式不另傳 `--profile`／`--decisions`。辨識 `--output-dir` 可省略，沿用本集 evidence_dir；明確傳入時必須相同。費用與來源上傳授權仍沿用原 ElevenLabs 流程，唯讀交接檢查不授權 API，也不生成字幕。

## 字幕偏好與學習

profile 的 `subtitle_preferences` 保留原系列偏好；可加 `subtitle_preferences_by_content_type.short_form`／`.long_form` 保存同系列已確認的格式差異。只在記憶體合併本案已指定格式，不改寫 profile。

短／長通用節奏依本案格式讀 `content-types/short-form.md` 或 `long-form.md`。最新本集要求與人工鎖定仍優先；機械 validator 目前只解讀既有 `tail_fill_threshold_ms`，其他語意偏好由代理讀取，不宣稱已被程式驗證。來源、講者、禁止插值及人工鎖定不能透過偏好放寬。

候選學習與衝突寫入上游傳入的 `learning_registry`，未指定時使用工作區 `.subtitles/learning-registry.json`；既有歷史紀錄保留原位供比對。已確認的系列規則回寫同一主要 profile，不複製第二份；通用修復經升格門檻與回歸後更新 canonical Skill。沿用 `learning-loop.md` 與 auto-edit 的通則／客戶／類型／單集作用域。

## 交付

上游依 auto-edit 交付位置規則確認本客戶當集目的地，字幕端直接沿用 `subtitle.delivery.directory`。寫檔前檢查實際目錄、掛載與可寫性；位置不明或不可寫時回報缺口，不改存工具工作區，不建立假的掛載目錄。使用本案既有字幕子目錄，不另發明分類層級。

context 的 `deliverables` 陣列每項包含 `role`、`job_id`、交付目錄內相對 `path` 及實際 `sha256`。必要角色為 `final_srt`、`review_srt`、`ai_baseline`、`report`，各自獨立檔案；上游 `subtitle.delivery.required_roles` 可增加角色。候選 SRT 放 evidence_dir；交付目錄內的 SRT 要先登記角色與 hash，避免共用目的地時拿錯檔。

```bash
python3 scripts/subtitle_context.py delivery <subtitle-context.json> --workspace <工作區> --bindings <裝置根目錄.json>
```

這只檢查本集交付檔的齊全、非空、路徑與 hash，不代替 `srt_style.py validate`、原音／講者複核、集中待確認或修訂差異審查。報告清楚區分交接／檔案驗證與內容品質。
