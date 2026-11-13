#!/usr/bin/env python3
"""Annotate a Duoyu raw HTML export with human-reviewed market stance scores."""

import argparse
from html import escape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common.stance import Stance, stance_html, validate
from __common.stance_style import install_stance_style


HEADING = re.compile(r"^### .+ \| ID `(?P<id>[1-9][0-9]*)`\s*$")
EXCLUDED = re.compile(r"^- ID `(?P<id>[1-9][0-9]*)`.+?\*\*市场态度：")
SCORE = re.compile(
    r"\*\*市场态度：(?P<score>[+-]?[0-2])｜(?P<scope>[^｜*\r\n]+)｜"
    r"(?P<time>[^｜*\r\n]+)\*\*\s*[—–-]\s*"
    r"(?:(?:依据：)?(?P<reason>\S.*))$"
)
ROW = re.compile(r'\n?<p class="market-stance" data-source="analysis">.*?</p>', re.S)


def scores_from_markdown(markdown: str) -> dict[str, tuple[str, str, str, str]]:
    scores = {}
    pending = None
    excluded = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            if pending is not None:
                raise ValueError(f"帖子 {pending} 缺少市场态度行")
            excluded = line.startswith("## 未纳入浓缩的提问")
        heading = HEADING.fullmatch(line)
        if heading:
            if pending is not None:
                raise ValueError(f"帖子 {pending} 缺少市场态度行")
            pending = heading.group("id")
            continue
        footer = EXCLUDED.match(line) if excluded else None
        if footer:
            post_id = footer.group("id")
        elif pending is not None and line.startswith("**市场态度："):
            post_id = pending
            pending = None
        elif "**市场态度：" in line and (line.startswith("### ") or line.startswith("- ID `")):
            raise ValueError(f"市场态度格式或所在章节无效：{line[:100]}")
        else:
            continue
        match = SCORE.search(line)
        if not match:
            raise ValueError(f"帖子 {post_id} 市场态度格式无效")
        score = match.group("score")
        if score not in ("-2", "-1", "0", "+1", "+2"):
            raise ValueError(f"帖子 {post_id} 市场态度分不在五档内")
        if post_id in scores:
            raise ValueError(f"帖子 {post_id} 的市场态度重复")
        scores[post_id] = (score, match.group("scope").strip(), match.group("time").strip(), match.group("reason").strip())
    if pending is not None:
        raise ValueError(f"帖子 {pending} 缺少市场态度行")
    return scores


class Cards(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=False)
        self.lines = [0]
        for match in re.finditer("\n", html):
            self.lines.append(match.end())
        self.cards = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "article":
            return
        attributes = dict(attrs)
        if "post-card" not in (attributes.get("class") or "").split():
            return
        post_id = attributes.get("data-post-id")
        if not post_id or not re.fullmatch(r"[1-9][0-9]*", post_id):
            raise ValueError(f"无效的 post-card ID：{post_id!r}")
        line, column = self.getpos()
        offset = self.lines[line - 1] + column + len(self.get_starttag_text())
        self.cards.append((post_id, offset))


def overlay(html: str, scores: dict[str, tuple[str, str, str, str]]) -> str:
    # Shared installer checks and replaces managed CSS after card annotations.
    # On reruns only our own adjacent annotations can be removed; reject foreign score markup.
    parser = Cards(html)
    parser.feed(html)
    if not parser.cards:
        raise ValueError("raw HTML 没有 post-card")
    ids = [post_id for post_id, _ in parser.cards]
    if len(set(ids)) != len(ids):
        raise ValueError("raw HTML 中 post-card ID 重复")
    reviewed = validate(
        [Stance(post_id, int(score), scope, time, reason) for post_id, (score, scope, time, reason) in scores.items()],
        ids,
    )
    # Exact coverage and all reviewed fields were checked before touching HTML.
    insertions = []
    existing_count = 0
    for post_id, offset in parser.cards:
        existing = ROW.match(html, offset)
        if existing:
            end = existing.end()
            existing_count += 1
        else:
            if html.startswith('\n<p class="market-stance"', offset) or html.startswith('<p class="market-stance"', offset):
                raise ValueError(f"帖子 {post_id} 已有不受管的市场态度行")
            end = offset
        annotation = '\n' + stance_html(reviewed[post_id], origin="分析标注 · 非 API 原文/能量评分")
        insertions.append((offset, end, annotation))
    if existing_count != html.count('class="market-stance" data-source="analysis"'):
        raise ValueError("HTML 有重复或不受管的分析标注")
    for start, end, replacement in reversed(insertions):
        html = html[:start] + replacement + html[end:]
    return install_stance_style(html, "duoyu-market-overlay")


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("raw_html", type=Path, help="已导出的多鱼 raw HTML（原路径原子更新）")
    args.add_argument("analysis_md", type=Path, help="人工分析生成的逐篇市场态度 Markdown")
    options = args.parse_args()
    if "_raw_" not in options.raw_html.name or options.analysis_md.name != options.raw_html.name.replace("_raw_", "_").removesuffix(".html") + ".md":
        args.error("分析 Markdown 必须与 raw HTML 的日期范围/文件名匹配")
    scores = scores_from_markdown(options.analysis_md.read_text(encoding="utf-8"))
    html = overlay(options.raw_html.read_text(encoding="utf-8"), scores)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=options.raw_html.parent, prefix=f".{options.raw_html.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(html)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, options.raw_html)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"已标注 {len(scores)} 篇市场态度：{options.raw_html}")


if __name__ == "__main__":
    main()
