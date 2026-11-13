#!/usr/bin/env python3
"""Duoyu 渲染薄封装：raw markdown + 分析产物 → 共享 draft/final HTML。

四阶段流水线的第③步：
  1. __common.render.render_draft 按 Duoyu 类别规则（is_question → qa，
     否则 host-info）渲染 kept 帖草稿（附图由旁路目录转 base64 内嵌）；
  2. __common.apply_stances 按 reviewed.json 注入市场态度行生成 final。

Usage:
  python3 render_duoyu.py RAW.md SUMMARIES.json REVIEWED.json FILTER.json \
      DRAFT.html FINAL.html [--images-dir DIR] [--origin LABEL]
"""

import argparse
import base64
from pathlib import Path
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common import filter as filter_mod
from __common.apply_stances import DEFAULT_ORIGIN, apply
from __common.render import load_summaries, render_draft
from __common.stance import load_json

FOCUS_OWNERS = {"求学", "大牛"}
CATEGORIES = [
    ("host-info", "星主发言与信息帖", "📝", "#e7f2e4"),
    ("qa", "用户问答", "🙋", "#fbe3ea"),
]
SOURCE_NAME = "多鱼朋友圈"


def classify(post) -> str:
    """Duoyu 类别规则：q&a 帖进「用户问答」，其余进「星主发言与信息帖」。"""
    return "qa" if post.is_question else "host-info"


def make_image_resolver(images_dir: Path | None):
    """旁路图片目录 → (post_id, idx) 定位 {post_id}-{idx}.* 并转 base64。"""
    if images_dir is None or not images_dir.is_dir():
        return None

    def resolve(post_id: str, index: int) -> str | None:
        matches = sorted(images_dir.glob(f"{post_id}-{index}.*"))
        if not matches:
            return None
        return base64.b64encode(matches[0].read_bytes()).decode("ascii")

    return resolve


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_md", type=Path)
    parser.add_argument("summaries_json", type=Path)
    parser.add_argument("reviewed_json", type=Path)
    parser.add_argument("filter_json", type=Path)
    parser.add_argument("draft_html", type=Path)
    parser.add_argument("final_html", type=Path)
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=None,
        help="旁路图片目录（默认 RAW.md 去扩展名 + _images）",
    )
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    args = parser.parse_args()

    raw_text = args.raw_md.read_text(encoding="utf-8")
    summaries = load_summaries(args.summaries_json)
    records = load_json(args.reviewed_json)
    manifest = filter_mod.load(args.filter_json)
    images_dir = args.images_dir or args.raw_md.with_suffix("").parent / (
        args.raw_md.with_suffix("").name + "_images"
    )

    draft = render_draft(
        raw_text=raw_text,
        summaries=summaries,
        focus_owners=FOCUS_OWNERS,
        categories=CATEGORIES,
        classify=classify,
        source_name=SOURCE_NAME,
        source_url=None,
        date_range=None,
        manifest=manifest,
        image_resolver=make_image_resolver(images_dir),
    )
    atomic_write(args.draft_html, draft)
    final = apply(draft, records, origin=args.origin)
    atomic_write(args.final_html, final)

    kept = len(summaries)
    excluded = sum(1 for entry in manifest.values() if not entry.get("keep"))
    print(
        f"渲染完成：kept {kept} 篇（排除 {excluded} 篇未渲染）\n"
        f"草稿：{args.draft_html}\n最终：{args.final_html}"
    )


if __name__ == "__main__":
    main()
