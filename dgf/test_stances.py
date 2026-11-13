"""Offline checks for reviewed stance validation and source adapters."""

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from __common.stance import Stance, stance_html, validate


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


DUOYU = module(ROOT / "duoyu/overlay_scores.py", "duoyu_overlay")
DGF = module(ROOT / "dgf/apply_stances.py", "dgf_stance")


class StanceTests(unittest.TestCase):
    def test_five_scores_and_escaping(self):
        for score in range(-2, 3):
            record = Stance("123", score, "光<模块", "短期", '据称 "上涨" & 观察')
            self.assertEqual(validate([record], ["123"])["123"], record)
            html = stance_html(record, origin="人工分析")
            self.assertIn("光&lt;模块", html)
            self.assertIn("&quot;上涨&quot; &amp;", html)
            self.assertNotIn("光<模块", html)

    def test_rejects_missing_extra_duplicate_empty_and_bool(self):
        good = Stance("123", 0, "标的", "未指定", "方向信息不足")
        for records, ids in [([good], ["123", "456"]), ([good], ["456"]), ([good, good], ["123"]), ([good], ["123", "123"]), ([Stance("123", True, "标的", "未指定", "依据")], ["123"]), ([Stance("123", 3, "标的", "未指定", "依据")], ["123"]), ([Stance("123", 0, " ", "未指定", "依据")], ["123"])]:
            with self.subTest(records=records, ids=ids), self.assertRaises(ValueError):
                validate(records, ids)

    def test_duoyu_same_card_idempotence(self):
        raw = '<html><style></style><article class="post-card" data-post-id="123"><pre>图片/OCR原文</pre></article></html>'
        scores = {"123": ("0", "标的", "未指定", "方向信息不足")}
        once = DUOYU.overlay(raw, scores)
        self.assertEqual(DUOYU.overlay(once, scores), once)
        self.assertIn("图片/OCR原文", once)
        self.assertIn('border-left:4px solid var(--gold,var(--star-line))', once)
        self.assertEqual(once.count('/* duoyu-market-overlay:begin */'), 1)
        with self.assertRaises(ValueError):
            DUOYU.overlay(once.replace('/* duoyu-market-overlay:end */', ''), scores)
        with self.assertRaises(ValueError):
            DUOYU.overlay(raw, {"456": scores["123"]})

    def test_dgf_three_sections_and_raw_coverage(self):
        raw = '## ⭐ 无分 | 11-12 10:00 | 标题\n\n正文\n\n---\n'
        draft = ('<!doctype html><style></style><section class="category" id="xingdapai">'
                 '<article class="post-card" data-post-id="a1"><div class="summary"><p>摘要</p></div>'
                 '<details class="raw-block"><summary>查看原文</summary><pre>## ⭐ 无分 | 11-12 10:00 | 标题\n正文</pre></details></article></section>'
                 '<section class="category" id="host-posts"></section><section class="category" id="questions"></section>')
        record = Stance("a1", 0, "整体市场", "未指定", "信息不足")
        result = DGF.apply(raw, draft, [record])
        self.assertEqual(DGF.apply(raw, result, [record]), result)
        self.assertIn('data-source="analysis"', result)
        self.assertIn('border-left:4px solid var(--gold,var(--star-line))', result)
        self.assertEqual(result.count('/* reviewed-stance:begin */'), 1)
        with self.assertRaises(ValueError):
            DGF.apply(raw, result.replace('/* reviewed-stance:end */', ''), [record])
        for bad_raw in [raw + '\n## ⭐ 无分 | 11-12 11:00 | 缺帖', raw.replace('标题', '错误')]:
            with self.assertRaises(ValueError):
                DGF.apply(bad_raw, draft, [record])
        raw_with_id = raw.replace(' | 标题', ' | 标题 | ID `123`')
        draft_with_id = draft.replace('data-post-id="a1"', 'data-post-id="123"').replace(' | 标题', ' | 标题 | ID `123`')
        self.assertIn('市场态度：0', DGF.apply(raw_with_id, draft_with_id, [Stance("123", 0, "整体市场", "未指定", "信息不足")]))
        with self.assertRaises(ValueError):
            DGF.apply(raw_with_id, draft, [record])


if __name__ == "__main__":
    unittest.main()
