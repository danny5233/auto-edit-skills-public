# 切點審查落實與人工回填

讀取 XML 後，先分清「啟用軌道的邊界候選」與「最終合成可見硬切」。持續的小 Logo 不會遮住所有底層切點；全畫幅上層片段可能遮住底層切點。未檢查合成遮蔽時，報告只稱軌道候選，不能稱全部可見。

`json_cut_align_xml.py` 是候選產生器。即使使用舊有 `--post-snap-audio-check passed`，其輸出仍不取代逐點審查，也不能直接稱正式成品。採用與拒絕都要完成判斷；不得把所有未決候選清空成 `xml_edit_checks: []` 後以格式通過交付。

## 正式套用

用 `reviewed_cut_release.py --srt master.srt --xml timeline.xml --sequence-name main --alignment mapping.json --review review.json --output final.srt --report release.json` 套用已完成的逐點判斷。`master.srt` 必須是已校字的同版底稿。

`review.json` 根欄位：

- `mode: reviewed_boundaries`、`reviewer`、`evidence`。
- `input_srt_sha256`、`xml_sha256`、`alignment_sha256`，皆是實際輸入檔案的 SHA256。
- `protected_terms`：本次受保護詞。
- `cuts`：每個 XML 軌道候選恰好一筆；不可漏列或重複。

每筆包含 `frame`、`action`、`reason`。`action` 可以是 `adopt`、`keep` 或 `not_visible`。`keep` 的理由描述實際語意／聲音／序列邊界原因；`not_visible` 記錄合成遮蔽證據，不能由沒有字幕命中反推不可見。

`adopt` 另外需要：

- `boundary_index`：校字全文去空白後的零起算字元邊界。
- `speech_boundary`：經局部回聽確認的秒數，距切點不得超過 5 幀。
- `visible_cut_confirmed`、`semantic_boundary_confirmed`、`speaker_boundary_preserved`、`hidden_event_clear`、`post_snap_audio_reviewed` 均為 true。
- `audio_evidence`：實際回聽者、同版原音片段與判斷；工具不會代替人或代理回聽，不得為通過驗證虛填。
- 需要把既有兩段的字重新分配時，指定 `replace_boundary_index`（原兩段共享的字元邊界）。舊字幕邊界可能離 XML 超過 5 幀；5 幀限制作用於重新確認的發聲邊界，不作用於舊錯誤邊界。這可避免只拆出一個極短碎段、卻留下舊錯誤分段。

Scribe 區間蓋住剪輯點時，檢查 `semantic_review_candidate` 的前後兩種歸屬。重疊字若是前段完整詞末字或後段完整詞首字，可在聲音證據充分時採用；不能把模型區間重疊直接當成最終拒絕。也不能因文字以「在」「跟」「對」結尾就一律拒絕；檢查是否真的拆開詞、否定或條件結構。

工具會檢查版本、完整候選覆蓋、保護詞、5 幀限制、未映射文字、重疊，以及後面的操作有沒有破壞前面採用的切點。輸出報告綁定實際成品 SHA256。仍須以 `srt_style.py validate` 檢查正式成品的閱讀與事件規則；同幀命中數不是越多越好，也不等於聲音全數驗證。

## 人工稿回填

內容負責人明確提供同集人工稿時，使用同一工具的 `human_reference` 模式。審查 JSON 保留 `input_srt_sha256`、`xml_sha256`、`reviewer`、`evidence`，另加 `human_srt` 與 `human_srt_sha256`；不需 alignment。工具原樣保存人工稿位元組並產生所有 cue 的 `start`、`end`、`text` 鎖定。將報告的 `exact` 合併回本集 decisions，保留先前版本與來源證據，再重跑驗證。

此模式是 `human_reference_replay`，只證明本集人工內容與時間沒有被改回。不得以人工稿重放的 100% 相符宣稱模型已能盲測做到同樣水準；未見過答案的新素材才是獨立生成評估。

## 系列知識與辨識提示分開

`confirmed_terms` 可以保存以前確認過的品牌與來賓，但不代表每集都出現。若 profile 有 `transcription_keyterms`，只用這份穩定提示名單加本集明確的 `--keyterm`；空陣列表示不自動提示舊集詞彙。沒有此欄位的 profile 保持既有相容行為，但誤辨識對照只可加入正確值，不能把錯字鍵當成期待辨識詞。

人工稿拼法只鎖定本集，不把兩個不同品牌建立全系列互換，也不批次替換同稿中有不同人工選擇的同音字。省略填充音與自然短句屬語意判斷：保存本集結果與正反例，不使用「一律刪除所有語助詞」或固定提前若干幀。

## 產稿必經入口與成品驗證

收到同版 XML 時，校字與講者歸屬完成後先建立可靠字元映射，再執行：

```bash
python3 scripts/xml_cut_pipeline.py prepare --srt master.srt --alignment mapping.json --xml timeline.xml --sequence-name main --output-dir cut-review-v1
```

這個入口會實際執行剪輯點候選產生器，保存原稿命中表、候選 SRT、逐點證據與完整 `review.json`。每個切點初始都是 `pending`，不能因候選工具成功便當作完成回聽。多講者不可選單一 ASR 聲道套全片；先完成每段主聲源歸屬，再提供同版映射。缺字元證據不得插值。

逐點確認後執行 `xml_cut_pipeline.py release`，參數與 `reviewed_cut_release.py` 相同；正式產稿必須使用它的輸出。只將時間碼四捨五入到影格、只寫候選 JSON，或把 `xml_edit_checks` 留空，都沒有完成剪輯點對齊。

`adopt` 的 `endpoint` 預設為 `change`（前後字幕共同切換）；長停頓兩側可指定 `start` 或 `end`，只改指定字幕端點，不把空白填滿。`boundary_index` 仍是對應端點的全文字元索引。所有模式同樣受 5 幀、詞彙、主聲源及回聽證據限制，不能用尾端模式繞過它們。

```bash
python3 scripts/xml_cut_pipeline.py verify --srt final.srt --xml timeline.xml --report release.json
```

驗證讀取實際寫出的 SRT，核對輸入 XML／成品 hash、完整切點表與每個已採用端點。`timebase=30` 且 `ntsc=TRUE` 的精確幀率為 `30000/1001`，報告保存分子與分母；30 fps 不可代換。同幀四捨五入只用於識別 XML frame，命中必須距真實切點不超過 1 毫秒（容納 SRT 匯出取整），不能將同格內任意時間視為精確命中。

已接入 `subtitle_context.py delivery` 的工作，只要選定來源含 XML timeline，就必須登記 `xml_cut_release` 交付角色；工具自動核對它與 `final_srt`、當版 XML。缺報告、候選報告、未處理切點或後續改掉已採用端點都阻擋交付。未接入 context 的舊案也必須直接執行上述 `verify`，在 job 記錄報告 hash 與結果，不重建案件分類。

人工稿原樣重放仍標記 `human_reference_replay`。它不能證明每個人工剪輯點符合通用 5 幀門檻，也不能冒充自動生成命中率。沒有主聲源／回聽證據時，保留待確認，不為追求命中數硬吸附。
