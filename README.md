# auto-edit-skills-public

這裡提供通用原始碼與使用規則，不包含作者的客戶系列設定、個人 API 授權或實際案件素材。

| Skill | 用途 | 狀態 |
|---|---|---|
| subtitle-tools | 剪映 SRT 校正、切句、驗證與人工回填 | 維護中 |
| make-custom-srt-subtitles | ElevenLabs JSON 轉繁中字幕及時間對齊 | 維護中；API 需自備金鑰與授權 |
| premiere-auto-rough-cut | Premiere XML 非破壞性粗剪與標色 | 維護中 |
| auto-edit | 分類確認、分層與唯讀案件檢查 | 可用流程入口；無自動成片引擎 |
| story-to-handdrawn-video | 手繪故事影片 Skill 與 wrapper | 需另備相容 renderer 專案，非獨立影片產生器 |

## 安裝

需要 Git 與 Python 3.10+。字幕影音處理可能另需 ffmpeg；ElevenLabs 呼叫需相應 SDK。

```bash
git clone https://github.com/danny5233/auto-edit-skills-public.git
cd auto-edit-skills-public
python3 scripts/install.py subtitle-tools
```

安裝器在 `~/.codex/skills/` 建立指向本 checkout 的連結。既有不同內容不會被覆寫。
其他 Skill 將最後的名稱換掉；只安裝所選入口與必要共用依賴。重新開啟 Codex 任務後使用。

## 更新與協作

每次使用前在 checkout 中執行 `git fetch origin main` 並檢查 `git status`；沒有本機變更時以 `git merge --ff-only origin/main` 更新，然後重讀 SKILL.md。
若自行改過，先保留、比較與整合差異，不使用強制覆寫。
歡迎透過 Issue 或 Pull Request 分享通用改進；不要提交金鑰、影音、客戶設定或逐字稿。
維護者的完整個人版使用另一個私人倉庫，不會把私人歷史 merge 到此倉庫。

本公開版的字幕排版預設只是通用起點；依使用者自己的系列設定調整。
`--profile` 可指定私人工作區的設定檔；付費 API 不繼承作者的授權。

## 驗證

```bash
python3 skills/subtitle-tools/scripts/test_srt_style.py
python3 skills/subtitle-tools/scripts/test_revision_report.py
python3 skills/make-custom-srt-subtitles/scripts/test_srt_style.py
python3 skills/make-custom-srt-subtitles/scripts/test_transcribe_elevenlabs.py
```

## 實拍剪輯架構

`auto-edit` 現為可用的分類確認、分層與案件檢查入口，沒有自動成片引擎。舊粗剪／字幕入口安裝時一併安裝auto-edit。公開層級只有合成示例，客戶與案件資料須留私人來源。`case_context.py`只做唯讀檢查，不能證明使用者回答的真偽，不能取代Premiere匯入測試。
