#!/usr/bin/env python3
"""知识星球 API 导出脚本 - 使用 zsxq API 直接获取完整帖子，无需浏览器展开"""

import base64
import errno
import html
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common.fields import extract_files, extract_post_title, extract_score, extract_tags

GROUP_ID = "15284854485542"
GROUP_NAME = "多鱼朋友圈"
GROUP_URL = f"https://wx.zsxq.com/group/{GROUP_ID}"
API_BASE = "https://api.zsxq.com/v2/groups"
OUTPUT_DIR = Path("/Users/melo/doc-Dgf/duoyu")
CST = timezone(timedelta(hours=8))
FOCUS_AUTHORS = frozenset({"求学", "大牛"})


def is_focus_author(name: str) -> bool:
    """判断作者是否为需要重点标注的星主。"""
    return name.strip() in FOCUS_AUTHORS


# 默认读取本 skill 套件根目录的 .env
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝所有 API 重定向，避免 Cookie 被转发到其他地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


API_OPENER = urllib.request.build_opener(RejectRedirectHandler)


@dataclass(frozen=True)
class FetchResult:
    """API 分页抓取结果及可核对的数量审计。"""

    topics: list[dict]
    api_item_count: int
    page_count: int
    outside_date_count: int
    duplicate_count: int


def read_token_from_env(env_path: Path) -> str | None:
    """通过单个安全文件描述符读取 access token，拒绝链接和非普通文件。"""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("当前系统不支持安全读取凭据文件（缺少 O_NOFOLLOW）")
    flags |= os.O_NOFOLLOW

    file_descriptor = None
    try:
        try:
            file_descriptor = os.open(env_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(f"凭据文件不能是符号链接：{env_path}") from exc
            raise RuntimeError(f"无法安全打开凭据文件 {env_path}：{exc}") from exc

        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError(f"凭据文件必须是普通文件：{env_path}")
        if file_stat.st_uid != os.getuid():
            raise RuntimeError(f"凭据文件不属于当前用户：{env_path}")
        if file_stat.st_mode & 0o077:
            raise RuntimeError(f"凭据文件权限过宽，请执行 chmod 600 {env_path}")

        token_file = os.fdopen(file_descriptor, "r", encoding="utf-8")
        file_descriptor = None
        with token_file:
            lines = token_file.read().splitlines()
    except RuntimeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"无法安全读取凭据文件 {env_path}（{type(exc).__name__}）"
        ) from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                # 仅当 fd 尚未移交给 fdopen 且已有主异常时才会到达这里；
                # 清理失败不能覆盖上方已净化的凭据读取错误。
                pass

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # KEY=VALUE 格式
        if "=" in line:
            key, _, val = line.partition("=")
            if key.strip().upper() == "ZSXQ_ACCESS_TOKEN":
                return val.strip().strip("\"'\"")
        else:
            # 纯 token（UUID_UUID 格式）
            if re.match(r"^[0-9A-Fa-f-]+_[0-9A-Fa-f]+$", line):
                return line
    return None


def validate_topic_structure(topic: dict) -> None:
    """验证格式化阶段会访问的 topic 字段，避免畸形结构延迟触发 traceback。"""
    topic_id = topic.get("topic_id")
    if isinstance(topic_id, bool):
        raise ValueError("帖子结构异常：topic_id 必须是正整数")
    if isinstance(topic_id, int):
        if topic_id <= 0:
            raise ValueError("帖子结构异常：topic_id 必须是正整数")
    elif isinstance(topic_id, str):
        if re.fullmatch(r"[1-9][0-9]*", topic_id) is None:
            raise ValueError(
                "帖子结构异常：topic_id 字符串必须是无前导零的十进制正整数"
            )
        topic["topic_id"] = int(topic_id)
    elif topic_id is not None:
        raise ValueError("帖子结构异常：topic_id 必须是正整数")

    topic_type = topic.get("type")
    if not isinstance(topic_type, str) or not topic_type:
        raise ValueError("帖子结构异常：type 必须是非空字符串")

    def validate_content(section_name: str, required: bool) -> dict | None:
        if section_name not in topic:
            if required:
                raise ValueError(f"帖子结构异常：{section_name} 必须是对象")
            return None
        section = topic[section_name]
        if not isinstance(section, dict):
            raise ValueError(f"帖子结构异常：{section_name} 必须是对象")
        if "text" in section and not isinstance(section["text"], str):
            raise ValueError(f"帖子结构异常：{section_name}.text 必须是字符串")
        if "owner" in section:
            owner = section["owner"]
            if not isinstance(owner, dict):
                raise ValueError(f"帖子结构异常：{section_name}.owner 必须是对象")
            if "name" in owner and not isinstance(owner["name"], str):
                raise ValueError(
                    f"帖子结构异常：{section_name}.owner.name 必须是字符串"
                )
        return section

    talk = validate_content("talk", required=topic_type != "q&a")
    validate_content("question", required=topic_type == "q&a")
    validate_content("answer", required=False)

    if talk is not None:
        files = talk.get("files", [])
        if not isinstance(files, list):
            raise ValueError("帖子结构异常：talk.files 必须是数组")
        for file_item in files:
            if not isinstance(file_item, dict):
                raise ValueError("帖子结构异常：talk.files 条目必须是对象")
            if "name" in file_item and not isinstance(file_item["name"], str):
                raise ValueError("帖子结构异常：talk.files.name 必须是字符串")
    for section_name in ("talk", "question", "answer"):
        section = topic.get(section_name) or {}
        images = section.get("images", [])
        if not isinstance(images, list):
            raise ValueError(f"帖子结构异常：{section_name}.images 必须是数组")

    comments = topic.get("show_comments", [])
    if not isinstance(comments, list):
        raise ValueError("帖子结构异常：show_comments 必须是数组")
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("帖子结构异常：show_comments 条目必须是对象")
        if "text" in comment and not isinstance(comment["text"], str):
            raise ValueError("帖子结构异常：show_comments.text 必须是字符串")
        if "owner" in comment:
            owner = comment["owner"]
            if not isinstance(owner, dict):
                raise ValueError("帖子结构异常：show_comments.owner 必须是对象")
            if "name" in owner and not isinstance(owner["name"], str):
                raise ValueError(
                    "帖子结构异常：show_comments.owner.name 必须是字符串"
                )

def extract_images(topic: dict) -> list[dict[str, str]]:
    """Collect attached image URLs from talk, question and answer without dropping image-only posts."""
    images = []
    for section_name in ("talk", "question", "answer"):
        section = topic.get(section_name) or {}
        for image in section.get("images", []):
            if not isinstance(image, dict):
                raise ValueError(f"帖子结构异常：{section_name}.images 条目必须是对象")
            url = next((size["url"] for key in ("original", "large", "thumbnail")
                        if isinstance(size := image.get(key), dict)
                        and isinstance(size.get("url"), str) and size["url"]), None)
            if url is None:
                raise ValueError(f"帖子结构异常：{section_name}.images 缺少图片 URL")
            images.append({"section": section_name, "url": url})
    return images


def recognize_images(posts: list[dict]) -> None:
    """Embed actual image bytes and Vision OCR; never invent text when recognition fails."""
    image_refs = [(post, image) for post in posts for image in post["images"]]
    if not image_refs:
        return
    with tempfile.TemporaryDirectory(prefix="duoyu-ocr-") as directory:
        paths = []
        for index, (post, image) in enumerate(image_refs):
            url = image["url"]
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or not (
                parsed.hostname == "zsxq.com" or parsed.hostname.endswith(".zsxq.com")
            ):
                raise RuntimeError(f"图片 URL 不属于可信 HTTPS 星球域名：{parsed.hostname}")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with API_OPENER.open(request, timeout=30) as response:
                    mime = response.headers.get_content_type()
                    if mime not in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/gif"):
                        raise RuntimeError(f"图片响应格式不支持：{mime}")
                    payload = response.read(20_000_001)
                    if len(payload) > 20_000_000:
                        raise RuntimeError("单张图片超过 20 MB")
            except (OSError, urllib.error.URLError) as exc:
                raise RuntimeError(f"图片下载失败（帖子 {post['topic_id']}）：{exc}") from exc
            image["data_uri"] = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
            path = Path(directory) / f"image-{index}"
            path.write_bytes(payload)
            paths.append(str(path))
        try:
            result = subprocess.run(["swift", str(Path(__file__).with_name("recognize_images.swift")), *paths],
                                    capture_output=True, text=True, check=True, timeout=300)
            recognized = json.loads(result.stdout)
            if not isinstance(recognized, list) or len(recognized) != len(paths) or any(not isinstance(text, str) for text in recognized):
                raise RuntimeError("OCR 返回条目数或类型不匹配")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"图片 OCR 失败：{exc.stderr.strip()}") from exc
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise RuntimeError(f"图片 OCR 失败：{exc}") from exc
        for (_, image), text in zip(image_refs, recognized):
            image["ocr"] = text.strip()


def decode_e_tags(s: str) -> str:
    """解码知识星球自定义 <e> 标签为纯文本。"""
    if not s:
        return ""
    # Bold: <e type="text_bold" title="xxx" />
    s = re.sub(
        r'<e type="text_bold" title="([^"]*?)"\s*/>',
        lambda m: urllib.parse.unquote(m.group(1)),
        s,
    )
    # Hashtag: <e type="hashtag" hid="xxx" title="xxx" />
    s = re.sub(
        r'<e type="hashtag"[^>]*title="([^"]*?)"\s*/>',
        lambda m: urllib.parse.unquote(m.group(1)),
        s,
    )
    # Mention: <e type="mention" uid="xxx" title="@xxx" />
    s = re.sub(
        r'<e type="mention"[^>]*title="([^"]*?)"\s*/>',
        lambda m: urllib.parse.unquote(m.group(1)),
        s,
    )
    # Other e tags with title
    s = re.sub(
        r'<e type="[^"]*"[^>]*title="([^"]*?)"[^>]*?/?>',
        lambda m: urllib.parse.unquote(m.group(1)),
        s,
    )
    # Remove remaining e tags
    s = re.sub(r"<e[^>]*/?>", "", s)
    return s


def fetch_topics(token: str, start_date: str, end_date: str) -> FetchResult:
    """获取指定日期范围内的全部帖子，并保留分页数量审计。"""
    headers = {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": GROUP_URL,
        "Origin": "https://wx.zsxq.com",
    }

    all_topics = []
    seen_topic_ids = set()
    end_time = None
    api_item_count = 0
    page_count = 0
    outside_date_count = 0
    duplicate_count = 0
    previous_topic_dt = None
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=CST)
    end_exclusive_dt = (
        datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    ).replace(tzinfo=CST)

    while True:
        url = f"{API_BASE}/{GROUP_ID}/topics?scope=all&count=20"
        if end_time:
            url += f"&end_time={urllib.parse.quote(end_time, safe='')}"

        req = urllib.request.Request(url, headers=headers)
        # 翻页重试：API 偶尔返回内部错误，最多重试 3 次
        data = None
        last_error_summary = "未知错误"
        for retry in range(3):
            try:
                with API_OPENER.open(req, timeout=30) as resp:
                    response_data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(response_data, dict):
                    raise ValueError("API 响应顶层必须是 JSON 对象")
                succeeded = response_data.get("succeeded")
                if not isinstance(succeeded, bool):
                    raise ValueError("API succeeded 必须是布尔值")
                if succeeded:
                    response_body = response_data.get("resp_data")
                    if not isinstance(response_body, dict):
                        raise ValueError("API 成功响应缺少对象类型的 resp_data")
                    response_topics = response_body.get("topics")
                    if not isinstance(response_topics, list):
                        raise ValueError("API 成功响应缺少数组类型的 topics")
                    if any(not isinstance(topic, dict) for topic in response_topics):
                        raise ValueError("API topics 数组包含非对象条目")
                    for topic in response_topics:
                        validate_topic_structure(topic)
                    is_end_value = response_body.get("is_end")
                    if is_end_value is not None and not isinstance(is_end_value, bool):
                        raise ValueError("API is_end 必须是布尔值或空值")
                    data = response_data
                    break
                err_msg = response_data.get("error", "未知错误")
                last_error_summary = str(err_msg)
                if "1059" in str(response_data.get("error_code", "")):
                    print(f"  API 限流，等待 10 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                    import time; time.sleep(10)
                    continue
                print(f"API 请求失败: {err_msg}，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)
            except urllib.error.HTTPError as e:
                last_error_summary = f"HTTP {e.code}"
                if e.code in (401, 403):
                    raise RuntimeError(
                        f"认证失败（HTTP {e.code}），请更新凭据文件中的 ZSXQ_ACCESS_TOKEN"
                    ) from e
                if 300 <= e.code < 400:
                    raise RuntimeError(
                        f"API 返回 HTTP {e.code} 重定向；为保护 access token 已拒绝跟随"
                    ) from e
                print(f"  HTTP {e.code} 错误，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)
            except Exception as e:
                last_error_summary = str(e)
                print(f"  请求异常: {e}，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)

        if data is None:
            raise RuntimeError(
                f"API 连续重试 3 次仍失败（最后错误：{last_error_summary}）；"
                f"已暂存 {len(all_topics)} 条帖子，取消导出以避免生成不完整文件"
            )

        page_count += 1
        topics = data["resp_data"].get("topics", [])
        api_item_count += len(topics)
        is_end = data["resp_data"].get("is_end")
        if not topics:
            if is_end is True:
                break
            raise RuntimeError("API 返回非终止空页（is_end 不为 true），取消导出以避免遗漏")

        should_stop = False
        for t in topics:
            ct = t.get("create_time", "")
            try:
                topic_dt = datetime.fromisoformat(ct)
                if topic_dt.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
                topic_dt = topic_dt.astimezone(CST)
            except (TypeError, ValueError) as exc:
                topic_id = t.get("topic_id", "未知")
                raise RuntimeError(
                    f"帖子 {topic_id} 的 create_time 无效，取消导出以避免遗漏"
                ) from exc

            if previous_topic_dt is not None and topic_dt > previous_topic_dt:
                raise RuntimeError(
                    "API 帖子时间不是降序，"
                    f"当前 {topic_dt.isoformat()} 晚于前一条 {previous_topic_dt.isoformat()}，"
                    "取消导出以避免提前结束分页"
                )
            previous_topic_dt = topic_dt

            topic_id = t.get("topic_id")
            if topic_id is None:
                raise RuntimeError("API 返回缺少 topic_id 的帖子，取消导出以避免无法去重")
            if topic_id in seen_topic_ids:
                duplicate_count += 1
                continue
            seen_topic_ids.add(topic_id)

            if topic_dt >= end_exclusive_dt:
                outside_date_count += 1
                continue
            if topic_dt < start_dt:
                outside_date_count += 1
                should_stop = True
                continue

            all_topics.append(t)

        if should_stop:
            break

        if is_end is True:
            break

        last = topics[-1]
        next_end_time = last.get("create_time", "")
        if not next_end_time:
            raise RuntimeError("API 未结束但末条帖子缺少 create_time，无法安全继续翻页")
        if end_time:
            try:
                if datetime.fromisoformat(next_end_time) >= datetime.fromisoformat(end_time):
                    raise RuntimeError(
                        f"分页游标未向更早时间推进：{end_time} -> {next_end_time}"
                    )
            except ValueError as exc:
                raise RuntimeError(f"API 返回无法解析的分页时间：{next_end_time}") from exc
        end_time = next_end_time

        import time
        time.sleep(2)

    audited_count = len(all_topics) + outside_date_count + duplicate_count
    if api_item_count != audited_count:
        raise RuntimeError(
            "抓取数量不守恒："
            f"API 分页返回 {api_item_count} != 日期范围内 {len(all_topics)} "
            f"+ 日期范围外 {outside_date_count} + 分页重复 {duplicate_count}"
        )

    return FetchResult(
        topics=all_topics,
        api_item_count=api_item_count,
        page_count=page_count,
        outside_date_count=outside_date_count,
        duplicate_count=duplicate_count,
    )


def build_daily_counts(
    topics: list[dict], start_date: str, end_date: str
) -> dict[str, int]:
    """按北京时间统计闭区间内每天的帖子数，包括零帖日期。"""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    counts = {
        (start + timedelta(days=offset)).isoformat(): 0
        for offset in range((end - start).days + 1)
    }
    for topic in topics:
        created = datetime.fromisoformat(topic["create_time"])
        day = created.astimezone(CST).date().isoformat()
        if day not in counts:
            raise RuntimeError(f"帖子日期 {day} 超出请求范围，取消导出")
        counts[day] += 1
    counted = sum(counts.values())
    if counted != len(topics):
        raise RuntimeError(
            f"逐日数量不守恒：逐日合计 {counted} != 日期范围内 {len(topics)}"
        )
    return counts



def extract_comments(topic: dict) -> list[dict[str, str]]:
    """提取评论，并保留作者字段供星主回复高亮。"""
    comments = []
    show_comments = topic.get("show_comments", [])
    for c in show_comments:
        owner = c.get("owner", {}).get("name", "")
        text = decode_e_tags(c.get("text", ""))
        if text:
            comments.append({"owner": owner, "text": text})
    return comments



def format_topic(topic: dict) -> dict | None:
    """将单个 topic 格式化为输出格式"""
    ttype = topic.get("type", "")
    talk = topic.get("talk", {})
    question = topic.get("question", {})
    answer = topic.get("answer", {})

    # 提取文本
    raw_text = talk.get("text", "") if talk else ""
    q_raw = question.get("text", "") if question else ""
    a_raw = answer.get("text", "") if answer else ""

    # 解码标签
    text = decode_e_tags(raw_text)
    q_text = decode_e_tags(q_raw)
    a_text = decode_e_tags(a_raw)

    # 合并全文用于评分提取
    full_raw = raw_text + " " + a_raw
    score = extract_score(decode_e_tags(full_raw))

    # 提取标签和附件
    tags = extract_tags(raw_text) + extract_tags(q_raw) + extract_tags(a_raw)
    files = extract_files(topic)

    # 提取日期和作者；统一转成北京时间，供后续过滤和展示使用
    created = topic.get("create_time", "")
    if created:
        created = datetime.fromisoformat(created).astimezone(CST).isoformat()
    if ttype == "q&a":
        owner = question.get("owner", {}).get("name", "") if question else ""
        answer_owner = answer.get("owner", {}).get("name", "") if answer else ""
    else:
        owner = talk.get("owner", {}).get("name", "") if talk else ""
        answer_owner = ""

    # 构建完整帖子纯文本
    if ttype == "q&a":
        full_text = ""
        if q_text:
            full_text += f"{owner} 提问：\n\n{q_text}\n\n"
        if a_text:
            full_text += f"{answer_owner} 回答：\n\n{a_text}"
    else:
        full_text = text

    # 提取标题；提问帖只基于问题正文，避免把“某某提问”署名当作标题
    title_source = q_text if ttype == "q&a" else full_text
    title = extract_post_title(title_source, ttype)

    # 提取评论
    comments = extract_comments(topic)

    return {
        "topic_id": topic["topic_id"],
        "type": ttype,
        "created": created,
        "owner": owner,
        "answer_owner": answer_owner,
        "question_text": q_text,
        "answer_text": a_text,
        "score": score,
        "title": title,
        "text": full_text,
        "tags": tags,
        "files": files,
        "comments": comments,
        "images": extract_images(topic),
    }


def has_focus_answer(post: dict) -> bool:
    """仅在回答正文存在时把重点作者计为有效回答。"""
    return bool(post["answer_text"]) and is_focus_author(post["answer_owner"])


def build_focus_counts(posts: list[dict], qa_posts: list[dict]) -> dict[str, int]:
    """统计重点星主在结构化作者字段中的发言、回答、评论和关联卡片。"""
    all_posts = posts + qa_posts
    authored = sum(is_focus_author(post["owner"]) for post in all_posts)
    answers = sum(has_focus_answer(post) for post in qa_posts)
    comments = sum(
        is_focus_author(comment["owner"])
        for post in all_posts
        for comment in post["comments"]
    )
    cards = sum(
        is_focus_author(post["owner"]) or has_focus_answer(post)
        for post in all_posts
    )
    return {
        "cards": cards,
        "authored": authored,
        "answers": answers,
        "comments": comments,
    }


def generate_html(
    posts: list[dict],
    qa_posts: list[dict],
    start_date: str,
    end_date: str,
    fetch_result: FetchResult,
    daily_counts: dict[str, int],
    filtered_count: int,
    no_filter: bool,
) -> str:
    """生成自包含 raw-only HTML，用户提问单独归入专属分区。"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    if no_filter:
        count_text = (
            f"输出帖子：帖子与发言 {len(posts)} 篇 | 用户提问 {len(qa_posts)} 篇"
            f" | 日期范围内 {len(fetch_result.topics)} 篇"
        )
    else:
        count_text = (
            f"输出帖子：帖子与发言 {len(posts)} 篇 | 用户提问 {len(qa_posts)} 篇"
            f" | 评分过滤 {filtered_count} 篇 | 日期范围内 {len(fetch_result.topics)} 篇"
        )
    audit_text = (
        f"抓取审计：API 分页返回 {fetch_result.api_item_count} 条"
        f" | 日期范围外 {fetch_result.outside_date_count} 条"
        f" | 分页重复 {fetch_result.duplicate_count} 条"
        f" | 成功请求 {fetch_result.page_count} 页"
    )
    daily_text = "逐日数量：" + " | ".join(
        f"{day} {count} 篇" for day, count in daily_counts.items()
    )
    focus_counts = build_focus_counts(posts, qa_posts)
    focus_text = (
        f"重点星主：关联卡片 {focus_counts['cards']} 篇"
        f" | 发言/提问 {focus_counts['authored']} 条"
        f" | 回答 {focus_counts['answers']} 条"
        f" | 评论 {focus_counts['comments']} 条"
    )
    image_count = sum(len(post.get("images", [])) for post in posts + qa_posts)
    image_text = f"附图 OCR：{image_count} 张（图片内嵌，识别结果逐图展示）"

    def escape(value: object) -> str:
        return html.escape(str(value), quote=True)

    def render_list(label: str, values: list[str], css_class: str) -> str:
        if not values:
            return ""
        items = "".join(f"<li>{escape(value)}</li>" for value in values)
        return (
            f'<div class="post-extra {css_class}">'
            f'<h4>{escape(label)}</h4><ul>{items}</ul></div>'
        )

    def render_images(images: list[dict[str, str]]) -> str:
        if not images:
            return ""
        items = []
        for index, image in enumerate(images, 1):
            recognized = image["ocr"] or "未识别出文字；请查看原图。"
            items.append(
                f'<figure><img src="{escape(image["data_uri"])}" alt="附件图片 {index}" loading="lazy">'
                f'<figcaption>图片 {index} · {escape(image["section"])} · OCR 原文</figcaption>'
                f'<pre class="raw-content ocr-content">{escape(recognized)}</pre></figure>'
            )
        return '<div class="post-extra post-images"><h4>附图与文字识别</h4>' + "".join(items) + '</div>'

    def render_identity(name: str, css_class: str = "owner") -> str:
        if not name:
            return ""
        focused = is_focus_author(name)
        classes = f'{css_class} focus-person' if focused else css_class
        badge = '<span class="star-badge">星主</span>' if focused else ""
        return f'<span class="{classes}">{escape(name)}{badge}</span>'

    def render_comments(comments: list[dict[str, str]]) -> str:
        if not comments:
            return ""
        items = []
        for comment in comments:
            comment_owner = comment["owner"]
            focused = is_focus_author(comment_owner)
            item_class = ' class="star-reply"' if focused else ""
            identity = render_identity(comment_owner, "comment-owner")
            separator = "：" if comment_owner else ""
            items.append(
                f'<li{item_class}>{identity}{escape(separator + comment["text"])}</li>'
            )
        return (
            '<div class="post-extra comments"><h4>评论预览</h4><ul>'
            + "".join(items)
            + "</ul></div>"
        )

    def render_qna_content(post: dict) -> str:
        parts = []
        if post["question_text"]:
            question_identity = render_identity(post["owner"], "qna-owner")
            parts.append(
                '<div class="qna-part question-part">'
                f'<h4>提问 {question_identity}</h4>'
                f'<pre class="raw-content">{escape(post["question_text"])}</pre></div>'
            )
        if post["answer_text"]:
            focused = is_focus_author(post["answer_owner"])
            answer_class = "qna-part answer-part star-reply" if focused else "qna-part answer-part"
            answer_identity = render_identity(post["answer_owner"], "qna-owner")
            parts.append(
                f'<div class="{answer_class}">'
                f'<h4>回答 {answer_identity}</h4>'
                f'<pre class="raw-content">{escape(post["answer_text"])}</pre></div>'
            )
        return '<div class="qna-content">' + "".join(parts) + "</div>"

    def render_post(post: dict, question: bool = False) -> str:
        score_text = f"{post['score']} 分" if post["score"] is not None else "无评分"
        created = post["created"] or ""
        if created:
            time_text = datetime.fromisoformat(created).strftime("%m-%d %H:%M")
        else:
            time_text = "未知时间"
        owner_focused = is_focus_author(post["owner"])
        answer_focused = has_focus_answer(post)
        card_focused = owner_focused
        if question:
            post_kind = "星主提问" if owner_focused else "用户提问"
        else:
            post_kind = "星主发言" if owner_focused else "帖子"
        owner = render_identity(post["owner"])
        tags = render_list("标签", post["tags"], "tags")
        files = render_list("附件", post["files"], "files")
        comments = render_comments(post["comments"])
        card_class = "post-card star-highlight" if card_focused else "post-card"
        focus_attr = ' data-focus-star="true"' if card_focused else ""
        content = (
            render_qna_content(post)
            if question
            else f'<pre class="raw-content">{escape(post["text"])}</pre>'
        )
        return (
            f'<article class="{card_class}" data-post-id="{escape(post["topic_id"])}" '
            f'data-timestamp="{escape(created)}"{focus_attr}>'
            '<header class="post-meta">'
            f'<span class="kind">{post_kind}</span>'
            f'<time datetime="{escape(created)}">{escape(time_text)}</time>'
            f'<span class="score">{escape(score_text)}</span>{owner}'
            '</header>'
            f'<h3>{escape(post["title"])}</h3>'
            f'{content}{render_images(post.get("images", []))}{tags}{files}{comments}'
            '</article>'
        )

    host_cards = "".join(render_post(post) for post in posts)
    question_cards = "".join(render_post(post, question=True) for post in qa_posts)
    if not host_cards:
        host_cards = '<p class="empty">该日期范围内没有帖子或发言。</p>'
    if not question_cards:
        question_cards = '<p class="empty">该日期范围内没有用户提问。</p>'

    document_start = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多鱼朋友圈 · 原始数据</title>
<style>
:root { color-scheme: light dark; --bg:#f4f1ea; --panel:#fffdf8; --text:#25221d; --muted:#736d63; --line:#d9d1c3; --accent:#b4532a; --accent-soft:#f4dfd2; --star-bg:#fff4cc; --star-line:#d99b16; --star-text:#704500; }
@media (prefers-color-scheme: dark) { :root { --bg:#171613; --panel:#22201c; --text:#f4efe7; --muted:#b8afa3; --line:#454038; --accent:#f0a47d; --accent-soft:#492b20; --star-bg:#3b3018; --star-line:#d8a33a; --star-text:#ffd67a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.7; }
a { color:var(--accent); }
.page { width:min(1080px, calc(100% - 32px)); margin:32px auto 64px; }
.hero, .section { background:var(--panel); border:1px solid var(--line); border-radius:18px; box-shadow:0 12px 32px rgba(0,0,0,.06); }
.hero { padding:32px; }
.eyebrow { color:var(--accent); font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
h1 { margin:.2rem 0 .5rem; font-size:clamp(2rem,5vw,3.6rem); line-height:1.15; }
.subtitle, .audit-row, .empty { color:var(--muted); }
.audit { display:grid; gap:8px; margin-top:22px; padding:18px; background:var(--accent-soft); border-radius:14px; }
.audit-row { margin:0; overflow-wrap:anywhere; }
.nav { display:flex; flex-wrap:wrap; gap:10px; margin-top:20px; }
.nav a { padding:8px 12px; border:1px solid var(--line); border-radius:999px; text-decoration:none; }
.section { margin-top:24px; padding:28px; }
.section-heading { display:flex; justify-content:space-between; gap:16px; align-items:baseline; border-bottom:1px solid var(--line); margin-bottom:20px; }
.section-heading h2 { margin:0 0 12px; }
.section-heading span { color:var(--muted); }
.post-card { padding:22px 0; border-bottom:1px solid var(--line); }
.post-card:last-child { border-bottom:0; }
.post-card.star-highlight { margin:12px 0; padding:22px; border:2px solid var(--star-line); border-radius:14px; background:var(--star-bg); box-shadow:0 8px 20px rgba(159,105,0,.10); }
.post-meta { display:flex; flex-wrap:wrap; gap:8px 12px; color:var(--muted); font-size:.92rem; }
.kind, .score { color:var(--accent); font-weight:700; }
.focus-person { color:var(--star-text); font-weight:800; }
.star-badge { display:inline-block; margin-left:6px; padding:1px 7px; border:1px solid var(--star-line); border-radius:999px; background:var(--star-bg); color:var(--star-text); font-size:.78rem; line-height:1.5; vertical-align:middle; }
.focus-note { margin:18px 0 0; padding:12px 14px; border-left:4px solid var(--star-line); border-radius:8px; background:var(--star-bg); color:var(--star-text); }
.post-card h3 { margin:10px 0 14px; font-size:1.25rem; }
.raw-content { margin:0; padding:16px; background:color-mix(in srgb, var(--bg) 72%, transparent); border-radius:12px; white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; }
.qna-content { display:grid; gap:12px; }
.qna-part { padding:14px; border:1px solid var(--line); border-radius:12px; }
.qna-part h4 { margin:0 0 8px; }
.qna-part.star-reply, .comments li.star-reply { border-color:var(--star-line); background:var(--star-bg); }
.comments li.star-reply { margin:6px 0; padding:8px 10px; border-left:4px solid var(--star-line); border-radius:6px; list-style-position:inside; }
.post-extra { margin-top:16px; }
.post-extra h4 { margin:0 0 6px; color:var(--muted); font-size:.92rem; }
.post-extra ul { margin:0; padding-left:1.4rem; }
.post-images figure { margin:12px 0; }
.post-images img { display:block; max-width:100%; height:auto; border-radius:10px; }
.post-images figcaption { color:var(--muted); margin:6px 0; }
footer { margin-top:24px; color:var(--muted); text-align:center; }
@media (max-width:640px) { .page { width:min(100% - 20px,1080px); margin-top:10px; } .hero,.section { border-radius:13px; padding:20px; } .section-heading { display:block; } }
</style>
</head>
<body>
<div class="page">
"""
    body = (
        '<header class="hero">'
        '<div class="eyebrow">ZSXQ RAW EXPORT</div>'
        f'<h1>{escape(GROUP_NAME)}</h1>'
        '<p class="subtitle">自包含 HTML · 只做抓取与排版，不生成摘要</p>'
        f'<p>来源：<a href="{escape(GROUP_URL)}">{escape(GROUP_URL)}</a></p>'
        f'<p>导出时间：{escape(now)} · 日期范围：{escape(start_date)} ～ {escape(end_date)}</p>'
        '<p class="focus-note"><span class="star-badge">星主</span> 重点关注：求学、大牛；他们的发言、回答和评论会以金色高亮。</p>'
        '<div class="audit">'
        f'<p class="audit-row">{escape(count_text)}</p>'
        f'<p class="audit-row">{escape(audit_text)}</p>'
        f'<p class="audit-row">{escape(daily_text)}</p>'
        f'<p class="audit-row">{escape(focus_text)}</p>'
        f'<p class="audit-row">{escape(image_text)}</p>'
        '</div>'
        '<nav class="nav" aria-label="页面分区">'
        f'<a href="#host-posts">帖子与发言 {len(posts)}</a>'
        f'<a href="#questions">用户提问 {len(qa_posts)}</a>'
        '</nav></header>'
        '<main>'
        '<section id="host-posts" class="section">'
        f'<div class="section-heading"><h2>帖子与发言</h2><span>{len(posts)} 篇</span></div>'
        f'{host_cards}</section>'
        '<section id="questions" class="section">'
        f'<div class="section-heading"><h2>用户提问</h2><span>{len(qa_posts)} 篇</span></div>'
        f'{question_cards}</section>'
        '</main>'
        f'<footer>生成于 {escape(now)} · 页面不依赖外部脚本或样式</footer>'
    )
    return document_start + body + "</div>\n</body>\n</html>\n"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="知识星球帖子导出")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="token 所在的 .env 文件路径")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=3, help="最近N天（默认3天）")
    filter_group = parser.add_mutually_exclusive_group()
    filter_group.add_argument("--no-filter", action="store_true", help="不按能量评分过滤，保留所有帖子")
    filter_group.add_argument("--min-score", type=float, default=None, help="最低能量评分过滤阈值（默认不过滤）")
    args = parser.parse_args()
    if args.min_score is not None and not math.isfinite(args.min_score):
        parser.error("--min-score 必须是有限数值")

    # token 只允许从 .env 读取，避免通过命令行参数泄露到 shell 历史或进程列表
    try:
        token = read_token_from_env(args.env_file)
    except RuntimeError as exc:
        parser.error(str(exc))
    if token:
        print(f"已从 {args.env_file} 读取 token")
    else:
        parser.error(
            "无法从 .env 文件读取 token。\n"
            f"请确认 {args.env_file} 存在并包含 ZSXQ_ACCESS_TOKEN，"
            "或仅包含一行纯 token。"
        )

    # 计算并校验日期范围
    now = datetime.now(CST)
    if bool(args.start) != bool(args.end):
        parser.error("--start 和 --end 必须同时提供")
    if args.start and args.end:
        try:
            parsed_start = datetime.strptime(args.start, "%Y-%m-%d")
            parsed_end = datetime.strptime(args.end, "%Y-%m-%d")
        except ValueError:
            parser.error("--start 和 --end 必须使用 YYYY-MM-DD 格式")
        if parsed_start > parsed_end:
            parser.error("--start 不能晚于 --end")
        start_date = parsed_start.strftime("%Y-%m-%d")
        end_date = parsed_end.strftime("%Y-%m-%d")
    else:
        if args.days < 1:
            parser.error("--days 必须大于等于 1")
        end_dt = now
        start_dt = end_dt - timedelta(days=args.days - 1)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")

    # 确定过滤模式
    no_filter = args.no_filter or (args.min_score is None)
    min_score = args.min_score if args.min_score is not None else 0

    print(f"正在获取 {start_date} ~ {end_date} 的帖子...")
    if no_filter:
        print("模式：不过滤评分，保留所有帖子")
    else:
        print(f"模式：过滤评分 >= {min_score}")

    # 获取帖子；失败时不生成可能不完整的输出文件
    try:
        fetch_result = fetch_topics(token, start_date, end_date)
        topics = fetch_result.topics
        daily_counts = build_daily_counts(topics, start_date, end_date)
    except RuntimeError as exc:
        parser.exit(1, f"导出失败：{exc}\n")
    print(
        f"API 分页返回 {fetch_result.api_item_count} 条；"
        f"日期范围内去重后保留 {len(topics)} 条；"
        f"范围外 {fetch_result.outside_date_count} 条；"
        f"分页重复 {fetch_result.duplicate_count} 条；"
        f"成功请求 {fetch_result.page_count} 页"
    )
    print("逐日数量：" + " | ".join(
        f"{day} {count} 篇" for day, count in daily_counts.items()
    ))

    # 格式化：非 q&a 帖子与用户提问分别保存
    all_posts = []
    qa_posts = []
    filtered_count = 0
    for topic in topics:
        post = format_topic(topic)
        if post is None:
            continue

        # 日期过滤
        if post["created"]:
            post_date = post["created"][:10]
            if post_date < start_date or post_date > end_date:
                parser.exit(1, f"导出失败：帖子时间 {post['created']} 超出请求范围\n")

        # 用户提问全部保留到独立章节，不参与能量评分过滤
        if post["type"] == "q&a":
            qa_posts.append(post)
            continue

        # 能量评分过滤仅作用于非 q&a 帖子，且只在指定 --min-score 时生效
        if not no_filter:
            if post["score"] is None or post["score"] < min_score:
                filtered_count += 1
                continue

        all_posts.append(post)

    # 非 q&a 帖子和用户提问均按北京时间降序；评分只展示，不参与排序
    all_posts.sort(key=lambda post: post["created"], reverse=True)
    qa_posts.sort(key=lambda post: post["created"], reverse=True)

    output_count = len(all_posts) + len(qa_posts)
    expected_output_count = len(topics) - filtered_count
    if output_count != expected_output_count:
        parser.exit(
            1,
            "导出失败：数量不守恒，"
            f"日期范围内 {len(topics)} - 评分过滤 {filtered_count} "
            f"!= 输出 {output_count}\n",
        )

    print(
        f"保留 {len(all_posts)} 篇帖子与发言"
        + (f"（评分过滤 {filtered_count} 篇）" if filtered_count > 0 else "")
    )
    print(f"保留用户提问（q&a）帖子：{len(qa_posts)} 篇，写入独立章节")
    try:
        recognize_images(all_posts + qa_posts)
    except RuntimeError as exc:
        parser.exit(1, f"导出失败：{exc}\n")

    # 生成自包含 HTML
    html_document = generate_html(
        all_posts,
        qa_posts,
        start_date,
        end_date,
        fetch_result,
        daily_counts,
        filtered_count,
        no_filter,
    )

    # 写入文件
    mmdd = now.strftime("%m%d")
    output_path = OUTPUT_DIR / f"{mmdd}-{GROUP_NAME}_raw_{start_date}_{end_date}.html"
    temp_path = None
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUTPUT_DIR,
            prefix=output_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(html_document)
        os.replace(temp_path, output_path)
        temp_path = None
    except (OSError, UnicodeError) as exc:
        cleanup_error = None
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_error = cleanup_exc
        message = f"导出失败：写入输出文件失败：{exc}"
        if cleanup_error is not None:
            message += f"；临时文件清理失败：{cleanup_error}"
        parser.exit(1, message + "\n")

    print(f"\n导出完成！")
    print(f"文件路径：{output_path}")
    print(f"帖子与发言数量：{len(all_posts)}")
    print(f"用户提问数量：{len(qa_posts)}（已写入独立章节）")
    focus_counts = build_focus_counts(all_posts, qa_posts)
    print(
        "重点星主标注："
        f"关联卡片 {focus_counts['cards']} 篇；"
        f"发言/提问 {focus_counts['authored']} 条；"
        f"回答 {focus_counts['answers']} 条；"
        f"评论 {focus_counts['comments']} 条"
    )
    print(f"日期范围内总数：{len(topics)}")
    print("逐日数量：" + " | ".join(
        f"{day} {count} 篇" for day, count in daily_counts.items()
    ))
    if not no_filter:
        print(f"评分过滤：{filtered_count} 篇非 q&a 帖子（评分<{min_score}或无评分）")
    print(f"日期范围：{start_date} ~ {end_date}")


if __name__ == "__main__":
    main()
