---
name: short-term
description: 读取指定日期（默认今天）的多鱼朋友圈与大锅饭两份分析 HTML 及持仓清单 short-term/hold，用 efinance 解析持仓代码后，为每只持仓给出未来两个交易日的操作计划（增持/减持/保持不动）。用户要短线计划、持仓操作建议时使用。
version: 1.1.0
allowed-tools: Read, Glob, Bash, Grep
argument-hint: "[YYYY-MM-DD 或 MMDD，留空默认今天]"
---

# 短线持仓决策（多鱼 + 大锅饭双源）

数据源是两条流水线已产出的**分析 HTML**（每帖含人工标注的市场态度 -2..+2，语义 = 未来两个交易日的买卖情绪信号）。本 skill 不抓取数据，只做决策。**任一源缺失直接报错终止，不降级为单源决策。**

## 输入参数

`$ARGUMENTS`：`YYYY-MM-DD` 或 `MMDD`（按文件名前缀 MMDD 匹配期次）；**留空默认今天**（`date +%m%d` 得 MMDD；当天期次文件尚未生成时按缺文件报错处理）。

## 执行步骤（单会话直接执行，禁止委派子 agent）

### 第一步：定位两份分析 HTML（缺一即报错）

```bash
date +%m%d   # 无参数时的默认 MMDD
ls /Users/melo/doc-Dgf/duoyu/{MMDD}-多鱼朋友圈_2*.html    # 多鱼 final
ls /Users/melo/doc-Dgf/zsxq_md/{MMDD}_大锅饭_2*.html      # 大锅饭 final
```

- 通配 `2*` 用于排除同前缀的 `*_raw_*` 文件（raw 文件名含 `_raw_`，不匹配）。
- duoyu glob 为空 → 报错：**先运行 duoyu-pipeline 生成 {MMDD} 期分析文件**，终止。
- zsxq_md glob 为空 → 报错：**先运行 dgf-pipeline 生成 {MMDD} 期分析文件**，终止。

### 第二步：读取持仓清单

`/Users/melo/src-skills/src-zsxq/zsxq-quant/short-term/hold`，每行一个持仓：

- 格式：`代码` 或 `代码 # 名称/备注`（备注帮助匹配文本中的提及）
- `#` 开头行与空行忽略；hold 为空或不存在 → 报错终止。

### 第三步：解析持仓代码（efinance）

```bash
cd /Users/melo/src-skills/src-zsxq/zsxq-quant/short-term
.venv/bin/python code_info.py <code> [<code> ...]
```

- 用 efinance 解析每个代码：**名称 + 最近 3 个交易日收盘/涨跌幅**（如 `159583 通信ETF富国 | 2026-11-13 收1.062 -4.84%`）。
- 名称用于第五步在两份文本中检索命中；最近涨跌幅作为决策参考（连续大跌/大涨影响加减仓倾向）。
- 单个代码解析失败**不终止**：输出 `解析失败(...)`，该持仓仅按代码/备注匹配，照常给出决策。
- 脚本在 efinance session 上挂了 curl 传输适配器（eastmoney WAF 会掐断 python TLS 栈的连接，curl 正常，含 3 次重试）；勿移除该兼容层。

### 第四步：提取两份 HTML 的纯文本

```bash
python3 /Users/melo/src-skills/src-zsxq/zsxq-quant/short-term/extract_text.py <duoyu.html>
python3 /Users/melo/src-skills/src-zsxq/zsxq-quant/short-term/extract_text.py <dgf.html>
```

脚本输出每帖：时间、标题、态度行（市场态度 ±N + 依据）、总结、截断原文；跳过图片/base64 与评论区。读取两份输出作为决策素材。

### 第五步：分析与决策

对每只持仓：

1. **检索命中**：代码、efinance 解析名称、备注名称、公司/标的名称、所处行业板块关键词在两份文本中的出现；
2. **整体情绪**：两源全部帖子市场态度的汇总（均值与多空分布）；
3. **决策（必须三选一，窗口 = 未来两个交易日，与态度行语义一致）**：
   - **增持**：标的或其板块被明确点名看多（态度 +1/+2 且依据具体）；
   - **减持**：标的或其板块被明确点名看空（态度 -1/-2 且依据具体）；
   - **保持不动**：无明确点名信号时的默认；只有整体情绪极端偏空且证据可迁移到该持仓时才可倾向减持，必须引用两源证据。

### 第六步：输出（直接在对话中，不写文件）

- 持仓计划表：`代码（名称） | 计划 | 依据（引用帖子时间 + 态度 + 要点）`
- 两源整体情绪各一行（期次、帖子数、态度均值/分布）；
- 所用数据文件路径与期次。

## 注意

- 只读 final 分析 HTML，不读 raw 文件；决策不修改任何输入文件。
- 报错场景不猜测、不降级：单源缺失、hold 为空、日期无匹配均直接终止并提示补救方式。
- 遵守仓库执行纪律：不委派子 agent、不平行复读审计。
