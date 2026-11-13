---
name: duoyu-pipeline
description: 一键抓取多鱼朋友圈导出 raw markdown（附图旁路+OCR），主会话过滤与逐篇浓缩打分，经共享渲染器产出标注市场态度的 final HTML；更新、刷新、导出或分析多鱼时使用。
version: 4.1.0
allowed-tools: Read, Write, Glob, Bash
argument-hint: "[3d | 7d | YYYY-MM-DD | YYYY-MM-DD:YYYY-MM-DD]"
---

# 多鱼朋友圈统一四阶段流水线（主会话单线程执行）

目标星球 `多鱼朋友圈`（https://wx.zsxq.com/group/15284854485542，ID `15284854485542`）。只操作本仓库 `duoyu/`，不碰大锅饭脚本或 `/Users/melo/doc-Dgf/zsxq_md/`。四阶段：①导出 raw markdown → ②过滤 + 浓缩打分 → ③共享渲染 draft/final → ④共享校验。

**执行纪律（防止慢流程复发）**：本 skill 由主会话一次性顺序执行下面四步，**禁止**把导出/分析/渲染任何一段委派给子 agent 或独立 worker，**禁止** director 平行复读审计，**禁止**中途 interject/纠偏返工。所有校验已脚本化：以脚本退出码与输出为准，不逐次小 eval 复核，不人工复读 raw 核对计数。只有脚本报错时才停下来排查。

## 参数与日期

`$ARGUMENTS`：无参数 = 北京时间最近 3 个自然日（今天及之前 2 天，含首尾）；`Nd` = 最近 N 个自然日；`YYYY-MM-DD` 单日；`YYYY-MM-DD:YYYY-MM-DD` 闭区间。`Nd` 的 start = today-(N-1)，end = today，按自然日不是滚动 72 小时；执行日只统计执行时刻前已发布的帖子。

## 第一步：导出 raw markdown（主会话直接运行）

```bash
cd /Users/melo/src-skills/src-zsxq/zsxq-quant/duoyu
python3 zsxq_export.py --days 3          # 或 --start YYYY-MM-DD --end YYYY-MM-DD
```

- 认证读 `duoyu/.env`（`ZSXQ_ACCESS_TOKEN` 或单行 token，需 0600）；禁止展示/打印 token。
- **不要先删除旧 raw**：成功时 `os.replace` 原子覆盖，失败保留旧版；脚本完整成功才继续。
- 脚本自身完成：API 分页抓取与审计（分页返回数 = 范围外 + 重复 + 范围内数；逐日数；输出数守恒）、topic_id 跨页去重、限流重试、附图下载到旁路目录 + macOS Vision OCR（统一转 JPEG）、失败即中止不写文件。**导出阶段校验以脚本退出码与 stdout 审计为准，无需人工复读。**
- 输出：`/Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_raw_{start}_{end}.md`（MMDD 为执行日）与旁路图片目录 `{同名去 .md}_images/`。标题约定与大锅饭一致：`## ⭐ …| ID \`N\``（星主帖/普通帖）与 `### ❓ ⭐ …| ID \`N\``（q&a，集中在 `## 🙋 用户提问` 章节），头部含 `> 来源：`、`> 日期范围：` 与抓取审计行。
- raw md **不含 base64 图片**：正文、问答、评论、附图占位符 `![图N](images/{ID}-{N}.jpg)` 与逐图 OCR 文本都在 md 内，可直接 `cat` 进上下文。
- 可选 `--min-score <阈值>`（仅用户明确要求时过滤非 q&a 帖；与 `--no-filter` 互斥）。默认不过滤。
- OCR 含糊数字/标的：分析阶段按需查看旁路目录对应原图（`{ID}-{N}.jpg`），不可臆造或直接当成事实。

## 第二步：过滤 + 浓缩打分（主会话直接写三个 JSON）

`TMP=$(mktemp -d)`（三个 JSON 为临时中间产物，渲染校验成功后删除）。`cat` raw md 一次读全（不再有 base64），对每张 `ID \`N\`` 卡片写：

1. `$TMP/filter.json` — 覆盖 raw **全部** N 帖：`{"N": {"keep": true/false, "reason": "…"}}`。排除规则：非星主发起、以提问为主，且求学/大牛均无实质回答或评论 → `keep=false`，reason 写明排除依据（实质回答由你判定；星主仅“收到”/表情/赞同不算实质，普通用户回答不替代星主回答）。归属不清时保守 keep。其余帖 `keep=true`、reason 空串。
2. `$TMP/summaries.json` — **仅 kept 帖**：`{"N": "1–3 句短摘要 HTML 或纯文本"}`（保留关键数据、标的、风险；不复述免责空话）。
3. `$TMP/reviewed.json` — **仅 kept 帖**，按 `__common/scoring_rubric.md` 五档（-2..+2，两个交易日买卖情绪信号）：数组，每条 `{"post_id","score","scope","timescale","evidence"}`；evidence 必填可追溯；被排除帖不打分。

重点星主固定为 `求学`、`大牛`（按结构化作者/评论标记 `**求学**`，不按正文提及）。帖子多为无能量评分/星大派/直播总结类：**无评分也保留，不按 Top N 或评分排序筛选**。覆盖审计：`raw N 篇 = kept A 篇 + excluded E 篇`，A+E 恰好覆盖每张卡片一次（脚本会强制校验）。

## 第三步：共享渲染（draft → final）

```bash
python3 /Users/melo/src-skills/src-zsxq/zsxq-quant/duoyu/render_duoyu.py \
  "/Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_raw_{start}_{end}.md" \
  "$TMP/summaries.json" "$TMP/reviewed.json" "$TMP/filter.json" \
  "$TMP/draft.html" \
  "/Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_{start}_{end}.html"
```

`render_duoyu.py` 是薄封装：`__common.render.render_draft`（focus_owners={求学,大牛}；类别 `host-info`=星主发言与信息帖（📝，浅绿底 `#e7f2e4`）、`qa`=用户问答（🙋，浅粉底 `#fbe3ea`）；classify：q&a→qa 否则 host-info；旁路图片按 `{ID}-{N}.jpg` 转 base64 内嵌；只渲染 kept 帖，summaries 键集合必须恰等于 kept，fail closed）产出 draft；`__common.apply_stances` 按 reviewed.json 给每张卡片注入唯一 `market-stance` 标注行产出 final HTML（分析标注与原帖内容区分，原子写入）。HTML 契约：**恒浅色主题**（无 `prefers-color-scheme: dark`）、**单一时间流无分栏**——全部 kept 帖跨类别按发帖时间**降序**（最新在最上面），每卡 `<article class="post-card" data-post-id data-category data-ts [style="background:{浅色}"]>`，h3 前缀类别图标，类别用图标+不同浅色背景区分（可配置，色相差异大、均为浅色不刺眼）。

## 第四步：共享校验（脚本退出码为准）

```bash
cd /Users/melo/src-skills/src-zsxq/zsxq-quant
python3 -m __common.validate \
  "/Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_raw_{start}_{end}.md" \
  "$TMP/draft.html" "$TMP/reviewed.json" \
  --final "/Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_{start}_{end}.html" \
  --manifest "$TMP/filter.json" \
  --focus-owners 求学,大牛 --categories host-info,qa
```

校验通过（退出码 0，输出 `J/kept/excluded` 与各类别计数，并校验 data-ts 降序）后删除 `$TMP`；失败则保留 `$TMP` 路径供排查并报告错误输出，不交付半成品 final。

## 完成报告与失败处理

校验通过后一次性报告：raw md 与旁路图片目录、final HTML 路径、日期范围、逐日及分页数量、OCR 图片数、`raw N = kept A + excluded E` 守恒、五档篇数（总和 = A）、host-info/qa 类别计数。

任一环节脚本失败即停止并报告其错误输出；不以旧 raw 冒充本次成功导出；分析无法确认星主是否回应时保守 keep 并在 evidence 注明不确定，不虚构。每次重新导出会覆盖旧 raw md 与图片目录，须重做第二至四步。
