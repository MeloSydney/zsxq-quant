#!/usr/bin/env python3
"""__common/filter.py 单测：pass_all、覆盖守恒、schema、load/save 往返。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from __common.filter import kept_ids, load, pass_all, save, validate_manifest


class FilterTest(unittest.TestCase):
    ids = ["101", "102", "103"]

    def test_pass_all(self):
        manifest = pass_all(self.ids)
        self.assertEqual(set(manifest), set(self.ids))
        errors, counts = validate_manifest(manifest, self.ids)
        self.assertEqual(errors, [])
        self.assertEqual(counts, {"keep": 3, "exclude": 0, "total": 3})
        self.assertEqual(kept_ids(manifest, self.ids), self.ids)

    def test_exclude_requires_reason(self):
        manifest = pass_all(self.ids)
        manifest["102"] = {"keep": False, "reason": ""}
        errors, counts = validate_manifest(manifest, self.ids)
        self.assertTrue(any("102" in e and "reason" in e for e in errors))
        self.assertEqual(counts["exclude"], 1)
        manifest["102"]["reason"] = "非星主发起+提问为主+星主无实质回答"
        errors, counts = validate_manifest(manifest, self.ids)
        self.assertEqual(errors, [])
        self.assertEqual(kept_ids(manifest, self.ids), ["101", "103"])

    def test_coverage_conservation(self):
        manifest = pass_all(self.ids)
        del manifest["103"]
        manifest["999"] = {"keep": True, "reason": ""}
        errors, _ = validate_manifest(manifest, self.ids)
        self.assertTrue(any("缺少" in e and "103" in e for e in errors))
        self.assertTrue(any("之外" in e and "999" in e for e in errors))

    def test_bad_entry_shape(self):
        manifest = pass_all(self.ids)
        manifest["101"] = {"keep": "yes", "reason": ""}
        errors, _ = validate_manifest(manifest, self.ids)
        self.assertTrue(any("keep 必须是布尔值" in e for e in errors))
        manifest["101"] = {"keep": True}
        errors, _ = validate_manifest(manifest, self.ids)
        self.assertTrue(any("只能包含" in e for e in errors))

    def test_save_load_roundtrip(self):
        manifest = pass_all(self.ids)
        manifest["102"] = {"keep": False, "reason": "水帖"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            save(manifest, path)
            self.assertEqual(load(path), manifest)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["102"]["reason"], "水帖")
        with self.assertRaises(ValueError):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "bad.json"
                path.write_text("[]", encoding="utf-8")
                load(path)


if __name__ == "__main__":
    unittest.main()
