#!/usr/bin/env python3
"""DGF source adapter for the unified pipeline: render draft + inject stances.

Thin wrapper over __common.render / __common.apply_stances with DGF-specific
config (focus-owner 三线文案大锅饭; categories 星大派/星主普通/用户提问; classify rule).
DGF 默认过滤为 pass-all（恒等，全部 keep）。

Usage:
  python3 dgf/render_dgf.py RAW.md SUMMARIES.json REVIEWED.json DRAFT.html FINAL.html \
      --pass-all [--origin LABEL]
  # 或显式 manifest：把 --pass-all 换成 --filter FILTER.json
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common import apply_stances, filter as flt, render

FOCUS_OWNERS = {"三线文案大锅饭"}
CATEGORIES = [
    ("xingdapai", "星大派", "🌟", "#fdf0c4"),
    ("host-posts", "星主普通", "📝", "#e7f2e4"),
    ("questions", "用户提问", "🙋", "#fbe3ea"),
]
SOURCE_NAME = "大锅饭与小伙伴的进步空间"


def classify(post) -> str:
    if post.is_question:
        return "questions"
    first = ""
    for line in post.segment.splitlines()[1:]:
        if line.strip():
            first = line.replace("**", "").strip()
            break
    if post.title.startswith("星大派") or first.startswith("星大派") or "直播总结" in post.title:
        return "xingdapai"
    return "host-posts"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("raw", type=Path)
    ap.add_argument("summaries", type=Path)
    ap.add_argument("reviewed", type=Path)
    ap.add_argument("draft", type=Path)
    ap.add_argument("final", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass-all", action="store_true", help="DGF 默认：全部 keep（恒等过滤）")
    g.add_argument("--filter", type=Path, help="显式 filter manifest JSON")
    ap.add_argument("--origin", default=apply_stances.DEFAULT_ORIGIN)
    args = ap.parse_args()

    raw_text = args.raw.read_text(encoding="utf-8")
    summaries = render.load_summaries(args.summaries)
    manifest = None if args.pass_all else flt.load(args.filter)
    draft = render.render_draft(
        raw_text=raw_text, summaries=summaries, focus_owners=FOCUS_OWNERS,
        categories=CATEGORIES, classify=classify, source_name=SOURCE_NAME,
        manifest=manifest, image_resolver=None)
    args.draft.parent.mkdir(parents=True, exist_ok=True)
    args.draft.write_text(draft, encoding="utf-8")

    records = apply_stances.load_json(args.reviewed)
    final = apply_stances.apply(draft, records, origin=args.origin)
    args.final.parent.mkdir(parents=True, exist_ok=True)
    args.final.write_text(final, encoding="utf-8")
    print(f"已生成草稿与正式 HTML：{args.draft} / {args.final}")


if __name__ == "__main__":
    main()
