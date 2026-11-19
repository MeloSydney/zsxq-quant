#!/usr/bin/env python3
"""Shared validation core for the unified ZSXQ pipeline (DGF / Duoyu).

泛化自 dgf/validate_output.py：focus-owner、类别集合、分类函数均由调用方
参数化，本模块只做结构与守恒校验（输出为单一时间流，无分栏）。

Checks (errors 汇总返回，CLI 下任一失败退出码非 0):
  1. raw 标题/topic-ID 唯一；filter manifest 覆盖守恒（每个 id 恰好一次）。
  2. 卡片 ID 集合 == kept 帖集合（被排除帖不得渲染）。
  3. 每卡 data-category ∈ 期望类别集合；按类别计数，总卡数 == kept 数；
     卡片按 data-ts 降序（非递增，最新在上）。
  4. 逐帖评论条数（续行并入起始行）；focus-owner 评论行数 == 卡片
     class="focus-name" 子串数（支持多 owner）。
  5. 每卡恰好 1 个 .summary、1 个默认收起的 .raw-block；无全局原文开关。
  6. raw-block 反转义后与 raw 片段逐字一致（规范化空白/---）。
  7. reviewed JSON 走 stance.load_json + stance.validate，且只覆盖 kept 帖。
  8. final html（可选）：卡片 ID 与 draft 一致；每卡恰好 1 个
     market-stance(data-source="analysis")。
  9. classify（可选回调）：每卡 data-category == classify(RawPost)。

Usage:
  python3 -m __common.validate RAW.md DRAFT.html REVIEWED.json \
      [--final FINAL.html] [--manifest MANIFEST.json] \
      --focus-owners 三线文案大锅饭[,求学,...] --categories xingdapai,host-posts,questions
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from html import unescape
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common import stance
from __common.apply_stances import ARTICLE as STANCE_ARTICLE, TIMELINE, timeline_html
from __common.filter import kept_ids, load as load_manifest, validate_manifest

TOPIC_HEADING = re.compile(r'^(## ⭐ .*?|### ❓ ⭐ .*?) \| ID `([1-9][0-9]*)`$', re.M)
COMMENT_START = re.compile(r'^\u3000*(.+?)(?:\s\u56DE\u590D\s(.+?))?\uFF1A')
ARTICLE = re.compile(r'<article\b[^>]*\bclass="post-card"[^>]*\bdata-post-id="([^"]+)"[^>]*>(.*?)</article>', re.S)
RAW_BLOCK = re.compile(r'<details\b[^>]*\bclass="raw-block"[^>]*>\s*<summary>查看原文</summary>\s*<pre>(.*?)</pre>\s*</details>', re.S)
CATEGORY_ATTR = re.compile(r'\bdata-category="([^"]*)"')
TS_ATTR = re.compile(r'\bdata-ts="([^"]*)"')
TS_FORMAT = '%Y-%m-%d %H:%M'
GLOBAL_TOGGLE = re.compile(r'toggle[-_]?all|show[-_]?all[-_]?raw|全局(显示|切换|原文)|切换(全部|所有)(原文|显示)', re.I)
SOURCE_LINE = re.compile(r'^> 来源：(\S+)', re.M)
RANGE_LINE = re.compile(r'^> 日期范围：(\d{4}-\d{2}-\d{2}) ~ (\d{4}-\d{2}-\d{2})', re.M)
HEADING_PREFIX = re.compile(r'^(## ⭐|### ❓ ⭐) (.*)$')
TIME_PART = re.compile(r'^(\d{2})-(\d{2}) (\d{2}):(\d{2})$')


def focus_line(owner: str) -> re.Pattern:
    """一个 focus-owner 的评论行起始正则（**owner 开头，允许全角空格缩进）。"""
    return re.compile(r'^\u3000*\*\*' + re.escape(owner) + r'\b')


def strip_focus(name: str) -> str:
    return name.replace('**', '').strip()


def match_focus_owner(line: str, focus_owners) -> str | None:
    """返回该行匹配的 focus-owner（首个），无匹配返回 None。"""
    for owner in sorted(focus_owners):
        if focus_line(owner).match(line):
            return owner
    return None


def split_raw(raw: str, focus_owners=frozenset()):
    """Return list of (topic_id, heading, segment, comment_lines, focus_count)."""
    matches = list(TOPIC_HEADING.finditer(raw))
    posts = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        seg = raw[m.start():end]
        comment_lines = []
        focus = 0
        idx = seg.find('评论原文：')
        if idx >= 0:
            for line in seg[idx:].splitlines()[1:]:
                if line.startswith('#') or line.strip() == '---':
                    break
                if not line.strip():
                    continue  # 空行分隔评论但不结束评论块
                comment_lines.append(line)
                if match_focus_owner(line, focus_owners) is not None:
                    focus += 1
            seg = seg[:idx].rstrip()
        posts.append((m.group(2), m.group(1), seg, comment_lines, focus))
    return posts


def parse_raw_meta(raw: str):
    """Return (source_url, start_date, end_date, default_year)."""
    range_match = RANGE_LINE.search(raw)
    if range_match:
        start_date, end_date = range_match.group(1), range_match.group(2)
        default_year = int(start_date[:4])
    else:
        today = datetime.now()
        start_date = end_date = today.strftime('%Y-%m-%d')
        default_year = today.year
    source_match = SOURCE_LINE.search(raw)
    source_url = source_match.group(1) if source_match else 'https://wx.zsxq.com/'
    return source_url, start_date, end_date, default_year


def parse_heading(heading: str, default_year: int):
    """Return (is_question, title, score_display, dt_or_None)."""
    m = HEADING_PREFIX.match(heading)
    if not m:
        raise ValueError(f"无法解析标题行：{heading!r}")
    is_question = m.group(1).startswith('###')
    fields = m.group(2).split(' | ', 2)
    if len(fields) != 3:
        raise ValueError(f"标题行字段不足：{heading!r}")
    score_part, time_part, title_part = fields
    title = strip_focus(title_part)
    score_display = score_part if re.match(r'^[0-9.]+分$', score_part) else '无能量评分'
    tm = TIME_PART.match(time_part)
    if tm:
        month, day, hour, minute = (int(g) for g in tm.groups())
        dt = datetime(default_year, month, day, hour, minute)
    else:
        dt = None
    return is_question, title, score_display, dt


@dataclass(frozen=True)
class RawPost:
    topic_id: str
    heading: str
    segment: str
    comment_lines: tuple
    focus_count: int
    is_question: bool
    title: str
    score_display: str
    dt: object | None


def load_posts(raw: str, focus_owners=frozenset()) -> list[RawPost]:
    """split_raw + 标题解析，供 render 的 classify(post) 与 validate 复用。"""
    _url, _start, _end, default_year = parse_raw_meta(raw)
    posts = []
    for topic_id, heading, seg, comment_lines, focus in split_raw(raw, focus_owners):
        is_question, title, score_display, dt = parse_heading(heading, default_year)
        posts.append(RawPost(topic_id, heading, seg, tuple(comment_lines),
                             focus, is_question, title, score_display, dt))
    return posts


def html_cards(html: str):
    return list(ARTICLE.finditer(html))


def normalize_text(t: str) -> str:
    lines = [l.rstrip() for l in t.splitlines() if l.strip() and l.strip() != '---']
    return '\n'.join(lines)


def normalize_segment(seg: str) -> str:
    return normalize_text(seg)


def check_card_comments(body: str, expected: int, post_id: str, errors):
    n = len(re.findall(r'class="comment(?: reply)?"', body))
    if expected == 0:
        if 'class="no-comments"' not in body and n != 0:
            errors.append(f"帖子 {post_id}：raw 无评论但卡片既无 no-comments 标注也无 0 条评论")
    elif n != expected:
        errors.append(f"帖子 {post_id}：raw 评论 {expected} 条，卡片评论 {n} 条")


def validate(*, raw_path, draft_path, reviewed_path, final_path=None,
             focus_owners, categories, manifest_path=None, classify=None):
    """Return (errors, stats)；stats = {"J", "kept", "excluded", "counts"}。

    categories: 有序 [(id, ...)] 或 id 字符串列表（期望类别集合）。classify:
    可选回调 classify(RawPost) -> category_id。
    """
    errors = []
    category_ids = [c if isinstance(c, str) else c[0] for c in categories]
    focus_owners = set(focus_owners)

    raw = Path(raw_path).read_text(encoding='utf-8')
    draft = Path(draft_path).read_text(encoding='utf-8')

    posts = load_posts(raw, focus_owners)
    ids = [p.topic_id for p in posts]
    if not ids:
        errors.append("raw 中未找到任何帖子标题（## ⭐ / ### ❓ ⭐ … | ID `N`）")
    dup = [i for i, c in Counter(ids).items() if c > 1]
    if dup:
        errors.append(f"raw topic ID 重复: {dup}")

    if manifest_path is not None:
        manifest = load_manifest(Path(manifest_path))
        manifest_errors, _counts = validate_manifest(manifest, ids)
        errors.extend(manifest_errors)
        kept = kept_ids(manifest, ids) if not manifest_errors else []
    else:
        kept = ids

    cards = html_cards(draft)
    card_ids = [c.group(1) for c in cards]
    if Counter(card_ids) != Counter(kept):
        only_kept = set(kept) - set(card_ids)
        only_card = set(card_ids) - set(kept)
        if only_kept or only_card:
            errors.append(f"卡片 ID 与 kept 帖不一致（kept 独有 {sorted(only_kept)}，卡片独有 {sorted(only_card)}）")
        else:
            errors.append("卡片 ID 多重出现，与 kept 帖计数不一致")

    counts: Counter = Counter()
    card_category = {}
    prev_ts = None
    for c in cards:
        opening = c.group(0).split('>', 1)[0]
        cat_m = CATEGORY_ATTR.search(opening)
        cat = cat_m.group(1) if cat_m else None
        if cat is None:
            errors.append(f"帖子 {c.group(1)}：卡片缺少 data-category 属性")
        elif cat not in category_ids:
            errors.append(
                f"帖子 {c.group(1)}：data-category {cat!r} 不在期望类别集合 {category_ids}")
        card_category[c.group(1)] = cat
        counts[cat] += 1
        ts = datetime.min
        ts_m = TS_ATTR.search(opening)
        if ts_m:
            try:
                ts = datetime.strptime(ts_m.group(1), TS_FORMAT)
            except ValueError:
                errors.append(
                    f"帖子 {c.group(1)}：data-ts {ts_m.group(1)!r} 不是 YYYY-MM-DD HH:MM")
        if prev_ts is not None and ts > prev_ts:
            errors.append(
                f"帖子 {c.group(1)}：卡片未按 data-ts 降序（"
                f"{prev_ts.strftime(TS_FORMAT)} 之后出现 {ts.strftime(TS_FORMAT)}）")
        prev_ts = ts
    if len(cards) != len(kept):
        detail = ', '.join(f"{k}={counts[k]}" for k in category_ids)
        errors.append(f"总卡数 {len(cards)}（{detail}）!= kept J = {len(kept)}")

    if classify is not None:
        raw_by_id_cls = {p.topic_id: p for p in posts}
        for c in cards:
            post = raw_by_id_cls.get(c.group(1))
            if post is None:
                continue
            expected = classify(post)
            actual = card_category.get(c.group(1))
            if actual != expected:
                errors.append(
                    f"帖子 {c.group(1)}：classify 期望类别 {expected}，实际 data-category {actual}")

    if GLOBAL_TOGGLE.search(draft):
        errors.append("draft 发现全局原文切换控件（禁止）")
    if final_path is not None and GLOBAL_TOGGLE.search(Path(final_path).read_text(encoding='utf-8')):
        errors.append("final 发现全局原文切换控件（禁止）")

    raw_by_id = {p.topic_id: p for p in posts}
    for c in cards:
        title = re.search(r'\bdata-title="([^"]*)"', c.group(0).split('>', 1)[0])
        post = raw_by_id.get(c.group(1))
        if post and (title is None or unescape(title.group(1)) != post.title):
            errors.append(f"帖子 {c.group(1)}：data-title 与 raw 标题不一致")
    for c in cards:
        post_id, body = c.group(1), c.group(2)
        if body.count('class="summary"') != 1:
            errors.append(f"帖子 {post_id}：卡片必须恰好 1 个 .summary")
        blocks = RAW_BLOCK.findall(body)
        if len(blocks) != 1:
            errors.append(f"帖子 {post_id}：卡片必须恰好 1 个 .raw-block")
            continue
        opening = body.split('<pre>', 1)[0]
        if re.search(r'<details\b[^>]*\bopen\b', opening):
            errors.append(f"帖子 {post_id}：raw-block 默认展开（禁止）")
        if post_id in raw_by_id:
            post = raw_by_id[post_id]
            starts = [l for l in post.comment_lines if COMMENT_START.match(l)]
            check_card_comments(body, len(starts), post_id, errors)
            focus_spans = len(re.findall(r'class="focus-name"', body))
            if focus_spans != post.focus_count:
                errors.append(f"帖子 {post_id}：focus-owner 评论 {post.focus_count} 条，卡片黄名标注 {focus_spans} 处")
            if normalize_text(unescape(blocks[0])) != normalize_segment(post.segment):
                errors.append(f"帖子 {post_id}：raw-block 原文与 raw 片段不一致")

    reviewed = None
    try:
        records = stance.load_json(Path(reviewed_path))
        if kept:
            reviewed = stance.validate(records, kept)
        elif records:
            errors.append("kept 帖为空但 reviewed JSON 非空")
    except ValueError as exc:
        errors.append(f"reviewed JSON 校验失败：{exc}")

    if final_path is not None:
        final = Path(final_path).read_text(encoding='utf-8')
        final_ids = [c.group(1) for c in html_cards(final)]
        if Counter(final_ids) != Counter(card_ids):
            errors.append("final html 卡片 ID 集合与 draft 不一致")
        for c in html_cards(final):
            n = len(re.findall(r'class="market-stance"[^>]*data-source="analysis"', c.group(2)))
            if n != 1:
                errors.append(f"final 帖子 {c.group(1)}：market-stance(data-source=analysis) 出现 {n} 次，应为 1")
        timelines = TIMELINE.findall(final)
        if reviewed is not None:
            expected = timeline_html(list(STANCE_ARTICLE.finditer(draft)), reviewed)
            if timelines != [expected] or final.find(expected) > final.find('<div class="legend">'):
                errors.append("final 市场评分时间线与 kept 卡片顺序、标题、时间或 reviewed 分数不一致")

    stats = {
        "J": len(posts),
        "kept": len(kept),
        "excluded": len(posts) - len(kept),
        "counts": {cat: counts[cat] for cat in category_ids},
    }
    return errors, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('raw', type=Path)
    ap.add_argument('draft', type=Path)
    ap.add_argument('reviewed', type=Path)
    ap.add_argument('--final', type=Path)
    ap.add_argument('--manifest', type=Path)
    ap.add_argument('--focus-owners', default='', help='逗号分隔的 focus-owner 名单')
    ap.add_argument('--categories', required=True, help='逗号分隔的有序类别 id')
    args = ap.parse_args()
    focus_owners = {o.strip() for o in args.focus_owners.split(',') if o.strip()}
    categories = [s.strip() for s in args.categories.split(',') if s.strip()]
    errors, stats = validate(
        raw_path=args.raw, draft_path=args.draft, reviewed_path=args.reviewed,
        final_path=args.final, focus_owners=focus_owners, categories=categories,
        manifest_path=args.manifest,
    )
    if errors:
        print(f"校验失败（{len(errors)} 项）：")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    detail = ', '.join(f"{k}={v}" for k, v in stats["counts"].items())
    print(f"校验通过：J={stats['J']}，kept={stats['kept']}（{detail}），excluded={stats['excluded']}；"
          "卡片/评论/focus-owner/reviewed/raw 原文全部一致。")


if __name__ == '__main__':
    main()
