#!/usr/bin/env python3
"""Shared stance injector for the unified ZSXQ pipeline (DGF / Duoyu).

Generalized from dgf/apply_stances.py: source-agnostic. It injects one
reviewed market-stance annotation per rendered card, fail closed. Unlike the
DGF-only version it does NOT hard-code section ids and supports filter
exclusion (cards == kept posts, a subset of raw posts).

Coverage is enforced by __common.stance.validate over the rendered card ids,
so excluded posts simply have no card and require no record. Structural
checks (unique doctype/style, one summary + one closed raw-block per card,
no duplicate stance) are kept; raw-block verbatim + section/coverage checks
live in __common.validate.

Usage:
  python3 -m __common.apply_stances DRAFT.html REVIEWED.json OUTPUT.html \
      [--origin "人工分析标注 · 非原帖能量评分"]
"""

import argparse
from html import escape
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common.stance import load_json, stance_html, validate
from __common.stance_style import install_stance_style

ARTICLE = re.compile(r'(<article\b[^>]*\bclass="post-card"[^>]*\bdata-post-id="([^"]+)"[^>]*>)(.*?)(</article>)', re.S)
STANCE = re.compile(r'<p\b[^>]*\bclass="market-stance"[^>]*>.*?</p>', re.S)
RAW_BLOCK = re.compile(r'<details\b[^>]*\bclass="raw-block"[^>]*>\s*<summary>查看原文</summary>\s*<pre>(.*?)</pre>\s*</details>', re.S)
DEFAULT_ORIGIN = "人工分析标注 · 非原帖能量评分"
TITLE = re.compile(r'\bdata-title="([^"]*)"')
TIMESTAMP = re.compile(r'\bdata-ts="([^"]+)"')
TIMELINE = re.compile(r'<div class="score-timeline"[^>]*>.*?</div>', re.S)


def timeline_html(cards, reviewed) -> str:
    """Use card order and headings, not titles as keys (titles need not be unique)."""
    lines = ['<div class="score-timeline" aria-label="市场评分时间线">',
             '<h2>市场评分时间线</h2>', '<ol>']
    previous_day = None
    for card in cards:
        title = TITLE.search(card.group(1))
        timestamp = TIMESTAMP.search(card.group(1))
        if title is None or timestamp is None:
            raise ValueError(f"帖子 {card.group(2)} 缺少标题或完整发布时间")
        when = timestamp.group(1)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', when):
            raise ValueError(f"帖子 {card.group(2)} 发布时间格式无效：{when}")
        day = when[:10]
        separator = ' class="new-day"' if previous_day is not None and previous_day != day else ''
        score = reviewed[card.group(2)].score
        score_text = f'{score:+d}' if score else '0'
        lines.append(f'<li{separator} data-post-id="{escape(card.group(2), quote=True)}">'
                     f'<strong>{score_text}</strong> · <span>{title.group(1)}</span> · '
                     f'<time datetime="{escape(when, quote=True)}">{escape(when)}</time></li>')
        previous_day = day
    lines.extend(['</ol>', '</div>'])
    return '\n'.join(lines)


def apply(draft: str, records, origin: str = DEFAULT_ORIGIN) -> str:
    if draft.count("</style>") != 1 or draft.count("<!doctype html>") != 1:
        raise ValueError("分析 HTML 缺少唯一文档头或样式插入点")
    cards = list(ARTICLE.finditer(draft))
    if not cards:
        raise ValueError("分析 HTML 没有任何 post-card 卡片")
    card_ids = [c.group(2) for c in cards]
    if len(card_ids) != len(set(card_ids)):
        raise ValueError("分析卡片 ID 重复")
    reviewed = validate(records, card_ids)
    for card in cards:
        body = card.group(3)
        if body.count('class="raw-block"') != 1 or body.count('class="summary"') != 1:
            raise ValueError(f"帖子 {card.group(2)} 缺少唯一摘要或原文折叠区")
        block = RAW_BLOCK.search(body)
        if not block:
            raise ValueError(f"帖子 {card.group(2)} 缺少原文折叠区")
        if " open" in block.group(0).split(">", 1)[0]:
            raise ValueError(f"帖子 {card.group(2)} 的原文不得默认展开")
    for card in reversed(cards):
        body = card.group(3)
        old = STANCE.findall(body)
        if len(old) > 1:
            raise ValueError(f"帖子 {card.group(2)} 有重复市场态度行")
        annotation = stance_html(reviewed[card.group(2)], origin=origin)
        if old:
            body = STANCE.sub(annotation, body, count=1)
        else:
            end = body.find('</div>', body.find('<div class="summary"'))
            if end < 0:
                raise ValueError(f"帖子 {card.group(2)} 缺少摘要结束标签")
            body = body[:end + 6] + annotation + body[end + 6:]
        draft = draft[:card.start()] + card.group(1) + body + card.group(4) + draft[card.end():]
    if TIMELINE.search(draft):
        raise ValueError("分析 HTML 已有市场评分时间线")
    if draft.count('<div class="legend">') != 1:
        raise ValueError("分析 HTML 缺少唯一五档图例")
    timeline = timeline_html(cards, reviewed)
    draft = draft.replace('<div class="legend">', timeline + '\n<div class="legend">', 1)
    return install_stance_style(draft, "reviewed-stance")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft_html", type=Path)
    parser.add_argument("reviewed_json", type=Path)
    parser.add_argument("output_html", type=Path)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    args = parser.parse_args()
    draft = args.draft_html.read_text(encoding="utf-8")
    records = load_json(args.reviewed_json)
    result = apply(draft, records, origin=args.origin)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=args.output_html.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(result)
        os.replace(tmp, args.output_html)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    print(f"已验证并标注 {len(ARTICLE.findall(result))} 篇：{args.output_html}")


if __name__ == "__main__":
    main()
