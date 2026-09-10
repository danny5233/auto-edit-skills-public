# JSON／JSONL 剪辑点词语归属增量

本增量只负责在主字稿已经由目标电脑的字幕 Skill 校正、断句并套用偏好后，重新判断指定文字位于 Premiere XML 剪辑点的前段或后段，以及定位重点字卡首尾字时间。目标 Skill 原有的繁简体、用词、字数、断句、讲者、客户与系列偏好仍是权威；不要用本增量覆盖或重写那些规则。

## 输入与顺序

1. 同一剪辑版本的 Scribe v2 字元 JSON，或每行一个 `{text,start,end}` 事件的 JSONL。
2. 已由目标 Skill 完成文字校正的主 SRT。
3. 同版 Premiere XML；若是多声道 Scribe，另确认真正讲者的 `channel_index`。
4. 可选的重点字卡 selections JSON。

先执行安装后的本机映射工具：

```bash
python3 scripts/json_cut_map_scribe.py \
  --scribe <同版.json或.jsonl> \
  --srt <校正版主字幕.srt> \
  --output <校正版字元映射.json>
```

多声道有多条 `transcripts[]` 时加入 `--channel-index <n>`。映射不会呼叫 API：相同字标为 `exact`；短等长错字修正标为 `substitution` 并继承原发声槽；新增或无法一一证明的字标为 `unmapped`，时间保持 `null`，不得插值。

再执行剪辑点与字卡判断：

```bash
python3 scripts/json_cut_align_xml.py \
  --srt <校正版主字幕.srt> \
  --alignment <校正版字元映射.json> \
  --xml <同版Premiere.xml> \
  --output-srt <XML逐字重切候选.srt> \
  --report <XML逐字判断报告.json> \
  --sequence-name <序列名称>
```

需要重点字卡时另加：

```bash
--cards <字卡选择.json> --cards-output <重点字卡.srt>
```

## 不得猜测的情况

- 剪辑点左右不是相邻且都有时间的两个字。
- 切点附近含 `unmapped` 字，或原辨识在两字之间有被删除的声音事件。
- 切点落在字元发声内、受保护词内，或最近字元边界超过 XML 前后 5 帧。
- 指定字卡范围内任何一个字没有时间。
- XML、SRT 与 JSON／JSONL 不是同一剪辑版本。

上述情况保留目标 Skill 原本的字幕并记录原因。只有会影响实际交付的未解点，才由剪辑师决定人工回听、局部定位或另外核准付费 Forced Alignment；不得自动重跑整支影片。

`cut_inside_spoken_character` 只能表示 Scribe 的字元时间区间与 XML 切点重叠，不能直接证明画面切点真的落在词中。工具会在 `semantic_review_candidate` 同时列出「该字前」与「该字后」两个文字归属候选。逐点检查：如果重叠字是左侧完整语意单位的最后一字，或右侧完整语意单位的第一字，可在原音、讲者与 hidden event 均安全时采用 XML 切点；如果会拆开完整词、否定／条件结构或其他未完成语法，则维持不采用。不得用字元拖尾时间一票否决，也不得把候选自动当成已采用。

短反应不能只因显示时间很短就拒绝。若分轨与语意确认它是独立回答或换讲者边界，可以单独成段；同一讲者黏附于后句、切开会破坏条件结构或反应字本身仍跨越切点时则不拆。

## 实际专案测试报告

测试时至少保存：来源雜湊、mapping coverage、`exact`／`substitution`／`unmapped` 数量、额外 API 呼叫与成本、每个 XML 点的采用／拒绝原因、采用点前后文字，以及每张字卡的首尾字时间。正式采用的 XML 点仍需回听原音。
