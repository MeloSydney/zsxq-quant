#!/usr/bin/env python3
"""__common render+filter+validate 最小链路测试。

DGF 形状：三类别（星大派/星主普通/用户提问）、单 focus-owner、pass-all 与排除
两种 manifest、focus 回复+无冒号续行、单一时间流降序+类别图标/浅色背景、
篡改 draft 必须校验失败。
Duoyu 形状：双 focus-owner（求学/大牛）、两类别、1 个被排除帖不打分不渲染、
image_resolver 旁路图嵌入。
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from __common.filter import pass_all, validate_manifest
from __common.render import render_draft
from __common.validate import validate

DGF_RAW = """# 测试星球（原始数据）

> 来源：https://wx.zsxq.com/group/999
> 导出时间：2026-11-13 10:00
> 日期范围：2026-11-11 ~ 2026-11-13
> 星主帖子：3 篇 | 用户提问：1 篇

---
## ⭐ 无分 | 11-12 09:00 | **星大派锐评：测试** | ID `3001`

**星大派锐评：测试** 正文。

评论原文：
　**三线文案大锅饭 回复 某人**：焦点回复内容
普通用户：普通评论
**三线文案大锅饭** 无冒号焦点续行
这是普通续行

---

## ⭐ 7.0分 | 11-13 10:00 | 普通标题 | ID `3002`

普通正文。

---

## ⭐ 无分 | 11-12 20:00 | 广告水帖 | ID `3003`

水帖正文。

---

## 🙋 用户提问

### ❓ ⭐ 无分 | 11-11 08:00 | 提问标题 | ID `3004`

提问正文。

---
"""

DGF_CATEGORIES = [
    ("xingdapai", "星大派", "🌟", "#fdf0c4"),
    ("host-posts", "星主普通", "📝", "#e7f2e4"),
    ("questions", "用户提问", "🙋", "#fbe3ea"),
]
DGF_OWNERS = {"三线文案大锅饭"}


def dgf_classify(post):
    if post.is_question:
        return "questions"
    if post.title.startswith("星大派") or "直播总结" in post.title:
        return "xingdapai"
    first = ""
    for line in post.segment.splitlines()[1:]:
        if line.strip():
            first = line.replace("**", "").strip()
            break
    if first.startswith("星大派"):
        return "xingdapai"
    return "host-posts"


DUOYU_RAW = """# 多鱼测试星球（原始数据）

> 来源：https://wx.zsxq.com/group/888
> 导出时间：2026-11-13 10:00
> 日期范围：2026-11-11 ~ 2026-11-13

---
## ⭐ 无分 | 11-12 09:00 | 星主发言测试 | ID `5001`

星主正文，含图片占位。

评论原文：
　**求学 回复 鱼友**：我觉得可以观望
　**大牛**：明确看多，未来两天偏买入情绪

---

## ⭐ 无分 | 11-12 12:00 | 转发水帖 | ID `5002`

非星主发起、提问为主、星主无实质回答。

---

## 🙋 用户问答

### ❓ ⭐ 无分 | 11-11 08:00 | 鱼友提问 | ID `5003`

提问正文。

评论原文：
求学：不带星号的同名评论不算 focus

---
"""

DUOYU_CATEGORIES = [
    ("host-info", "星主发言与信息帖", "📝", "#e7f2e4"),
    ("qa", "用户问答", "🙋", "#fbe3ea"),
]
DUOYU_OWNERS = {"求学", "大牛"}


def duoyu_classify(post):
    return "qa" if post.is_question else "host-info"


def reviewed_for(ids):
    return [
        {"post_id": i, "score": 1, "scope": "测试范围", "timescale": "未来两个交易日",
         "evidence": "测试依据：正文原句"}
        for i in ids
    ]


class PipelineCase(unittest.TestCase):
    def run_validate(self, tmp, raw_text, draft_html, reviewed, kept, *,
                     owners, categories, manifest=None, classify=None, final_html=None):
        raw_path = tmp / "raw.md"
        raw_path.write_text(raw_text, encoding="utf-8")
        draft_path = tmp / "draft.html"
        draft_path.write_text(draft_html, encoding="utf-8")
        reviewed_path = tmp / "reviewed.json"
        reviewed_path.write_text(json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")
        manifest_path = None
        if manifest is not None:
            manifest_path = tmp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        final_path = None
        if final_html is not None:
            final_path = tmp / "final.html"
            final_path.write_text(final_html, encoding="utf-8")
        return validate(
            raw_path=raw_path, draft_path=draft_path, reviewed_path=reviewed_path,
            final_path=final_path, focus_owners=owners, categories=categories,
            manifest_path=manifest_path, classify=classify,
        ), draft_path, reviewed_path, manifest_path


class DgfShapeTest(PipelineCase):
    ids = ["3001", "3002", "3003", "3004"]

    def render(self, manifest=None, summaries=None, **kw):
        return render_draft(
            raw_text=DGF_RAW,
            summaries=summaries if summaries is not None else {i: f"<p>{i} 浓缩。</p>" for i in self.ids},
            focus_owners=DGF_OWNERS, categories=DGF_CATEGORIES, classify=dgf_classify,
            source_name="测试星球", manifest=manifest, **kw,
        )

    def test_pass_all_renders_and_validates(self):
        manifest = pass_all(self.ids)
        draft = self.render(manifest=manifest)
        self.assertNotIn("market-stance", draft)
        self.assertNotIn(" open>", draft.split("<pre>")[0])
        # 恒浅色主题：无深色 media query
        self.assertNotIn("prefers-color-scheme", draft)
        # 单一时间流：无分栏 section，跨类别按 data-ts 降序（最新在上）
        self.assertNotIn("<section", draft)
        order = re.findall(r'data-post-id="(\d+)"', draft)
        self.assertEqual(order, ["3002", "3003", "3001", "3004"])
        ts_list = re.findall(r'data-ts="([^"]+)"', draft)
        self.assertEqual(ts_list, sorted(ts_list, reverse=True))
        # 类别图标 + 浅色背景：xingdapai 🌟/#fdf0c4、host-posts 📝/#e7f2e4、
        # questions 🙋/#fbe3ea（三色色相差异大、均为浅色）
        self.assertIn("<h3>🌟 ", draft)
        self.assertIn("<h3>📝 ", draft)
        self.assertIn("<h3>🙋 ", draft)
        self.assertIn('style="background:#fdf0c4"', draft)
        self.assertIn('style="background:#fbe3ea"', draft)
        opening2 = re.search(r'<article[^>]*data-post-id="3002"[^>]*>', draft).group(0)
        self.assertIn('data-category="host-posts"', opening2)
        self.assertIn('style="background:#e7f2e4"', opening2)
        opening1 = re.search(r'<article[^>]*data-post-id="3001"[^>]*>', draft).group(0)
        self.assertIn('data-category="xingdapai"', opening1)
        self.assertIn('data-ts="2026-11-12 09:00"', opening1)
        with tempfile.TemporaryDirectory() as tmp:
            (errors, stats), *_ = self.run_validate(
                Path(tmp), DGF_RAW, draft, reviewed_for(self.ids), self.ids,
                owners=DGF_OWNERS, categories=DGF_CATEGORIES, manifest=manifest,
                classify=dgf_classify)
        self.assertEqual(errors, [])
        self.assertEqual(stats, {"J": 4, "kept": 4, "excluded": 0,
                                 "counts": {"xingdapai": 1, "host-posts": 2, "questions": 1}})
        # 3001：焦点回复 1 条 + 无冒号焦点续行 1 处 → 2 个 focus-name span
        card = re.search(r'data-post-id="3001".*?</article>', draft, re.S).group(0)
        self.assertEqual(card.count('class="focus-name"'), 2)
        self.assertEqual(card.count('class="cname"'), 1)
        self.assertIn('class="comment reply"', card)
        self.assertEqual(len(re.findall(r'class="comment(?: reply)?"', card)), 2)
        self.assertIn('<p class="no-comments">', draft)

    def test_excluded_post_not_rendered(self):
        manifest = pass_all(self.ids)
        manifest["3003"] = {"keep": False, "reason": "广告水帖"}
        kept = ["3001", "3002", "3004"]
        draft = self.render(manifest=manifest,
                            summaries={i: f"<p>{i} 浓缩。</p>" for i in kept})
        self.assertNotIn("3003", draft)
        with tempfile.TemporaryDirectory() as tmp:
            (errors, stats), *_ = self.run_validate(
                Path(tmp), DGF_RAW, draft, reviewed_for(kept), kept,
                owners=DGF_OWNERS, categories=DGF_CATEGORIES, manifest=manifest,
                classify=dgf_classify)
        self.assertEqual(errors, [])
        self.assertEqual(stats["excluded"], 1)
        self.assertEqual(stats["counts"], {"xingdapai": 1, "host-posts": 1, "questions": 1})

    def test_reviewed_must_cover_only_kept(self):
        manifest = pass_all(self.ids)
        manifest["3003"] = {"keep": False, "reason": "广告水帖"}
        kept = ["3001", "3002", "3004"]
        draft = self.render(manifest=manifest,
                            summaries={i: f"<p>{i} 浓缩。</p>" for i in kept})
        with tempfile.TemporaryDirectory() as tmp:
            (errors, _), *_ = self.run_validate(
                Path(tmp), DGF_RAW, draft, reviewed_for(self.ids), kept,
                owners=DGF_OWNERS, categories=DGF_CATEGORIES, manifest=manifest)
        self.assertTrue(any("reviewed" in e and "3003" in e for e in errors))


    def test_summaries_must_match_kept(self):
        manifest = pass_all(self.ids)
        manifest["3003"] = {"keep": False, "reason": "广告水帖"}
        with self.assertRaises(ValueError):
            self.render(manifest=manifest)  # summaries 仍含 3003

    def test_exclude_without_reason_fails_render(self):
        manifest = pass_all(self.ids)
        manifest["3003"] = {"keep": False, "reason": "  "}
        errors, _ = validate_manifest(manifest, self.ids)
        self.assertTrue(errors)
        with self.assertRaises(ValueError):
            self.render(manifest=manifest,
                        summaries={i: f"<p>{i} 浓缩。</p>" for i in self.ids})

    def test_tampered_draft_fails(self):
        draft = self.render()
        with tempfile.TemporaryDirectory() as tmp:
            good, *_ = self.run_validate(Path(tmp), DGF_RAW, draft,
                                         reviewed_for(self.ids), self.ids,
                                         owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertEqual(good[0], [])
            # focus span 被降级 → focus 计数不符
            tampered = draft.replace('class="focus-name"', 'class="cname"', 1)
            (errors, _), *_ = self.run_validate(Path(tmp), DGF_RAW, tampered,
                                                reviewed_for(self.ids), self.ids,
                                                owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertTrue(any("focus-owner" in e for e in errors))
            # raw-block 内容被改 → 逐字校验失败
            tampered2 = draft.replace("普通正文。", "被篡改的正文。", 1)
            (errors2, _), *_ = self.run_validate(Path(tmp), DGF_RAW, tampered2,
                                                 reviewed_for(self.ids), self.ids,
                                                 owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertTrue(any("raw-block" in e for e in errors2))
            # 全局开关 → 禁止
            tampered3 = draft.replace("</main>", "<button>全局显示原文</button></main>")
            (errors3, _), *_ = self.run_validate(Path(tmp), DGF_RAW, tampered3,
                                                 reviewed_for(self.ids), self.ids,
                                                 owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertTrue(any("全局" in e for e in errors3))
            # data-ts 顺序被打乱 → 降序校验失败（交换前两张卡 3002/3003）
            cards = re.findall(r'<article class="post-card".*?</article>', draft, re.S)
            swapped = (draft.replace(cards[0], "@@A@@", 1)
                       .replace(cards[1], cards[0], 1)
                       .replace("@@A@@", cards[1], 1))
            (errors4, _), *_ = self.run_validate(Path(tmp), DGF_RAW, swapped,
                                                 reviewed_for(self.ids), self.ids,
                                                 owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertTrue(any("降序" in e for e in errors4))
            # 未知类别 → 不在期望类别集合
            tampered5 = draft.replace('data-category="xingdapai"', 'data-category="nope"', 1)
            (errors5, _), *_ = self.run_validate(Path(tmp), DGF_RAW, tampered5,
                                                 reviewed_for(self.ids), self.ids,
                                                 owners=DGF_OWNERS, categories=DGF_CATEGORIES)
            self.assertTrue(any("不在期望类别集合" in e for e in errors5))

    def test_wrong_category_detected_by_classify(self):
        draft = self.render()
        # 把 3001 卡片的 data-category 从 xingdapai 改成 host-posts
        tampered = draft.replace('data-category="xingdapai"', 'data-category="host-posts"', 1)
        with tempfile.TemporaryDirectory() as tmp:
            (errors, _), *_ = self.run_validate(Path(tmp), DGF_RAW, tampered,
                                                reviewed_for(self.ids), self.ids,
                                                owners=DGF_OWNERS, categories=DGF_CATEGORIES,
                                                classify=dgf_classify)
        self.assertTrue(any("classify" in e and "3001" in e for e in errors))


class DuoyuShapeTest(PipelineCase):
    all_ids = ["5001", "5002", "5003"]
    kept = ["5001", "5003"]

    def manifest(self):
        m = pass_all(self.all_ids)
        m["5002"] = {"keep": False, "reason": "非星主发起+提问为主+星主无实质回答"}
        return m

    def render(self, image_resolver=None):
        return render_draft(
            raw_text=DUOYU_RAW,
            summaries={i: f"<p>{i} 浓缩。</p>" for i in self.kept},
            focus_owners=DUOYU_OWNERS, categories=DUOYU_CATEGORIES, classify=duoyu_classify,
            source_name="多鱼测试星球", manifest=self.manifest(),
            image_resolver=image_resolver,
        )

    def test_dual_owner_exclusion_and_images(self):
        images = {("5001", 1): "QUJD", ("5001", 2): "REVG"}
        resolver = lambda pid, idx: images.get((pid, idx))
        draft = self.render(image_resolver=resolver)
        self.assertNotIn("5002", draft)
        self.assertNotIn("转发水帖", draft)
        # 单一时间流：无分栏，data-ts 降序（5001 在 5003 前）
        self.assertNotIn("<section", draft)
        self.assertNotIn("prefers-color-scheme", draft)
        self.assertLess(draft.index('data-post-id="5001"'), draft.index('data-post-id="5003"'))
        opening = re.search(r'<article[^>]*data-post-id="5001"[^>]*>', draft).group(0)
        self.assertIn('data-category="host-info"', opening)
        self.assertIn('style="background:#e7f2e4"', opening)
        self.assertIn("<h3>📝 ", draft)
        opening3 = re.search(r'<article[^>]*data-post-id="5003"[^>]*>', draft).group(0)
        self.assertIn('data-category="qa"', opening3)
        self.assertIn('style="background:#fbe3ea"', opening3)
        self.assertIn("<h3>🙋 ", draft)
        card = re.search(r'data-post-id="5001".*?</article>', draft, re.S).group(0)
        # 双 focus-owner：求学(回复) + 大牛 → 2 个 focus-name
        self.assertEqual(card.count('class="focus-name"'), 2)
        self.assertIn('<span class="focus-name">求学</span>', card)
        self.assertIn('<span class="focus-name">大牛</span>', card)
        # 旁路图嵌入
        self.assertEqual(card.count("data:image/jpeg;base64,"), 2)
        self.assertIn("data:image/jpeg;base64,QUJD", card)
        # 5003：不带 ** 的同名评论不算 focus
        card3 = re.search(r'data-post-id="5003".*?</article>', draft, re.S).group(0)
        self.assertEqual(card3.count('class="focus-name"'), 0)
        self.assertEqual(card3.count('class="cname"'), 1)
        with tempfile.TemporaryDirectory() as tmp:
            (errors, stats), *_ = self.run_validate(
                Path(tmp), DUOYU_RAW, draft, reviewed_for(self.kept), self.kept,
                owners=DUOYU_OWNERS, categories=DUOYU_CATEGORIES,
                manifest=self.manifest(), classify=duoyu_classify)
        self.assertEqual(errors, [])
        self.assertEqual(stats, {"J": 3, "kept": 2, "excluded": 1,
                                 "counts": {"host-info": 1, "qa": 1}})

    def test_no_resolver_no_images(self):
        draft = self.render()
        self.assertNotIn("data:image", draft)
        self.assertNotIn('<div class="images">', draft)


if __name__ == "__main__":
    unittest.main()
