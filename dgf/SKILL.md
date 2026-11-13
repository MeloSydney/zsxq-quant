---
name: dgf-pipeline
description: 一键抓取并全量分析大锅饭知识星球；导出 raw Markdown，经统一过滤/打分/渲染为按时间降序单一时间流的恒浅色 HTML；更新、刷新、导出、分析或总结星球时使用。
version: 3.1.0
allowed-tools: Read, Write, Glob, Bash
argument-hint: "[日期或日期范围] 例如: 3d、7d、2026-05-01:2026-05-12"
---

# 知识星球完整工作流（统一四阶段，单会话执行，禁止分段委派）

抓取 → 过滤 → 打分 → 渲染，全程 API 抓取，不需要浏览器。打分/过滤/渲染/校验全部复用共享层 `__common/`（与 duoyu-pipeline 同一套算法）。**输出目录 `/Users/melo/doc-Dgf/zsxq_md/` 只保留两类文件：raw Markdown 与正式 HTML；summaries/reviewed/draft 是临时中间件，写入 `mktemp -d` 临时目录，发布+校验通过后随目录一并删除，绝不落盘 zsxq_md。** 整个流程在本会话内一次完成：

1. **导出**：主会话直接跑导出脚本（脚本自带审计输出）；
2. **过滤**：DGF 为 pass-all（恒等过滤，全部 keep，不做评分/Top-N 过滤）；
3. **打分**：主会话按共享 `__common/scoring_rubric.md` 对**每帖**人工审阅五档市场态度（两个交易日买卖情绪信号），写 reviewed JSON；并撰写每帖 summary；
4. **渲染**：跑 `dgf/render_dgf.py`（内部调 `__common/render.py` 生成草稿 + `__common/apply_stances.py` 注入态度行）一步产出正式 HTML，再跑 `__common/validate.py` 一次完成全部校验，通过后删除临时目录。

**禁止**：把四阶段拆给子 agent 或独立 worker；director/主控平行复读审计；已校验内容二次重验；对中间产物逐次小 eval 校验（校验一律脚本化一次完成）；执行中途 interject 纠偏返工。导出失败（401/限流）时不进入后续步骤，直接提示用户。

## 输入参数

`$ARGUMENTS`：单日 `2026-05-05` | 范围 `2026-05-01:2026-05-05` | 相对 `1d/3d/7d` | 留空默认最近 3 天。

## 第一步：导出 raw

```bash
date +%Y-%m-%d   # 得 today 与 MMDD
rm -f /Users/melo/doc-Dgf/zsxq_md/{MMDD}_大锅饭_raw_{start}_{end}.md   # 不用缓存
python3 /Users/melo/doc-Dgf/zsxq_export.py --days <N>          # 或 --start X --end Y
```

- token 自动读 `dgf/.env`（`zsxq_access_token`，过期时提示用户从浏览器 Cookie 更新）。
- 脚本契约（内置，勿在会话中复验）：全量评论走评论接口（count=20、`end_time` 游标、`comment_id` 去重），失败即中止不生成不完整 raw；跨页按 `topic_id` 去重；`q&a` 帖完整保留并集中写入文末「🙋 用户提问」章节；每帖标题末尾 `| ID \`{topic_id}\``；不做评分过滤；限流 code 1059 时等待重试。
- 输出：`/Users/melo/doc-Dgf/zsxq_md/{MMDD}_大锅饭_raw_{start}_{end}.md`。

## 第二步：过滤（DGF = pass-all）

DGF 不做任何过滤：全部帖子 keep（无评分过滤、无 Top-N、不因无评分丢弃）。`render_dgf.py --pass-all` 即恒等过滤，无需单独 manifest 文件。

## 第三步：主会话浓缩 + 打分（临时目录）

0. `TMP=$(mktemp -d)`——本步全部中间件只写入 `$TMP`，zsxq_md 根目录不得出现中间件。
1. `bash cat` 一次性读入整个 raw 文件，理解每帖正文、标签、附件、评论与嵌套回复。
2. 读取 `dgf/blackwords`（每行 `词 - 解释`）与共享 `__common/scoring_rubric.md`（统一五档 rubric）。
3. 为**每个** raw 帖子撰写 2-5 句浓缩 summary（关键数字/公司/代码加粗；无信息内容删除；截断须标注；首次出现黑话括注解释），写 `$TMP/{MMDD}_大锅饭_summaries_{s}_{e}.json`（`{post_id: summary_html}`）。
4. 按共享 rubric 人工审阅**每帖**五档市场态度（定位为**未来两个交易日买入/卖出的情绪信号**），写 `$TMP/{MMDD}_大锅饭_reviewed_{s}_{e}.json`（格式见下）。

### 审阅 JSON 契约（共享 `__common/stance.py` schema）

```json
[{"post_id":"卡片 data-post-id","score":0,"scope":"具体范围","timescale":"未来两个交易日","evidence":"一句可追溯依据"}]
```

- 每个 raw 帖子（含未回答提问）恰好一条；`score` 整数 -2..+2；其他字段非空；不推断、不从能量分换算。
- 五档语义、输入边界、evidence 要求、冲突处理、研报转述规则一律以 `__common/scoring_rubric.md` 为准（不在本文件重复维护）。

## 第四步：渲染 + 一次性校验 + 清理

```bash
cd /Users/melo/doc-Dgf/zsxq_md
python3 /Users/melo/src-skills/src-zsxq/zsxq-quant/dgf/render_dgf.py \
  {MMDD}_大锅饭_raw_{s}_{e}.md \
  "$TMP/{MMDD}_大锅饭_summaries_{s}_{e}.json" \
  "$TMP/{MMDD}_大锅饭_reviewed_{s}_{e}.json" \
  "$TMP/{MMDD}_大锅饭_draft_{s}_{e}.html" \
  {MMDD}_大锅饭_{s}_{e}.html --pass-all
python3 -m __common.validate \
  {MMDD}_大锅饭_raw_{s}_{e}.md "$TMP/{MMDD}_大锅饭_draft_{s}_{e}.html" \
  "$TMP/{MMDD}_大锅饭_reviewed_{s}_{e}.json" \
  --final {MMDD}_大锅饭_{s}_{e}.html \
  --focus-owners 三线文案大锅饭 --categories xingdapai,host-posts,questions
rm -rf "$TMP"    # 仅在校验通过后执行
```

- `render_dgf.py`：调 `__common/render.py` 机械注入 CSS（恒浅色）/header/五档图例、互斥分类（`q&a`→用户提问；标题或正文首行去 `**` 以「星大派」开头或标题含「直播总结」→星大派；其余→星主普通）、**单一时间流**：全部 kept 帖跨类别按发帖时间降序（最新在上）、卡片 h3（类别图标+标题）/meta/`data-category`/`data-ts`/类别浅色背景、评论区逐字渲染（多行合并、`回复` 对象、focus-owner `三线文案大锅饭` 黄名 `class="focus-name"`、无评论 `no-comments`）、每卡 raw-block 逐字转义、header 各类别计数与合计；再调 `__common/apply_stances.py` 注入经审阅的态度行（`market-stance` + `data-source="analysis"`）。**LLM 只产出 summary 与 reviewed JSON；raw-block/评论区/CSS/时间流排序/计数/态度注入一律脚本完成，禁止逐字转写 raw。**
- `__common/validate.py` 单次完成全部校验：topic ID 唯一、卡片集合==kept、每卡 data-category ∈ 类别集合、各类别计数之和==kept J、data-ts 降序、逐帖评论条数、focus-owner 条数、每卡 1 summary/1 收起 raw-block/1 态度行、无全局开关、reviewed 五档/字段/ID 一致、raw-block 与原文逐字一致。退出码非 0 时按报错修复后重跑，不做其他人工审计。
- **失败处理**：render/validate 任一失败 → 不删 `$TMP`、不产出半成品正式 HTML，在最终汇报中报告 `$TMP` 路径便于排查；修复后从第三步重跑。
- 交付物命名统一 `{MMDD}_大锅饭_*`；raw md 只读不改；zsxq_md 只产出 raw md 与正式 HTML，不产出分析 Markdown，也不留任何中间件。

### HTML 契约（由 `__common/render.py` 实现，人工核对时参考）

- 自包含：`<!doctype html>`、UTF-8、viewport、CSS 全内嵌，可离线打开；**恒浅色主题**（`#f4f1ea` 系，无 `prefers-color-scheme: dark` 深色变量组），定义 `--line --gold --summary --text --muted`。
- **单一时间流，无分栏**：不再使用 `<section>` 专栏；全部 kept 帖跨类别按发帖时间**降序**（最新在最上面，新帖对当前市场判断影响更大）；能量评分仅展示不影响排序。类别用图标+浅色背景区分（可配置，三色色相差异大、均为浅色不刺眼）：星大派 `xingdapai`（🌟，浅黄底 `#fdf0c4`）、星主普通 `host-posts`（📝，浅绿底 `#e7f2e4`）、用户提问 `questions`（🙋，浅粉底 `#fbe3ea`）。
- 每卡：`<article class="post-card" data-post-id="{topic_id}" data-category="{类别}" data-ts="YYYY-MM-DD HH:MM" [style="background:{浅色}"]>` + h3(类别图标+标题≤15字) + meta(发布时间·能量评分·Topic ID) + `.summary` + `.comments`(逐字、`class="comment"`/`comment reply`、focus `class="focus-name"` 金色文字无底色) + `.market-stance`(注入) + `<details class="raw-block">`(默认收起、`white-space: pre-wrap`)。
- **评论逐字保留**；**每卡片独立「查看原文」**；**页面不得有任何全局原文开关/按钮**；raw 片段转义 `& < > " '` 后逐字写入。

## 最终汇报

raw 路径、正式 HTML 路径、M/N/Q（有评分/无评分、已答/未答细分）、J 与覆盖校验结果、日期范围、`__common/validate.py` 通过输出、临时目录已删除（失败时改为保留路径）；`ls zsxq_md` 确认本期只新增 raw 与正式 HTML 两个文件。
