# 案件與來源契約

`scripts/case_context.py` 是唯讀檢查器，沒有 init/record/revise 或剪輯命令。使用 `assets/case.template.json` 建立案件 JSON，首次存檔使用新路徑，勿覆寫別案。單集完整資料留 workspace 的 `.editing/cases/<case_id>/` 或既有 job 目錄；舊字幕 job 保持原檔，以 `legacy_job` 連結，不改名、不搬移、不改其 decisions。

## 必填欄位

- schema_version=1；case_id、episode_id、client_id、type_id 為穩定識別；title 為顯示名稱。
- classification.path = [影片剪輯, 客戶顯示名, 類型顯示名]；status=proposed 或 confirmed。
- confirmed 必須附 confirmation 的 case_id、client_id、type_id、path（與當案相同）、quote、source、at。來源指向實際使用者訊息，代理負責核對回答內容；JSON 字串本身不是人類批准證明。
- scope 記允許階段與本次任務限制；status 可為 intake/in_progress/delivered/final。final 另需 final_confirmation，綁 case_id、版本 version、quote、source、at。分類確認不等於剪輯授權或定案。
- sources 各含 id、case_id、role、root、path、version、sha256。role 為 media/timeline/audio/transcript/reference/evidence。除 reference 外 case_id 必須匹配；reference 不能進本集可剪來源。root=workspace 或 bindings 定義的具名根目錄。path 必須為相對路徑，不可有 `..`、磁碟機字首或越過根目錄的 symlink。
- dependencies/交付記錄置於 artifacts，每項 id、status、depends_on（來源 ID→hash）、path；hash 改變標 stale，舊來源不能被靜默覆蓋成新版本。重新核對後另存新版記錄。

分類未確認時 check 在讀取來源前停止。inspect 可讀中繼資料、給出應讀層級與分類狀態，不宣稱可開始剪輯。check 另外要求 sources 非空，核對每個檔案 hash、當集隔離與交付 dependencies；缺檔明記不可接續，不搜尋另一集替代。

## 版本與其他裝置

同案換主軸保存舊 manifest，建立新 revision、更新 sources hash，舊依賴保留以便偵測 stale。分類身份未变且已有真實回答可沿用。新集須新 case_id 和新回答，不能複製 approval。

bindings 是每台裝置私有 JSON，例如 `{"media": "/mounted/media-root"}`；不進公開或私人知識倉庫。工作目錄用 `--workspace` 明確指定。Windows 可傳 `{"media":"D:\\Media"}`，路徑解析由該平台 Python 處理；XML 內 file URL 重新連結須在新輸出副本進行，這個檢查器不修改 XML。不要對原文全域字串取代。

GitHub 同步規則與精簡案件索引，**不會同步素材或字幕交付**。另台裝置需使用者媒體儲存及同版案件資料；缺檔只能閱讀知識與接續說明。私人 `references/case-index/` 只保存已審查的簡要狀態、相對證據位置與 hash，不存原字幕或影音。
