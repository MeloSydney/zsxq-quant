#!/usr/bin/env python3
"""大锅饭导出脚本的窄范围离线回归测试。"""

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "zsxq_export.py"
SPEC = importlib.util.spec_from_file_location("dgf_zsxq_export", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FetchTopicsRegressionTests(unittest.TestCase):
    def test_authentication_failure_after_retries_fails_closed(self):
        error = urllib.error.HTTPError(
            "https://api.zsxq.com", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with patch.object(MODULE.urllib.request, "urlopen", side_effect=error) as mocked_open, patch(
            "time.sleep", return_value=None
        ), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "取消导出"):
                MODULE.fetch_topics("secret", "2026-11-01", "2026-11-02")
        self.assertEqual(mocked_open.call_count, 3)

    def test_business_error_after_retries_fails_closed(self):
        failed = {"succeeded": False, "error": "内部错误"}
        with patch.object(
            MODULE.urllib.request,
            "urlopen",
            side_effect=[FakeResponse(failed), FakeResponse(failed), FakeResponse(failed)],
        ) as mocked_open, patch("time.sleep", return_value=None), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "取消导出"):
                MODULE.fetch_topics("secret", "2026-11-01", "2026-11-02")
        self.assertEqual(mocked_open.call_count, 3)

    def test_overlapping_pages_keep_first_topic_and_advance_cursor(self):
        first = {"topic_id": 123, "create_time": "2026-11-02T10:00:00+08:00"}
        overlap = {"topic_id": 123, "create_time": "2026-11-02T09:00:00+08:00"}
        second = {"topic_id": 456, "create_time": "2026-11-02T08:00:00+08:00"}
        pages = [
            {"succeeded": True, "resp_data": {"topics": [first], "is_end": False}},
            {"succeeded": True, "resp_data": {"topics": [overlap, second], "is_end": True}},
        ]
        with patch.object(MODULE.urllib.request, "urlopen", side_effect=map(FakeResponse, pages)) as mocked_open, patch(
            "time.sleep", return_value=None
        ):
            topics = MODULE.fetch_topics("secret", "2026-11-01", "2026-11-02")
        self.assertEqual(topics, [first, second])
        self.assertEqual(len({str(t["topic_id"]) for t in topics}), len(topics))
        self.assertEqual(mocked_open.call_count, 2)
        self.assertIn("end_time=2026-11-02T10%3A00%3A00%2B08%3A00", mocked_open.call_args_list[1].args[0].full_url)

    def test_missing_topic_id_fails_closed(self):
        page = {"succeeded": True, "resp_data": {"topics": [
            {"create_time": "2026-11-02T10:00:00+08:00"}
        ], "is_end": True}}
        with patch.object(MODULE.urllib.request, "urlopen", return_value=FakeResponse(page)):
            with self.assertRaisesRegex(RuntimeError, "缺少 topic_id"):
                MODULE.fetch_topics("secret", "2026-11-01", "2026-11-02")


class TopicIdContractTests(unittest.TestCase):
    def test_markdown_headings_include_original_topic_id(self):
        posts = [
            {
                "topic_id": "123",
                "created": "2026-11-01T10:00:00+08:00",
                "score": 8,
                "title": "标题",
                "text": "正文",
                "tags": [],
                "files": [],
                "comments": [],
                "ocr": "",
            }
        ]
        questions = [dict(posts[0], topic_id="456", score=None, title="问题")]
        markdown = MODULE.generate_markdown(
            posts,
            questions,
            "2026-11-01",
            "2026-11-02",
            2,
            0,
            True,
        )

        self.assertIn("## ⭐ 8分 | 11-01 10:00 | 标题 | ID `123`", markdown)
        self.assertIn("### ❓ ⭐ 无分 | 11-01 10:00 | 问题 | ID `456`", markdown)


if __name__ == "__main__":
    unittest.main()
