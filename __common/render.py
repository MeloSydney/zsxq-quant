"""Shared renderer for the unified ZSXQ pipeline (generalized from dgf/build_draft.py).

脚本负责草稿 HTML 的全部机械部分：CSS 骨架（恒浅色主题）、header/图例、单一时间流
（全部 kept 帖跨类别按发帖时间降序，最新在上）、每帖卡片（类别图标+浅色背景、
标题/时间/评分/Topic ID）、评论区逐字渲染（含 focus-owner 黄名，支持多 owner）、
图片旁路嵌入（image_resolver，可为 None）、raw-block 逐字转义注入、按 filter
manifest 只渲染 kept 帖。
LLM/审阅者只产出每帖 summary 与人工审阅态度（reviewed JSON，由发布阶段脚本注入）。

参数化点：
  focus_owners : set[str]，各源自行配置（DGF=三线文案大锅饭；Duoyu=求学/大牛…）
  categories   : 有序 [(id, label, icon, bg|None)]；icon=emoji，bg=浅色 hex 或 None
  classify     : classify(RawPost) -> category_id（RawPost 见 __common.validate）
  image_resolver: (post_id, idx 从 1 起) -> base64 字符串 | None；None 表示无图
  source_name / source_url / date_range: 页面署名；URL 与日期缺省从 raw 头部解析

focus span 的 class 必须恰为 class="focus-name"（validator 子串计数），非 focus
用 class="cname"，reply 用 class="comment reply"。输出无全局原文开关。
"""

from datetime import datetime
import html
import json
from pathlib import Path

from __common.filter import kept_ids, validate_manifest
from __common.validate import (
    COMMENT_START,
    RawPost,
    focus_line,
    load_posts,
    match_focus_owner,
    parse_raw_meta,
    strip_focus,
)

MAX_IMAGES_PER_POST = 32

CSS = """
:root {
  --bg: #f4f1ea; --panel: #fffdf8; --text: #25221d; --line: #d9d1c3;
  --muted: #6d6558; --gold: #926300; --summary: #fff8ec; --comments: #faf5ea;
  --rawbg: #f1ebdd; --codebg: #efe9db;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.75 -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
header.page { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px 24px; margin-bottom: 24px; }
header.page h1 { margin: 0 0 8px; font-size: 24px; }
header.page .src { color: var(--muted); font-size: 13px; margin: 4px 0; }
.legend { margin-top: 12px; padding: 10px 12px; background: var(--summary);
  border: 1px solid var(--line); border-radius: 8px; font-size: 13px; color: var(--muted); }
article.post-card { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  padding: 16px 18px; margin-bottom: 18px; }
article.post-card h3 { margin: 0 0 4px; font-size: 17px; }
article.post-card .meta { color: var(--muted); font-size: 12.5px; margin: 0 0 10px; }
article.post-card .meta code { background: var(--codebg); border-radius: 4px; padding: 1px 5px;
  font-size: 12px; }
.summary { background: var(--summary); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; }
.summary p { margin: 0 0 8px; }
.summary p:last-child { margin-bottom: 0; }
.images { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.images figure { margin: 0; border: 1px solid var(--line); border-radius: 8px;
  overflow: hidden; background: var(--rawbg); }
.images img { display: block; max-width: 100%; height: auto; }
.images figcaption { padding: 6px 12px; font-size: 12.5px; color: var(--muted); }
.comments { background: var(--comments); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; margin-top: 12px; }
.comments h4 { margin: 0 0 8px; font-size: 14px; color: var(--muted); }
.comments .comment { padding: 6px 0; border-top: 1px dashed var(--line); font-size: 14px; }
.comments .comment:first-of-type { border-top: none; }
.comments .comment.reply { margin-left: 22px; }
.comments .cname { font-weight: 600; }
.comments .focus-name { font-weight: 700; color: var(--gold); }
.comments .no-comments { color: var(--muted); font-size: 13px; margin: 0; }
details.raw-block { margin-top: 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--rawbg); }
details.raw-block summary { cursor: pointer; padding: 8px 14px; font-size: 13.5px;
  color: var(--muted); user-select: none; }
.raw-block pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0;
  padding: 12px 14px; max-height: 480px; overflow-y: auto; font-size: 12.5px; line-height: 1.6;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  border-top: 1px solid var(--line); }
footer.page { color: var(--muted); font-size: 12.5px; text-align: center; }
"""

DEFAULT_LEGEND = (
    '市场态度五档图例（人工复核，独立于原帖能量评分，评分对象为当前市场情绪、'
    'timescale 默认「未来两个交易日」）：<strong>+2</strong> 明确看多并建议增加风险敞口 · '
    '<strong>+1</strong> 看好修复但提示风险 · <strong>0</strong> 多空相抵 / 震荡观望 / 方向信息不足 · '
    '<strong>-1</strong> 偏谨慎、反弹持续性存疑或建议降低仓位 · <strong>-2</strong> 明确看跌、要求避险减仓。'
    '态度行由发布阶段脚本按人工审阅 JSON 注入本草稿。'
)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def name_span(line: str, author: str, focus_owners) -> str:
    """validator 用子串 class="focus-name" 计数，focus 行必须恰用 focus-name。"""
    owner = match_focus_owner(line, focus_owners)
    if owner is not None:
        return f'<span class="focus-name">{esc(owner)}</span>'
    return f'<span class="cname">{esc(strip_focus(author))}</span>'


def render_comments(comment_lines, focus_owners) -> str:
    comment_lines = list(comment_lines)
    starts = [l for l in comment_lines if COMMENT_START.match(l)]
    if not starts and not comment_lines:
        return (
            '<div class="comments"><h4>评论区（0 条，按时间升序）</h4>'
            '<p class="no-comments">本帖暂无评论</p></div>'
        )
    divs: list[str] = []
    for line in comment_lines:
        m = COMMENT_START.match(line)
        if m:
            author, reply_to = m.group(1), m.group(2)
            content = line[m.end():]
            cls = 'comment reply' if reply_to else 'comment'
            middle = f' 回复 {esc(reply_to)}' if reply_to else ''
            divs.append(
                f'<div class="{cls}">{name_span(line, author, focus_owners)}{middle}：{esc(content)}</div>'
            )
        elif divs:
            # 续行并入当前评论；若续行本身是 focus 行也要产出 focus-name 标注
            owner = match_focus_owner(line, focus_owners)
            if owner is not None:
                rest = focus_line(owner).sub('', line, count=1).lstrip('*').strip()
                divs[-1] = (
                    divs[-1][:-6]
                    + f'<span class="focus-name">{esc(owner)}</span>{esc(rest)}</div>'
                )
            else:
                divs[-1] = divs[-1][:-6] + esc(line.replace('**', '').strip()) + '</div>'
        else:
            divs.append(f'<div class="comment">{esc(line.strip())}</div>')
    return (
        f'<div class="comments"><h4>评论区（{len(starts)} 条，按时间升序）</h4>'
        + ''.join(divs)
        + '</div>'
    )


def render_images(post_id: str, image_resolver) -> str:
    """旁路图片目录 → 卡片内嵌 base64 figure；resolver 为 None 时不产出。"""
    if image_resolver is None:
        return ''
    figures = []
    for idx in range(1, MAX_IMAGES_PER_POST + 1):
        data = image_resolver(post_id, idx)
        if data is None:
            break
        figures.append(
            f'<figure><img src="data:image/jpeg;base64,{data}" alt="图{idx}">'
            f'<figcaption>图{idx}</figcaption></figure>'
        )
    if not figures:
        return ''
    return '<div class="images">' + ''.join(figures) + '</div>'


def summary_html(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("summary 为空")
    if value.startswith('<'):
        return value
    return f'<p>{value}</p>'


def load_summaries(path: Path) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out = {}
        for item in data:
            if not isinstance(item, dict) or 'post_id' not in item:
                raise ValueError(f"SUMMARIES 列表项缺少 post_id：{item!r}")
            summary = item.get('summary', item.get('summary_html', ''))
            out[str(item['post_id'])] = str(summary)
        return out
    raise ValueError("SUMMARIES.json 必须是对象或数组")


def render_draft(*, raw_text: str, summaries: dict[str, str], focus_owners,
                 categories, classify, source_name: str,
                 source_url: str | None = None, date_range: tuple[str, str] | None = None,
                 legend: str | None = DEFAULT_LEGEND, manifest=None,
                 image_resolver=None) -> str:
    """Build the shared draft HTML. categories: 有序 [(id, label, icon, bg|None)]。

    输出单一时间流：全部 kept 帖跨类别按发帖时间降序（最新在上），每卡带
    data-category / data-ts 属性、类别图标与浅色背景（bg=None 用默认面板色）。
    manifest 为 None 时渲染全部帖；否则只渲染 keep 帖（summaries 键集合必须
    恰等于 kept 帖集合，fail closed）。
    """
    focus_owners = set(focus_owners)
    cat_by_id = {c[0]: c for c in categories}
    posts = load_posts(raw_text, focus_owners)
    if not posts:
        raise ValueError("raw 中未找到任何帖子标题")
    ids = [p.topic_id for p in posts]
    if len(set(ids)) != len(ids):
        raise ValueError("raw topic ID 重复")

    if manifest is not None:
        manifest_errors, _counts = validate_manifest(manifest, ids)
        if manifest_errors:
            raise ValueError('；'.join(manifest_errors))
        kept = set(kept_ids(manifest, ids))
    else:
        kept = set(ids)

    kept_list = [p for p in posts if p.topic_id in kept]
    if set(summaries) != kept:
        raise ValueError(
            f"SUMMARIES post_id 集合与 kept 帖不一致（缺 {sorted(kept - set(summaries))}，"
            f"多 {sorted(set(summaries) - kept)}）"
        )

    meta_url, meta_start, meta_end, _year = parse_raw_meta(raw_text)
    url = source_url or meta_url
    start_date, end_date = date_range or (meta_start, meta_end)
    group_id = url.rstrip('/').rsplit('/', 1)[-1]

    counts: dict[str, int] = {key: 0 for key in cat_by_id}
    entries: list[tuple[RawPost, tuple]] = []
    for post in kept_list:
        category = classify(post)
        if category not in cat_by_id:
            raise ValueError(f"帖子 {post.topic_id}：classify 返回未知类别 {category!r}")
        counts[category] += 1
        entries.append((post, cat_by_id[category]))
    entries.sort(key=lambda pair: pair[0].dt or datetime.min, reverse=True)

    cards: list[str] = []
    for post, (cat_id, _label, icon, bg) in entries:
        when = post.dt.strftime('%Y-%m-%d %H:%M') if post.dt else '时间未知'
        ts_attr = f' data-ts="{when}"' if post.dt else ''
        style_attr = f' style="background:{bg}"' if bg else ''
        cards.append(
            f'<article class="post-card" data-post-id="{esc(post.topic_id)}" '
            f'data-category="{esc(cat_id)}"{ts_attr}{style_attr}>\n'
            f'<h3>{icon} {esc(post.title)}</h3>\n'
            f'<p class="meta">发布 {when} · {esc(post.score_display)} · '
            f'Topic ID <code>{esc(post.topic_id)}</code></p>\n'
            f'<div class="summary">{summary_html(summaries[post.topic_id])}</div>\n'
            f'{render_images(post.topic_id, image_resolver)}'
            f'{render_comments(post.comment_lines, focus_owners)}\n'
            f'<details class="raw-block"><summary>查看原文</summary>'
            f'<pre>{esc(post.segment)}</pre></details>\n'
            f'</article>'
        )

    J = len(kept_list)
    parts = [
        '<!doctype html>',
        '<html lang="zh-CN">',
        '<head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{esc(source_name)} · 分析草稿 · {start_date} ~ {end_date}</title>',
        f'<style>{CSS}</style>',
        '</head>',
        '<body>',
        '<div class="wrap">',
        '<header class="page">',
        f'<h1>{esc(source_name)} · 浓缩分析（草稿）</h1>',
        f'<p class="src">来源：<a href="{esc(url)}">知识星球 {esc(group_id)}</a> · '
        f'日期范围 {start_date} ~ {end_date}</p>',
        '<p class="src">'
        + ' · '.join(f'{icon} {label} {counts[key]} 篇' for key, label, icon, _bg in categories)
        + f' · 合计 {J} 篇（= 过滤后 kept 总数 {J}，覆盖校验通过）</p>',
    ]
    if legend:
        parts.append(f'<div class="legend">{legend}</div>')
    parts.extend(['</header>', '<main>'])
    parts.extend(cards)
    parts.extend([
        '</main>',
        f'<footer class="page">{esc(source_name)} · 浓缩分析草稿 · '
        f'{start_date} ~ {end_date}</footer>',
        '</div>',
        '</body>',
        '</html>',
        '',
    ])
    return '\n'.join(parts)
