#!/usr/bin/env python3
"""render_duoyu.py 薄封装的离线端到端测试（共享渲染 + 标注 + 校验）。"""

import base64
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from __common import filter as filter_mod
from __common.validate import validate

MODULE_PATH = Path(__file__).resolve().parents[1] / "render_duoyu.py"
SPEC = importlib.util.spec_from_file_location("duoyu_render", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RAW = """# 多鱼朋友圈（原始数据）

> 来源：https://wx.zsxq.com/group/15284854485542
> 导出时间：2026-11-13 12:00
> 日期范围：2026-11-01 ~ 2026-11-02
> 输出帖子：帖子与发言 1 篇 | 用户提问 1 篇 | 日期范围内 2 篇

---

## ⭐ 无分 | 11-02 09:00 | 星主帖标题 | ID `111`

星主帖正文。

![图1](images/111-1.jpg)

**图片OCR（图1）**：

```
OCR文本
```

评论原文：
**求学**：重点评论
普通用户：普通评论

---

## 🙋 用户提问

> 本章节保留日期范围内抓取到的全部用户提问及回答。

### ❓ ⭐ 无分 | 11-01 08:00 | 提问标题 | ID `222`

用户 提问：

问题正文

---
"""

SUMMARIES = {"111": "<p>星主帖摘要</p>"}
REVIEWED = [
    {
        "post_id": "111",
        "score": 1,
        "scope": "大盘整体",
        "timescale": "未来两个交易日",
        "evidence": "求学评论重点评论偏多",
    }
]
MANIFEST = {
    "111": {"keep": True, "reason": ""},
    "222": {"keep": False, "reason": "非星主发起提问，求学/大牛无实质回答"},
}
JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-payload"


class RenderDuoyuTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.intermediate = self.root / "_temp-file"
        self.intermediate.mkdir()
        self.raw_path = self.intermediate / "1113-多鱼朋友圈_raw_2026-11-01_2026-11-02.md"
        self.raw_path.write_text(RAW, encoding="utf-8")
        self.images_dir = self.raw_path.with_suffix("").parent / (self.raw_path.stem + "_images")
        self.images_dir.mkdir()
        (self.images_dir / "111-1.jpg").write_bytes(JPEG_BYTES)
        self.summaries_path = self.intermediate / "summaries.json"
        self.summaries_path.write_text(
            json.dumps(SUMMARIES, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed_path = self.intermediate / "reviewed.json"
        self.reviewed_path.write_text(
            json.dumps(REVIEWED, ensure_ascii=False), encoding="utf-8"
        )
        self.filter_path = self.intermediate / "filter.json"
        filter_mod.save(MANIFEST, self.filter_path)
        self.draft_path = self.intermediate / "draft.html"
        self.final_path = self.root / "final.html"

    def tearDown(self):
        self._tmp.cleanup()

    def render(self):
        resolver = MODULE.make_image_resolver(self.images_dir)
        draft = MODULE.render_draft(
            raw_text=RAW,
            summaries=MODULE.load_summaries(self.summaries_path),
            focus_owners=MODULE.FOCUS_OWNERS,
            categories=MODULE.CATEGORIES,
            classify=MODULE.classify,
            source_name=MODULE.SOURCE_NAME,
            manifest=filter_mod.load(self.filter_path),
            image_resolver=resolver,
        )
        MODULE.atomic_write(self.draft_path, draft)
        final = MODULE.apply(draft, MODULE.load_json(self.reviewed_path))
        MODULE.atomic_write(self.final_path, final)
        return draft, final

    def test_cli_uses_relocated_sidecar_and_root_final(self):
        argv = ["render_duoyu.py", str(self.raw_path), str(self.summaries_path),
                str(self.reviewed_path), str(self.filter_path), str(self.draft_path),
                str(self.final_path)]
        with patch.object(sys, "argv", argv):
            MODULE.main()
        encoded = base64.b64encode(JPEG_BYTES).decode("ascii")
        self.assertIn(f"data:image/jpeg;base64,{encoded}", self.final_path.read_text(encoding="utf-8"))
        self.assertEqual(self.final_path.parent, self.root)
        self.assertEqual({entry.name for entry in self.root.iterdir()}, {"_temp-file", "final.html"})

    def test_end_to_end_render_apply_validate(self):
        draft, final = self.render()

        # 被排除帖不渲染；kept 帖带正确类别属性（单一时间流，无分栏 section）
        self.assertNotIn('data-post-id="222"', draft)
        self.assertIn('data-post-id="111"', draft)
        self.assertNotIn('<section', draft)
        self.assertNotIn('prefers-color-scheme', draft)
        opening = re.search(r'<article[^>]*data-post-id="111"[^>]*>', draft).group(0)
        self.assertIn('data-category="host-info"', opening)
        self.assertIn('data-ts="2026-11-02 09:00"', opening)
        self.assertIn('style="background:#e7f2e4"', opening)
        self.assertIn('<h3>📝 ', draft)

        # 旁路图片转 base64 内嵌；focus 评论黄名
        encoded = base64.b64encode(JPEG_BYTES).decode("ascii")
        self.assertIn(f'data:image/jpeg;base64,{encoded}', draft)
        self.assertEqual(draft.count('class="focus-name"'), 1)
        self.assertIn("求学", draft)

        # final 注入唯一 market-stance
        self.assertEqual(final.count('class="market-stance"'), 1)
        self.assertIn("市场态度：+1", final)

        errors, stats = validate(
            raw_path=self.raw_path,
            draft_path=self.draft_path,
            reviewed_path=self.reviewed_path,
            final_path=self.final_path,
            focus_owners=MODULE.FOCUS_OWNERS,
            categories=[c[0] for c in MODULE.CATEGORIES],
            manifest_path=self.filter_path,
            classify=MODULE.classify,
        )
        self.assertEqual(errors, [])
        self.assertEqual(stats, {"J": 2, "kept": 1, "excluded": 1,
                                 "counts": {"host-info": 1, "qa": 0}})

    def test_summaries_covering_excluded_post_fail_closed(self):
        manifest = dict(MANIFEST)
        summaries = dict(SUMMARIES, **{"222": "被排除帖不应有摘要"})
        with self.assertRaises(ValueError):
            MODULE.render_draft(
                raw_text=RAW,
                summaries=summaries,
                focus_owners=MODULE.FOCUS_OWNERS,
                categories=MODULE.CATEGORIES,
                classify=MODULE.classify,
                source_name=MODULE.SOURCE_NAME,
                manifest=manifest,
            )

    def test_exclude_without_reason_fail_closed(self):
        manifest = {"111": {"keep": True, "reason": ""}, "222": {"keep": False, "reason": ""}}
        with self.assertRaises(ValueError):
            MODULE.render_draft(
                raw_text=RAW,
                summaries=SUMMARIES,
                focus_owners=MODULE.FOCUS_OWNERS,
                categories=MODULE.CATEGORIES,
                classify=MODULE.classify,
                source_name=MODULE.SOURCE_NAME,
                manifest=manifest,
            )

    def test_image_resolver_returns_none_for_missing_index(self):
        resolver = MODULE.make_image_resolver(self.images_dir)
        self.assertEqual(
            resolver("111", 1), base64.b64encode(JPEG_BYTES).decode("ascii")
        )
        self.assertIsNone(resolver("111", 2))
        self.assertIsNone(resolver("222", 1))
        self.assertIsNone(MODULE.make_image_resolver(self.root / "missing"))


if __name__ == "__main__":
    unittest.main()
