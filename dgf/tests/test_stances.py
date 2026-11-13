"""Offline checks for reviewed stance validation and the shared stance injector."""

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from __common.stance import Stance, stance_html, validate


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ADAPTER = module(ROOT / "__common/apply_stances.py", "shared_stance")

DRAFT = (
    '<!doctype html><html><head><style></style></head><body>'
    '<article class="post-card" data-post-id="a1"><div class="summary"><p>摘要</p></div>'
    '<details class="raw-block"><summary>查看原文</summary><pre>原文</pre></details></article>'
    '</body></html>'
)


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

    def test_shared_adapter_inject_idempotent_and_origin(self):
        record = Stance("a1", 0, "整体市场", "未来两个交易日", "方向信息不足")
        once = ADAPTER.apply(DRAFT, [record])
        self.assertEqual(ADAPTER.apply(once, [record]), once)
        self.assertIn('data-source="analysis"', once)
        self.assertIn('border-left:4px solid var(--gold,var(--star-line))', once)
        self.assertEqual(once.count('/* reviewed-stance:begin */'), 1)
        self.assertIn("人工分析标注 · 非原帖能量评分", once)
        self.assertIn("市场态度：0", once)

    def test_shared_adapter_rejects_incomplete_coverage(self):
        with self.assertRaises(ValueError):
            ADAPTER.apply(DRAFT, [Stance("zzz", 0, "标的", "未指定", "依据")])

    def test_shared_adapter_rejects_bad_structure(self):
        no_summary = DRAFT.replace('<div class="summary"><p>摘要</p></div>', '')
        with self.assertRaises(ValueError):
            ADAPTER.apply(no_summary, [Stance("a1", 0, "标的", "未指定", "依据")])
        open_block = DRAFT.replace('<details class="raw-block">', '<details class="raw-block" open>')
        with self.assertRaises(ValueError):
            ADAPTER.apply(open_block, [Stance("a1", 0, "标的", "未指定", "依据")])


if __name__ == "__main__":
    unittest.main()
