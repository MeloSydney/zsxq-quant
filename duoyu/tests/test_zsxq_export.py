#!/usr/bin/env python3
"""多鱼朋友圈导出脚本的离线单元测试。"""

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "zsxq_export.py"
SPEC = importlib.util.spec_from_file_location("duoyu_zsxq_export", MODULE_PATH)
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


def topic(topic_id: int | str, created: str, topic_type: str = "talk") -> dict:
    result = {
        "topic_id": topic_id,
        "type": topic_type,
        "create_time": created,
    }
    if topic_type == "q&a":
        result["question"] = {"text": "问题", "owner": {"name": "用户"}}
        result["answer"] = {"text": "回答", "owner": {"name": "多鱼"}}
    else:
        result["talk"] = {"text": f"帖子 {topic_id}", "owner": {"name": "多鱼"}}
    return result


def page(topics: list[dict], is_end: bool = False) -> dict:
    return {
        "succeeded": True,
        "resp_data": {"topics": topics, "is_end": is_end},
    }


class FetchTopicsTests(unittest.TestCase):
    def test_pagination_audit_deduplicates_and_counts_outside_range(self):
        responses = iter(
            [
                page(
                    [
                        topic(3, "2026-11-02T09:00:00+08:00"),
                        topic(2, "2026-11-01T10:00:00+08:00"),
                    ]
                ),
                page(
                    [
                        topic(2, "2026-11-01T10:00:00+08:00"),
                        topic(1, "2026-10-31T08:00:00+08:00"),
                        topic(4, "2026-10-30T23:59:59+08:00"),
                    ]
                ),
            ]
        )
        requested_urls = []

        def fake_open(request, timeout):
            self.assertEqual(timeout, 30)
            requested_urls.append(request.full_url)
            return FakeResponse(next(responses))

        with patch.object(MODULE.API_OPENER, "open", side_effect=fake_open), patch(
            "time.sleep", return_value=None
        ):
            result = MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

        self.assertEqual([item["topic_id"] for item in result.topics], [3, 2, 1])
        self.assertEqual(result.api_item_count, 5)
        self.assertEqual(result.outside_date_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.page_count, 2)
        self.assertEqual(
            result.api_item_count,
            len(result.topics) + result.outside_date_count + result.duplicate_count,
        )
        self.assertNotIn("end_time=", requested_urls[0])
        self.assertIn(
            "end_time=2026-11-01T10%3A00%3A00%2B08%3A00",
            requested_urls[1],
        )

    def test_mixed_integer_and_decimal_string_topic_ids_deduplicate(self):
        payload = page(
            [
                topic(123, "2026-11-01T09:00:00+08:00"),
                topic("123", "2026-11-01T09:00:00+08:00"),
            ],
            is_end=True,
        )
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            result = MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

        self.assertEqual([item["topic_id"] for item in result.topics], [123])
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.api_item_count, 2)

    def test_fetch_uses_exact_beijing_day_boundaries_for_utc_timestamps(self):
        payload = page(
            [
                topic(4, "2026-11-01T16:00:00+00:00"),  # 北京 11-02 00:00:00
                topic(3, "2026-11-01T15:59:59+00:00"),  # 北京 11-01 23:59:59
                topic(2, "2026-10-31T16:00:00+00:00"),  # 北京 11-01 00:00:00
                topic(1, "2026-10-31T15:59:59+00:00"),  # 北京 10-31 23:59:59
            ],
            is_end=True,
        )
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            result = MODULE.fetch_topics("secret", "2026-11-01", "2026-11-01")

        self.assertEqual([item["topic_id"] for item in result.topics], [3, 2])
        self.assertEqual(result.outside_date_count, 2)
        self.assertEqual(result.api_item_count, 4)

    def test_nonterminal_empty_page_fails(self):
        payload = page([], is_end=False)
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "非终止空页"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

    def test_out_of_order_api_page_fails(self):
        payload = page(
            [
                topic(1, "2026-11-01T09:00:00+08:00"),
                topic(2, "2026-11-02T09:00:00+08:00"),
            ],
            is_end=True,
        )
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "时间不是降序"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

    def test_redirect_is_rejected_without_retrying(self):
        error = MODULE.urllib.error.HTTPError(
            "https://api.zsxq.com", 302, "Found", {}, None
        )
        with patch.object(MODULE.API_OPENER, "open", side_effect=error) as mocked_open:
            with self.assertRaisesRegex(RuntimeError, "为保护 access token 已拒绝"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")
        self.assertEqual(mocked_open.call_count, 1)
        self.assertIsNone(
            MODULE.RejectRedirectHandler().redirect_request(
                None, None, 302, "Found", {}, "https://example.com"
            )
        )

    def test_invalid_timestamp_reports_topic_id_and_fails(self):
        payload = page([topic(99, "not-a-time")], is_end=True)
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "帖子 99 的 create_time 无效"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

    def test_missing_topic_id_fails_instead_of_weakening_deduplication(self):
        item = topic(1, "2026-11-01T10:00:00+08:00")
        del item["topic_id"]
        payload = page([item], is_end=True)
        with patch.object(
            MODULE.API_OPENER, "open", return_value=FakeResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "缺少 topic_id"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

    def test_authentication_failure_is_reported_without_retrying(self):
        error = MODULE.urllib.error.HTTPError(
            "https://api.zsxq.com", 401, "Unauthorized", {}, None
        )
        with patch.object(
            MODULE.API_OPENER, "open", side_effect=error
        ) as mocked_open:
            with self.assertRaisesRegex(RuntimeError, "认证失败.*ZSXQ_ACCESS_TOKEN"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")
        self.assertEqual(mocked_open.call_count, 1)

    def test_three_failed_attempts_abort(self):
        failed = {"succeeded": False, "error": "内部错误"}
        with patch.object(
            MODULE.API_OPENER,
            "open",
            side_effect=[FakeResponse(failed), FakeResponse(failed), FakeResponse(failed)],
        ), patch("time.sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "取消导出以避免生成不完整文件"):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")

    def test_malformed_success_response_retries_then_fails_cleanly(self):
        malformed = {"succeeded": True}
        with patch.object(
            MODULE.API_OPENER,
            "open",
            side_effect=[
                FakeResponse(malformed),
                FakeResponse(malformed),
                FakeResponse(malformed),
            ],
        ) as mocked_open, patch("time.sleep", return_value=None):
            with self.assertRaisesRegex(
                RuntimeError, "最后错误：API 成功响应缺少对象类型的 resp_data"
            ):
                MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")
        self.assertEqual(mocked_open.call_count, 3)

    def test_succeeded_field_must_be_json_boolean(self):
        for invalid_value in ("false", 1, None):
            with self.subTest(invalid_value=invalid_value):
                malformed = {
                    "succeeded": invalid_value,
                    "resp_data": {"topics": [], "is_end": True},
                }
                with patch.object(
                    MODULE.API_OPENER,
                    "open",
                    return_value=FakeResponse(malformed),
                ) as mocked_open, patch("time.sleep", return_value=None):
                    with self.assertRaisesRegex(
                        RuntimeError, "最后错误：API succeeded 必须是布尔值"
                    ):
                        MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")
                self.assertEqual(mocked_open.call_count, 3)

    def test_malformed_topic_fields_retry_then_fail_cleanly(self):
        malformed_topics = []

        invalid_id = topic(1, "2026-11-01T09:00:00+08:00")
        invalid_id["topic_id"] = []
        malformed_topics.append(invalid_id)

        blank_id = topic(8, "2026-11-01T09:00:00+08:00")
        blank_id["topic_id"] = "   "
        malformed_topics.append(blank_id)

        for invalid_topic_id in ("abc", "001", "-1", -1, 0):
            invalid_canonical_id = topic(13, "2026-11-01T09:00:00+08:00")
            invalid_canonical_id["topic_id"] = invalid_topic_id
            malformed_topics.append(invalid_canonical_id)

        invalid_talk = topic(2, "2026-11-01T09:00:00+08:00")
        invalid_talk["talk"] = []
        malformed_topics.append(invalid_talk)

        invalid_text = topic(3, "2026-11-01T09:00:00+08:00")
        invalid_text["talk"]["text"] = 42
        malformed_topics.append(invalid_text)

        invalid_files = topic(4, "2026-11-01T09:00:00+08:00")
        invalid_files["talk"]["files"] = {}
        malformed_topics.append(invalid_files)

        invalid_comments = topic(5, "2026-11-01T09:00:00+08:00")
        invalid_comments["show_comments"] = {}
        malformed_topics.append(invalid_comments)

        null_talk_owner = topic(9, "2026-11-01T09:00:00+08:00")
        null_talk_owner["talk"]["owner"] = None
        malformed_topics.append(null_talk_owner)

        null_comment_owner = topic(10, "2026-11-01T09:00:00+08:00")
        null_comment_owner["show_comments"] = [{"text": "评论", "owner": None}]
        malformed_topics.append(null_comment_owner)

        invalid_question = topic(6, "2026-11-01T09:00:00+08:00", "q&a")
        invalid_question["question"] = []
        malformed_topics.append(invalid_question)

        invalid_answer = topic(7, "2026-11-01T09:00:00+08:00", "q&a")
        invalid_answer["answer"] = []
        malformed_topics.append(invalid_answer)

        null_optional_talk = topic(11, "2026-11-01T09:00:00+08:00", "q&a")
        null_optional_talk["talk"] = None
        malformed_topics.append(null_optional_talk)

        null_optional_answer = topic(12, "2026-11-01T09:00:00+08:00", "q&a")
        null_optional_answer["answer"] = None
        malformed_topics.append(null_optional_answer)

        for malformed_topic in malformed_topics:
            with self.subTest(topic_id=malformed_topic.get("topic_id")):
                payload = page([malformed_topic], is_end=True)
                with patch.object(
                    MODULE.API_OPENER,
                    "open",
                    return_value=FakeResponse(payload),
                ) as mocked_open, patch("time.sleep", return_value=None):
                    with self.assertRaisesRegex(
                        RuntimeError, "最后错误：帖子结构异常"
                    ):
                        MODULE.fetch_topics("secret", "2026-10-31", "2026-11-02")
                self.assertEqual(mocked_open.call_count, 3)


class AuditOutputTests(unittest.TestCase):
    def test_daily_counts_include_zero_dates_and_convert_utc_to_beijing(self):
        topics = [
            topic(2, "2026-11-01T23:30:00+08:00"),
            topic(1, "2026-10-31T16:30:00+00:00"),
        ]
        counts = MODULE.build_daily_counts(topics, "2026-10-31", "2026-11-02")
        self.assertEqual(
            counts,
            {"2026-10-31": 0, "2026-11-01": 2, "2026-11-02": 0},
        )

    def test_question_hashtag_is_preserved_in_html_metadata(self):
        source_topic = topic(1, "2026-11-02T09:00:00+08:00", "q&a")
        source_topic["question"]["text"] = (
            '问题 <e type="hashtag" hid="1" title="%23问题标签" />'
        )
        question = MODULE.format_topic(source_topic)
        self.assertEqual(question["tags"], ["#问题标签"])
        result = MODULE.FetchResult([source_topic], 1, 1, 0, 0)
        html_document = MODULE.generate_html(
            [],
            [question],
            "2026-11-02",
            "2026-11-02",
            result,
            {"2026-11-02": 1},
            filtered_count=0,
            no_filter=True,
        )
        self.assertIn("#问题标签", html_document)
        self.assertEqual(html_document.count('class="post-card"'), 1)

    def test_html_contains_unambiguous_audit_lines(self):
        source_topics = [
            topic(2, "2026-11-02T09:00:00+08:00"),
            topic(1, "2026-11-01T09:00:00+08:00", "q&a"),
        ]
        result = MODULE.FetchResult(
            topics=source_topics,
            api_item_count=3,
            page_count=1,
            outside_date_count=1,
            duplicate_count=0,
        )
        posts = [MODULE.format_topic(source_topics[0])]
        questions = [MODULE.format_topic(source_topics[1])]
        daily_counts = {
            "2026-10-31": 0,
            "2026-11-01": 1,
            "2026-11-02": 1,
        }

        html_document = MODULE.generate_html(
            posts,
            questions,
            "2026-10-31",
            "2026-11-02",
            result,
            daily_counts,
            filtered_count=0,
            no_filter=True,
        )

        self.assertIn("输出帖子：帖子与发言 1 篇 | 用户提问 1 篇 | 日期范围内 2 篇", html_document)
        self.assertIn("API 分页返回 3 条 | 日期范围外 1 条 | 分页重复 0 条", html_document)
        self.assertIn("2026-10-31 0 篇 | 2026-11-01 1 篇 | 2026-11-02 1 篇", html_document)
        self.assertNotIn("（API 返回", html_document)

    def test_html_is_self_contained_escaped_and_structurally_auditable(self):
        posts = [
            {
                "topic_id": 2,
                "type": "talk",
                "created": "2026-11-02T09:00:00+08:00",
                "owner": '作者 <owner> "x"',
                "answer_owner": "",
                "question_text": "",
                "answer_text": "",
                "score": 8.0,
                "title": '<script>alert("x")</script>',
                "text": '正文 & < > " \'',
                "tags": ["标签 <tag>"],
                "files": ['附件 "file"'],
                "comments": [{"owner": "评论者", "text": "评论 <img src=x>"}],
            }
        ]
        questions = [
            {
                "topic_id": 1,
                "type": "q&a",
                "created": "2026-11-01T08:00:00+08:00",
                "owner": "用户",
                "answer_owner": "回答者",
                "question_text": "问题正文",
                "answer_text": "回答正文",
                "score": None,
                "title": "问题",
                "text": "用户 提问：\n\n问题正文",
                "tags": [],
                "files": [],
                "comments": [],
            }
        ]
        result = MODULE.FetchResult(posts + questions, 2, 1, 0, 0)
        html_document = MODULE.generate_html(
            posts,
            questions,
            "2026-11-01",
            "2026-11-02",
            result,
            {"2026-11-01": 1, "2026-11-02": 1},
            filtered_count=0,
            no_filter=True,
        )

        self.assertTrue(html_document.startswith("<!DOCTYPE html>"))
        self.assertIn('<meta charset="utf-8">', html_document)
        self.assertIn('<meta name="viewport"', html_document)
        self.assertIn('<section id="host-posts"', html_document)
        self.assertIn('<section id="questions"', html_document)
        self.assertEqual(html_document.count('class="post-card"'), 2)
        self.assertEqual(set(re.findall(r'data-post-id="([^"]+)"', html_document)), {"1", "2"})
        self.assertEqual(
            re.findall(r'data-timestamp="([^"]+)"', html_document),
            ["2026-11-02T09:00:00+08:00", "2026-11-01T08:00:00+08:00"],
        )
        self.assertNotIn('<script>alert("x")</script>', html_document)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html_document)
        self.assertIn("正文 &amp; &lt; &gt; &quot; &#x27;", html_document)
        self.assertIn("作者 &lt;owner&gt; &quot;x&quot;", html_document)
        self.assertIn("标签 &lt;tag&gt;", html_document)
        self.assertIn("评论 &lt;img src=x&gt;", html_document)
        self.assertNotIn("<script", html_document.lower())
        self.assertNotIn("<link", html_document.lower())
        self.assertNotIn("src=\"http", html_document.lower())

    def test_focus_authors_are_highlighted_by_structured_owner_fields(self):
        host_topic = topic(2, "2026-11-02T09:00:00+08:00")
        host_topic["talk"]["owner"]["name"] = "求学"
        host_topic["show_comments"] = [
            {"owner": {"name": "大牛"}, "text": "重点评论"},
            {"owner": {"name": "普通用户"}, "text": "普通评论"},
        ]
        question_topic = topic(1, "2026-11-01T08:00:00+08:00", "q&a")
        question_topic["answer"]["owner"]["name"] = "求学"
        normal_topic = topic(3, "2026-11-01T07:00:00+08:00")
        normal_topic["talk"]["text"] = "正文中提到 @求学，但作者不是星主"
        posts = [MODULE.format_topic(host_topic), MODULE.format_topic(normal_topic)]
        questions = [MODULE.format_topic(question_topic)]
        result = MODULE.FetchResult(
            [host_topic, question_topic, normal_topic], 3, 1, 0, 0
        )

        html_document = MODULE.generate_html(
            posts,
            questions,
            "2026-11-01",
            "2026-11-02",
            result,
            {"2026-11-01": 2, "2026-11-02": 1},
            filtered_count=0,
            no_filter=True,
        )

        counts = MODULE.build_focus_counts(posts, questions)
        self.assertEqual(
            counts,
            {"cards": 2, "authored": 1, "answers": 1, "comments": 1},
        )
        self.assertIn(
            "重点星主：关联卡片 2 篇 | 发言/提问 1 条 | 回答 1 条 | 评论 1 条",
            html_document,
        )
        self.assertEqual(html_document.count('class="post-card star-highlight"'), 1)
        self.assertEqual(html_document.count('data-focus-star="true"'), 1)
        self.assertIn('<span class="kind">星主发言</span>', html_document)
        self.assertIn('<span class="owner focus-person">求学<span class="star-badge">星主</span>', html_document)
        self.assertIn('<li class="star-reply"><span class="comment-owner focus-person">大牛', html_document)
        self.assertIn('class="qna-part answer-part star-reply"', html_document)
        self.assertIn('<span class="qna-owner focus-person">求学', html_document)
        # 星主回答的问答卡不应在卡片级渲染金色，仅回答区高亮
        question_card = re.search(
            r'<article class="post-card" data-post-id="1".*?</article>',
            html_document,
            re.DOTALL,
        )
        self.assertIsNotNone(question_card)
        self.assertNotIn("star-highlight", question_card.group(0))
        self.assertNotIn("data-focus-star", question_card.group(0))
        self.assertIn('class="qna-part answer-part star-reply"', question_card.group(0))
        normal_card = re.search(
            r'<article class="post-card" data-post-id="3".*?</article>',
            html_document,
            re.DOTALL,
        )
        self.assertIsNotNone(normal_card)
        self.assertNotIn("data-focus-star", normal_card.group(0))

    def test_focus_answer_requires_nonempty_answer_text(self):
        source_topic = topic(1, "2026-11-02T09:00:00+08:00", "q&a")
        source_topic["answer"] = {"text": "", "owner": {"name": "大牛"}}
        question = MODULE.format_topic(source_topic)
        counts = MODULE.build_focus_counts([], [question])
        self.assertEqual(
            counts,
            {"cards": 0, "authored": 0, "answers": 0, "comments": 0},
        )
        result = MODULE.FetchResult([source_topic], 1, 1, 0, 0)
        html_document = MODULE.generate_html(
            [],
            [question],
            "2026-11-02",
            "2026-11-02",
            result,
            {"2026-11-02": 1},
            filtered_count=0,
            no_filter=True,
        )
        self.assertNotIn('class="post-card star-highlight"', html_document)
        self.assertNotIn('data-focus-star="true"', html_document)
        self.assertNotIn('class="qna-part answer-part', html_document)


class CredentialFileTests(unittest.TestCase):
    def test_credential_file_must_be_owned_secure_regular_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            self.assertEqual(MODULE.read_token_from_env(env_file), "fake-token")

            env_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "权限过宽"):
                MODULE.read_token_from_env(env_file)

            env_file.chmod(0o600)
            link = root / "linked.env"
            link.symlink_to(env_file)
            with self.assertRaisesRegex(RuntimeError, "不能是符号链接"):
                MODULE.read_token_from_env(link)

            fifo = root / "token.fifo"
            MODULE.os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(RuntimeError, "必须是普通文件"):
                MODULE.read_token_from_env(fifo)

            invalid_utf8 = root / "invalid-utf8.env"
            invalid_utf8.write_bytes(b"\xff\xfe\xfd")
            invalid_utf8.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "无法安全读取凭据文件"):
                MODULE.read_token_from_env(invalid_utf8)

            real_close = MODULE.os.close

            def close_then_fail(file_descriptor):
                real_close(file_descriptor)
                raise OSError("cleanup failure")

            with patch.object(
                MODULE.os, "fstat", side_effect=OSError("read failure")
            ), patch.object(MODULE.os, "close", side_effect=close_then_fail):
                with self.assertRaisesRegex(
                    RuntimeError, "无法安全读取凭据文件"
                ) as raised:
                    MODULE.read_token_from_env(env_file)
            self.assertEqual(str(raised.exception.__cause__), "read failure")

    def test_path_swap_after_open_does_not_change_read_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=original-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            real_fstat = MODULE.os.fstat

            def replace_path_after_open(file_descriptor):
                env_file.unlink()
                env_file.write_text("ZSXQ_ACCESS_TOKEN=replacement-token\n", encoding="utf-8")
                env_file.chmod(0o600)
                return real_fstat(file_descriptor)

            with patch.object(MODULE.os, "fstat", side_effect=replace_path_after_open):
                token_value = MODULE.read_token_from_env(env_file)

            self.assertEqual(token_value, "original-token")


class MainWorkflowTests(unittest.TestCase):
    def test_main_writes_audited_html_in_time_order_atomically(self):
        source_topics = [
            {
                **topic(1, "2026-11-01T09:00:00+08:00"),
                "talk": {"text": "能量评分9.0分。较早帖子", "owner": {"name": "多鱼"}},
            },
            {
                **topic(2, "2026-11-02T09:00:00+08:00"),
                "talk": {"text": "能量评分1.0分。较新帖子", "owner": {"name": "多鱼"}},
            },
            topic(3, "2026-11-02T08:00:00+08:00", "q&a"),
        ]
        result = MODULE.FetchResult(
            topics=source_topics,
            api_item_count=4,
            page_count=1,
            outside_date_count=1,
            duplicate_count=0,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            argv = [
                "zsxq_export.py",
                "--env-file",
                str(env_file),
                "--start",
                "2026-11-01",
                "--end",
                "2026-11-02",
            ]
            temp_names = []
            real_named_temporary_file = MODULE.tempfile.NamedTemporaryFile

            def tracked_temp_file(*args, **kwargs):
                temp_file = real_named_temporary_file(*args, **kwargs)
                temp_names.append(temp_file.name)
                return temp_file

            with patch.object(MODULE, "OUTPUT_DIR", root), patch.object(
                MODULE, "fetch_topics", return_value=result
            ), patch.object(
                MODULE.tempfile,
                "NamedTemporaryFile",
                side_effect=tracked_temp_file,
            ), patch.object(
                MODULE.os,
                "replace",
                wraps=MODULE.os.replace,
            ) as mocked_replace, patch.object(
                sys, "argv", argv
            ), redirect_stdout(io.StringIO()):
                MODULE.main()
                MODULE.main()

            self.assertEqual(len(temp_names), 2)
            self.assertEqual(len(set(temp_names)), 2)
            self.assertEqual(mocked_replace.call_count, 2)

            outputs = list(root.glob("*-多鱼朋友圈_raw_2026-11-01_2026-11-02.html"))
            self.assertEqual(len(outputs), 1)
            self.assertFalse(list(root.glob("*.tmp")))
            html_document = outputs[0].read_text(encoding="utf-8")
            self.assertTrue(html_document.startswith("<!DOCTYPE html>"))
            self.assertLess(html_document.index("较新帖子"), html_document.index("较早帖子"))
            self.assertIn("日期范围内 3 篇", html_document)
            self.assertIn("API 分页返回 4 条", html_document)
            self.assertIn("2026-11-01 1 篇 | 2026-11-02 2 篇", html_document)
            self.assertEqual(html_document.count('class="post-card"'), 3)

    def test_replace_failure_preserves_old_output_and_removes_temp_file(self):
        source_topics = [topic(1, "2026-11-02T09:00:00+08:00")]
        result = MODULE.FetchResult(source_topics, 1, 1, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            mmdd = MODULE.datetime.now(MODULE.CST).strftime("%m%d")
            output_path = root / f"{mmdd}-多鱼朋友圈_raw_2026-11-02_2026-11-02.html"
            output_path.write_text("existing complete output", encoding="utf-8")
            argv = [
                "zsxq_export.py",
                "--env-file",
                str(env_file),
                "--start",
                "2026-11-02",
                "--end",
                "2026-11-02",
            ]
            with patch.object(MODULE, "OUTPUT_DIR", root), patch.object(
                MODULE, "fetch_topics", return_value=result
            ), patch.object(
                MODULE.os, "replace", side_effect=OSError("simulated replace failure")
            ), patch.object(sys, "argv", argv), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.main()

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing complete output")
            self.assertFalse(list(root.glob("*.tmp")))

    def test_write_failure_preserves_old_output_and_removes_partial_temp(self):
        source_topics = [topic(1, "2026-11-02T09:00:00+08:00")]
        result = MODULE.FetchResult(source_topics, 1, 1, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            mmdd = MODULE.datetime.now(MODULE.CST).strftime("%m%d")
            output_path = root / f"{mmdd}-多鱼朋友圈_raw_2026-11-02_2026-11-02.html"
            output_path.write_text("existing complete output", encoding="utf-8")
            forced_temp_path = root / "forced-partial.tmp"

            class FailingTempFile:
                name = str(forced_temp_path)

                def __enter__(self):
                    forced_temp_path.write_text("partial", encoding="utf-8")
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def write(self, content):
                    raise OSError("simulated write failure")

            argv = [
                "zsxq_export.py",
                "--env-file",
                str(env_file),
                "--start",
                "2026-11-02",
                "--end",
                "2026-11-02",
            ]
            with patch.object(MODULE, "OUTPUT_DIR", root), patch.object(
                MODULE, "fetch_topics", return_value=result
            ), patch.object(
                MODULE.tempfile, "NamedTemporaryFile", return_value=FailingTempFile()
            ), patch.object(
                MODULE.os, "replace"
            ) as mocked_replace, patch.object(
                sys, "argv", argv
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.main()

            mocked_replace.assert_not_called()
            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing complete output")
            self.assertFalse(forced_temp_path.exists())

    def test_output_directory_failure_is_reported_and_preserves_old_output(self):
        source_topics = [topic(1, "2026-11-02T09:00:00+08:00")]
        result = MODULE.FetchResult(source_topics, 1, 1, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            mmdd = MODULE.datetime.now(MODULE.CST).strftime("%m%d")
            output_path = root / f"{mmdd}-多鱼朋友圈_raw_2026-11-02_2026-11-02.html"
            output_path.write_text("existing complete output", encoding="utf-8")
            argv = [
                "zsxq_export.py",
                "--env-file",
                str(env_file),
                "--start",
                "2026-11-02",
                "--end",
                "2026-11-02",
            ]
            with patch.object(MODULE, "OUTPUT_DIR", root), patch.object(
                MODULE, "fetch_topics", return_value=result
            ), patch.object(
                MODULE.Path,
                "mkdir",
                side_effect=PermissionError("simulated mkdir failure"),
            ), patch.object(sys, "argv", argv), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.main()

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing complete output")
            self.assertFalse(list(root.glob("*.tmp")))

    def test_score_filter_removes_only_low_score_hosts_and_keeps_qa(self):
        source_topics = [
            {
                **topic(1, "2026-11-02T09:00:00+08:00"),
                "talk": {"text": "能量评分8.0分。高分星主帖", "owner": {"name": "多鱼"}},
            },
            {
                **topic(2, "2026-11-02T08:00:00+08:00"),
                "talk": {"text": "能量评分3.0分。低分星主帖", "owner": {"name": "多鱼"}},
            },
            topic(3, "2026-11-02T07:00:00+08:00", "q&a"),
        ]
        result = MODULE.FetchResult(source_topics, 3, 1, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("ZSXQ_ACCESS_TOKEN=fake-token\n", encoding="utf-8")
            env_file.chmod(0o600)
            argv = [
                "zsxq_export.py",
                "--env-file",
                str(env_file),
                "--start",
                "2026-11-02",
                "--end",
                "2026-11-02",
                "--min-score",
                "5",
            ]
            with patch.object(MODULE, "OUTPUT_DIR", root), patch.object(
                MODULE, "fetch_topics", return_value=result
            ), patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                MODULE.main()

            html_document = next(root.glob("*-多鱼朋友圈_raw_2026-11-02_2026-11-02.html")).read_text(
                encoding="utf-8"
            )
            self.assertIn("高分星主帖", html_document)
            self.assertNotIn("低分星主帖", html_document)
            self.assertIn('<h4>提问 <span class="qna-owner">用户</span></h4>', html_document)
            self.assertIn("评分过滤 1 篇", html_document)

    def test_filter_options_reject_conflict_and_nonfinite_score(self):
        invalid_argvs = [
            ["zsxq_export.py", "--no-filter", "--min-score", "5"],
            ["zsxq_export.py", "--min-score", "nan"],
            ["zsxq_export.py", "--min-score", "inf"],
        ]
        for argv in invalid_argvs:
            with self.subTest(argv=argv), patch.object(sys, "argv", argv), redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit):
                    MODULE.main()


if __name__ == "__main__":
    unittest.main()
