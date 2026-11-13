#!/usr/bin/env python3
"""Apply reviewed stances to an agent-authored DGF analysis HTML, fail closed."""

import argparse
from collections import Counter
from html import unescape
from html.parser import HTMLParser
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
RAW_HEADING = re.compile(r'^## ⭐ .*$', re.M)
QUESTION_HEADING = re.compile(r'^### ❓ .*$', re.M)
RAW_BLOCK = re.compile(r'<details\b[^>]*\bclass="raw-block"[^>]*>\s*<summary>查看原文</summary>\s*<pre>(.*?)</pre>\s*</details>', re.S)
TOPIC_ID = re.compile(r" \| ID `([1-9][0-9]*)`$")


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = []
        self.current = None
        self.cards = Counter()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "section" and "category" in (attributes.get("class") or "").split():
            self.current = attributes.get("id")
            self.sections.append(self.current)
        if tag == "article" and "post-card" in (attributes.get("class") or "").split():
            self.cards[self.current] += 1

    def handle_endtag(self, tag):
        if tag == "section":
            self.current = None


def apply(raw: str, draft: str, records) -> str:
    if draft.count("</style>") != 1 or draft.count("<!doctype html>") != 1:
        raise ValueError("分析 HTML 缺少唯一文档头或样式插入点")
    # Style integrity is checked while installing the shared annotation CSS.
    structure = Structure()
    structure.feed(draft)
    if structure.sections != ["xingdapai", "host-posts", "questions"]:
        raise ValueError("分析 HTML 必须包含按顺序排列的三个专栏")
    raw_headings = RAW_HEADING.findall(raw) + QUESTION_HEADING.findall(raw)
    if not raw_headings:
        raise ValueError("raw 没有帖子")
    cards = list(ARTICLE.finditer(draft))
    if len(cards) != len(raw_headings) or len(cards) != sum(structure.cards.values()):
        raise ValueError("raw 帖子数与分析卡片数不一致")
    raw_ids = [TOPIC_ID.search(heading) for heading in raw_headings]
    if any(raw_ids) and not all(raw_ids):
        raise ValueError("raw 中只有部分帖子带 topic ID")
    reviewed = validate(records, [card.group(2) for card in cards])
    draft_headings = []
    for card in cards:
        block = RAW_BLOCK.search(card.group(3))
        if not block or card.group(3).count('class="raw-block"') != 1 or card.group(3).count('class="summary"') != 1:
            raise ValueError(f"帖子 {card.group(2)} 缺少唯一摘要或原文折叠区")
        if " open" in block.group(0).split(">", 1)[0]:
            raise ValueError(f"帖子 {card.group(2)} 的原文不得默认展开")
        text = unescape(block.group(1))
        heading = RAW_HEADING.search(text) or QUESTION_HEADING.search(text)
        if not heading:
            raise ValueError(f"帖子 {card.group(2)} 缺少 raw 标题")
        draft_headings.append(heading.group())
    if all(raw_ids) and Counter(match.group(1) for match in raw_ids) != Counter(card.group(2) for card in cards):
        raise ValueError("分析卡片 ID 与 raw topic ID 不一致")
    if Counter(raw_headings) != Counter(draft_headings):
        raise ValueError("分析卡片原文与 raw 帖子标题不一致")
    for card in reversed(cards):
        body = card.group(3)
        old = STANCE.findall(body)
        if len(old) > 1:
            raise ValueError(f"帖子 {card.group(2)} 有重复市场态度行")
        annotation = stance_html(reviewed[card.group(2)], origin="人工分析标注 · 非原帖能量评分")
        if old:
            body = STANCE.sub(annotation, body, count=1)
        else:
            end = body.find('</div>', body.find('<div class="summary"'))
            if end < 0:
                raise ValueError(f"帖子 {card.group(2)} 缺少摘要结束标签")
            body = body[:end + 6] + annotation + body[end + 6:]
        draft = draft[:card.start()] + card.group(1) + body + card.group(4) + draft[card.end():]
    return install_stance_style(draft, "reviewed-stance")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("draft_html", type=Path)
    parser.add_argument("reviewed_json", type=Path)
    parser.add_argument("output_html", type=Path)
    args = parser.parse_args()
    if args.output_html == args.draft_html or args.output_html == args.raw:
        parser.error("草稿和 raw 必须与目标文件不同")
    result = apply(args.raw.read_text(encoding="utf-8"), args.draft_html.read_text(encoding="utf-8"), load_json(args.reviewed_json))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output_html.parent, prefix=f".{args.output_html.name}.", delete=False) as target:
            temporary = Path(target.name)
            target.write(result)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, args.output_html)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"已验证并标注 {len(ARTICLE.findall(result))} 篇：{args.output_html}")


if __name__ == "__main__":
    main()
