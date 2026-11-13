#!/usr/bin/env python3
"""DGF 1113 真实数据回归：从正式 final HTML 反推 summaries+reviewed（写入 mktemp），
用 __common/render.py(focus_owners={三线文案大锅饭}, categories=星大派/星主普通/用户提问,
classify=DGF 规则, image_resolver=None) + filter.pass_all 生成单一时间流 draft，
__common/apply_stances.py 注入态度得到临时 final，__common/validate.py CLI 退出码 0。

断言：J=25、xingdapai=12、host-posts=13、questions=0、draft focus-name=18、
final market-stance=25；无 <section> 分栏、data-ts 降序、类别图标+浅色背景、
恒浅色主题（无 prefers-color-scheme dark）。
只读 /Users/melo/doc-Dgf 正式交付物，全部输出写临时目录并清理。
"""

from html import unescape
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from __common.filter import pass_all, save as save_manifest
from __common.render import render_draft
from __common.validate import ARTICLE, load_posts, validate

RAW = Path("/Users/melo/doc-Dgf/zsxq_md/1113_大锅饭_raw_2026-11-11_2026-11-13.md")
FINAL = Path("/Users/melo/doc-Dgf/zsxq_md/1113_大锅饭_2026-11-11_2026-11-13.html")
APPLY = ROOT / "__common" / "apply_stances.py"
VALIDATE_CLI = ROOT / "__common" / "validate.py"

OWNERS = {"三线文案大锅饭"}
CATEGORIES = [
    ("xingdapai", "星大派", "🌟", "#fdf0c4"),
    ("host-posts", "星主普通", "📝", "#e7f2e4"),
    ("questions", "用户提问", "🙋", "#fbe3ea"),
]
SOURCE_NAME = "大锅饭与小伙伴的进步空间"
SUMMARY_RE = re.compile(r'<div class="summary">(.*?)</div>', re.S)
STANCE_RE = re.compile(
    r'<p class="market-stance" data-source="analysis">.*?'
    r'<strong>市场态度：([+-]?\d)</strong>｜(.*?)｜(.*?) — 依据：(.*?)</p>', re.S)


def dgf_classify(post):
    if post.is_question:
        return "questions"
    if post.title.startswith("星大派") or "直播总结" in post.title:
        return "xingdapai"
    for line in post.segment.splitlines()[1:]:
        if line.strip():
            if line.replace("**", "").strip().startswith("星大派"):
                return "xingdapai"
            break
    return "host-posts"


@unittest.skipUnless(RAW.exists() and FINAL.exists(), "1113 正式交付物不存在（只读回归数据缺失）")
class Regression1113Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw_text = RAW.read_text(encoding="utf-8")
        cls.final_text = FINAL.read_text(encoding="utf-8")
        cls.tmp = Path(tempfile.mkdtemp(prefix="zsxq-common-1113-"))
        cls.ids = [p.topic_id for p in load_posts(cls.raw_text, OWNERS)]

        cls.summaries = {}
        cls.reviewed = []
        for card in ARTICLE.finditer(cls.final_text):
            post_id, body = card.group(1), card.group(2)
            summary = SUMMARY_RE.search(body)
            stance = STANCE_RE.search(body)
            if not summary or not stance:
                raise AssertionError(f"final 卡片 {post_id} 缺少 summary 或 market-stance")
            cls.summaries[post_id] = summary.group(1)
            score, scope, timescale, evidence = stance.groups()
            cls.reviewed.append({
                "post_id": post_id, "score": int(score),
                "scope": unescape(scope), "timescale": unescape(timescale),
                "evidence": unescape(evidence),
            })

        cls.summaries_path = cls.tmp / "summaries.json"
        cls.summaries_path.write_text(json.dumps(cls.summaries, ensure_ascii=False), encoding="utf-8")
        cls.reviewed_path = cls.tmp / "reviewed.json"
        cls.reviewed_path.write_text(json.dumps(cls.reviewed, ensure_ascii=False), encoding="utf-8")
        cls.manifest = pass_all(cls.ids)
        cls.manifest_path = cls.tmp / "manifest.json"
        save_manifest(cls.manifest, cls.manifest_path)
        cls.raw_path = cls.tmp / "raw.md"
        cls.raw_path.write_text(cls.raw_text, encoding="utf-8")

        draft = render_draft(
            raw_text=cls.raw_text, summaries=cls.summaries, focus_owners=OWNERS,
            categories=CATEGORIES, classify=dgf_classify, source_name=SOURCE_NAME,
            manifest=cls.manifest, image_resolver=None)
        cls.draft_path = cls.tmp / "draft.html"
        cls.draft_path.write_text(draft, encoding="utf-8")
        cls.draft_text = draft

        cls.final_tmp = cls.tmp / "final.html"
        proc = subprocess.run(
            [sys.executable, str(APPLY), str(cls.draft_path),
             str(cls.reviewed_path), str(cls.final_tmp)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"apply_stances 失败：{proc.stdout}{proc.stderr}")
        cls.final_tmp_text = cls.final_tmp.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_extraction_complete(self):
        self.assertEqual(len(self.ids), 25)
        self.assertEqual(len(self.summaries), 25)
        self.assertEqual(len(self.reviewed), 25)

    def test_validate_api_passes_with_counts(self):
        errors, stats = validate(
            raw_path=self.raw_path, draft_path=self.draft_path,
            reviewed_path=self.reviewed_path, final_path=self.final_tmp,
            focus_owners=OWNERS, categories=[k for k, _, _, _ in CATEGORIES],
            manifest_path=self.manifest_path, classify=dgf_classify)
        self.assertEqual(errors, [])
        self.assertEqual(stats["J"], 25)
        self.assertEqual(stats["kept"], 25)
        self.assertEqual(stats["counts"], {"xingdapai": 12, "host-posts": 13, "questions": 0})

    def test_validate_cli_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_CLI), str(self.raw_path), str(self.draft_path),
             str(self.reviewed_path), "--final", str(self.final_tmp),
             "--manifest", str(self.manifest_path),
             "--focus-owners", "三线文案大锅饭",
             "--categories", "xingdapai,host-posts,questions"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("校验通过", proc.stdout)
        self.assertIn("J=25", proc.stdout)

    def test_single_timeline_light_theme(self):
        # 无分栏 section；卡片按 data-ts 降序（最新在上）
        self.assertNotIn("<section", self.draft_text)
        self.assertNotIn("<nav>", self.draft_text)
        ts_list = re.findall(r'data-ts="([^"]+)"', self.draft_text)
        self.assertEqual(len(ts_list), 25)
        self.assertEqual(ts_list, sorted(ts_list, reverse=True))
        # 每卡 data-category + 类别图标 + 浅色背景
        cats = re.findall(r'data-category="([^"]+)"', self.draft_text)
        self.assertEqual(sorted(set(cats)), ["host-posts", "xingdapai"])
        self.assertIn("<h3>🌟 ", self.draft_text)
        self.assertIn("<h3>📝 ", self.draft_text)
        self.assertIn('style="background:#fdf0c4"', self.draft_text)
        self.assertIn('style="background:#e7f2e4"', self.draft_text)
        # 恒浅色主题：draft 与 final 均无深色 media query
        self.assertNotIn("prefers-color-scheme", self.draft_text)
        self.assertNotIn("prefers-color-scheme", self.final_tmp_text)

    def test_focus_and_stance_counts(self):
        self.assertEqual(self.draft_text.count('class="focus-name"'), 18)
        self.assertEqual(
            len(re.findall(r'class="market-stance"[^>]*data-source="analysis"',
                           self.final_tmp_text)), 25)
        cards = ARTICLE.findall(self.final_tmp_text)
        self.assertEqual(len(cards), 25)

    def test_no_global_toggle(self):
        toggle = re.compile(r"toggle[-_]?all|show[-_]?all[-_]?raw|全局(显示|切换|原文)|切换(全部|所有)(原文|显示)", re.I)
        self.assertIsNone(toggle.search(self.draft_text))
        self.assertIsNone(toggle.search(self.final_tmp_text))


if __name__ == "__main__":
    unittest.main()
