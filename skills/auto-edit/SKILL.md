---
name: auto-edit
description: 實拍影片剪輯與字幕工作的通用流程入口，處理分類確認、素材證據、Premiere XML 交換、案件接續及學習回填；不是自動成片引擎。
---

# 影片剪輯通則

每個實拍剪輯案件及字幕子工作必讀本檔與 [工作規格](references/workflow.md)。先提出「影片剪輯 → 客戶 → 類型」分類，取得使用者回答才正式歸類、分析素材或剪輯。回答附案件身份、原話、來源和時間保存；同案身份不變沿用已確認紀錄。技能建置不是新剪輯案件。

依序讀本入口、workflow、catalog指定的客戶與同客戶類型，再讀案件與來源。未知客戶／類型先提出新分類。公開catalog只有合成示例，實際客戶規則及案件必須放使用者的私人維護來源，不提交到這個公開repo。

讀 [案件契約](references/cases.md)，從assets/case.template.json建立新manifest。使用唯讀 `scripts/case_context.py inspect CASE.json`檢查中繼資料；有真實回答後再 `check CASE.json --workspace WORKSPACE --bindings BINDINGS.json`檢查來源hash。工具不證明回答真偽，也不執行剪輯。沒有editflow.py或通用一鍵精剪引擎。

實際工作與舊工具的路由見 [相容性](references/compatibility.md)，人工學習讀 [回填規格](references/learning.md)。每條規則有來源、scope、primary及狀態，候選不冒充永久偏好。編輯旁白不當ASR逐字稿；使用者特定格式不能被一般去標點工具靜默刪除。

使用前fetch此公開倉庫，乾淨工作樹只fast-forward，保留自行修改。個人API授權、客戶資料和素材留私人位置；完成修改後驗證及正常提交同步，不強推。安裝auto-edit也須安裝所需的粗剪或字幕skill；若單獨安裝旧入口，本依賴需一併安裝。程式定位使用已安裝連結的真實位置；Python3.10+，媒體依賴另按專責skill準備。Premiere Pro 2026實機匯入與結構檢查分開記錄。
