# 舊入口轉接

實拍剪輯或其字幕子工作先讀auto-edit及workflow並確認「影片剪輯 → 客戶 → 類型」。同案子階段沿用回答，不複製別案確認。

- premiere-auto-rough-cut：保留只切割標色、不刪除或ripple的契約。原路線要求剪映SRT；使用者明確授權的其他同版字幕來源須記實際origin，不冒稱剪映。nearest profile只提供候選，必須核對確認的客戶與類型。
- subtitle-tools：保留必要剪映SRT與同版影音要求；缺少不從零ASR。
- make-custom-srt-subtitles：保留ElevenLabs原始JSON與同版影音、成本確認、講者與詞級驗證；不因整合取消dry-run或授權。
- 舊job/profile/decisions保留原位置，以ID引用其primary，進行中案件不搬移或重跑。類型是剪輯分類；short_form/long_form是另一个字幕維度，不能按秒數猜。
- POV編輯旁白、唱腔與人工格式例外需保留。舊validator不支援时，分角色檢查且明記未覆蓋項，不為機械通過改掉人工內容。
- 文字／圖片生成影片、Remotion、HyperFrames仍為獨立路線，不替代Premiere實拍剪輯工具。單集腳本未參數化前不當通用引擎。

每次學習按learning.md判斷通則、客戶、類型與單集主要位置；原字幕learning-loop的鎖定／機械差異驗證仍保留，跨路線共用採較嚴的跨獨立客戶證據要求。
