#!/usr/bin/env python3
"""知识星球 API 导出脚本 - 使用 zsxq API 直接获取完整帖子，无需浏览器展开"""

import json
import re
import sys
import urllib.parse
from html.parser import HTMLParser
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __common.fields import extract_files, extract_post_title, extract_score, extract_tags

GROUP_ID = "15522441811252"
API_BASE = "https://api.zsxq.com/v2/groups"
OUTPUT_DIR = Path("/Users/melo/doc-Dgf/zsxq_md")
CST = timezone(timedelta(hours=8))
# 重点关注作者：其发言、回答与评论在输出中单独标记
FOCUS_OWNER = "三线文案大锅饭"
COMMENT_SLEEP = 2  # 每帖评论请求间隔（秒），避免触发限流

# Vision OCR 工具路径
SCRIPT_DIR = Path(__file__).resolve().parent
VISION_OCR_BIN = SCRIPT_DIR / "vision_ocr"
IMAGE_DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://wx.zsxq.com/",
}


# 默认 .env 路径（存放 zsxq_access_token）
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"


def read_token_from_env(env_path: Path) -> str | None:
    """从 .env 文件读取 zsxq_access_token，支持 KEY=VALUE 和纯 token 两种格式"""
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # KEY=VALUE 格式
        if "=" in line:
            key, _, val = line.partition("=")
            if key.strip().upper() in ("ZSXQ_ACCESS_TOKEN", "TOKEN", "ACCESS_TOKEN"):
                return val.strip().strip("\"'\"")
        else:
            # 纯 token（UUID_UUID 格式）
            if re.match(r"^[0-9A-Fa-f-]+_[0-9A-Fa-f]+$", line):
                return line
    return None



def decode_e_tags(s: str) -> str:
    """解码知识星球自定义 <e> 标签为 Markdown"""
    if not s:
        return ""
    # Bold: <e type="text_bold" title="xxx" />
    s = re.sub(
        r'<e type="text_bold" title="([^"]*?)"\s*/>',
        lambda m: "**" + urllib.parse.unquote(m.group(1)) + "**",
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


def fetch_topics(token: str, start_date: str, end_date: str) -> list[dict]:
    """获取指定日期范围内的所有帖子"""
    headers = {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"https://wx.zsxq.com/group/{GROUP_ID}",
        "Origin": "https://wx.zsxq.com",
    }

    all_topics = []
    seen_topic_ids: set[str] = set()
    end_time = None
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=CST)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=CST
    )

    while True:
        url = f"{API_BASE}/{GROUP_ID}/topics?scope=all&count=20"
        if end_time:
            url += f"&end_time={urllib.parse.quote(end_time, safe='')}"

        req = urllib.request.Request(url, headers=headers)
        # 翻页重试：API 偶尔返回内部错误，最多重试 3 次
        data = None
        for retry in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("succeeded"):
                    break
                # API 返回业务错误
                err_msg = data.get("error", "未知错误")
                if "1059" in str(data.get("error_code", "")):
                    print(f"  API 限流，等待 10 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                    import time; time.sleep(10)
                    continue
                print(f"API 请求失败: {err_msg}，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)
            except urllib.error.HTTPError as e:
                print(f"  HTTP {e.code} 错误，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)
            except Exception as e:
                print(f"  请求异常: {e}，等待 5 秒后重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)

        if data is None or not data.get("succeeded"):
            raise RuntimeError(f"翻页重试 3 次仍失败，已获取 {len(all_topics)} 条帖子；取消导出")

        topics = data["resp_data"].get("topics", [])
        if not topics:
            break

        should_stop = False
        for t in topics:
            ct = t.get("create_time", "")
            try:
                topic_dt = datetime.fromisoformat(ct)
            except ValueError:
                continue

            if topic_dt > end_dt:
                continue
            if topic_dt < start_dt:
                should_stop = True
                break

            topic_id = str(t.get("topic_id", "")).strip()
            if not topic_id:
                raise RuntimeError("帖子缺少 topic_id，取消导出")
            if topic_id in seen_topic_ids:
                continue
            seen_topic_ids.add(topic_id)
            all_topics.append(t)

        if should_stop or not topics:
            break

        is_end = data["resp_data"].get("is_end")
        if is_end is True:
            break

        last = topics[-1]
        end_time = last.get("create_time", "")
        if not end_time:
            break

        import time
        time.sleep(2)

    return all_topics



def extract_images(topic: dict) -> list[dict]:
    """提取帖子中的图片信息，返回 [{url, width, height}, ...]"""
    images = []
    talk = topic.get("talk", {})
    for img in talk.get("images", []):
        original = img.get("original", {})
        if not original.get("url"):
            large = img.get("large", {})
            if not large.get("url"):
                thumb = img.get("thumbnail", {})
                url = thumb.get("url", "")
            else:
                url = large.get("url", "")
        else:
            url = original.get("url", "")
        if url:
            images.append({
                "url": url,
                "width": original.get("width", 0),
                "height": original.get("height", 0),
            })
    # q&a 帖子中 question 或 answer 也可能有图片
    question = topic.get("question", {})
    answer = topic.get("answer", {})
    for src in [question, answer]:
        for img in src.get("images", []):
            original = img.get("original", {})
            url = original.get("url") or img.get("large", {}).get("url") or img.get("thumbnail", {}).get("url", "")
            if url:
                images.append({
                    "url": url,
                    "width": original.get("width", 0),
                    "height": original.get("height", 0),
                })
    return images


def download_image(url: str, dest: Path) -> bool:
    """下载图片到本地，返回是否成功"""
    try:
        req = urllib.request.Request(url, headers=IMAGE_DL_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  下载图片失败: {e}", file=sys.stderr)
        return False


def run_vision_ocr(image_path: Path) -> list[str]:
    """运行 Vision OCR，返回文本行列表"""
    import subprocess
    try:
        result = subprocess.run(
            [str(VISION_OCR_BIN), str(image_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  OCR 失败 (exit {result.returncode}): {result.stderr[:200]}", file=sys.stderr)
            return []
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        return lines
    except subprocess.TimeoutExpired:
        print(f"  OCR 超时", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  OCR 异常: {e}", file=sys.stderr)
        return []


def render_ocr_layout(ocr_lines: list[str], grid_width: int = 120) -> str:
    """
    将 Vision OCR 输出渲染为空间定位的等宽文本布局。
    OCR 输出格式: x\ty\ttext (坐标为归一化值 0~1，原点左下)
    策略：将 x 坐标映射到字符列，按 y 聚类成行后用空格定位，
    保留原始视觉布局。表格类自然对齐成表格，截图类保留空间关系。
    """
    if not ocr_lines:
        return ""

    entries = []
    for line in ocr_lines:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            x = float(parts[0])
            y = float(parts[1])
            text = parts[2]
            entries.append((x, y, text))
        except ValueError:
            continue

    if not entries:
        return ""

    # 按 y 降序排列（y 大 = 图片上方 = 第一行）
    entries.sort(key=lambda e: -e[1])

    # 聚类成行：y 差值 < 0.02 视为同一行
    rows = []
    current_row = [entries[0]]
    current_y = entries[0][1]
    for i in range(1, len(entries)):
        if abs(entries[i][1] - current_y) < 0.02:
            current_row.append(entries[i])
        else:
            rows.append(current_row)
            current_row = [entries[i]]
            current_y = entries[i][1]
    rows.append(current_row)

    # 渲染每行：用空格在字符网格上定位文本
    output_lines = []
    for row in rows:
        row.sort(key=lambda e: e[0])
        buf = [" "] * grid_width
        for x, _y, text in row:
            col = int(x * grid_width)
            for i, ch in enumerate(text):
                pos = col + i
                if 0 <= pos < grid_width:
                    # 避免覆盖已有字符（非空格）
                    if buf[pos] == " ":
                        buf[pos] = ch
                    else:
                        # 冲突时用 '·' 分隔或直接拼接
                        buf[pos] = ch
        line = "".join(buf).rstrip()
        if line.strip():
            output_lines.append(line)

    # 去除尾部空行
    while output_lines and not output_lines[-1].strip():
        output_lines.pop()

    return "\n".join(output_lines) if output_lines else ""


def ocr_topic_images(topic: dict) -> str:
    """
    对帖子中的所有图片执行 OCR，返回 Markdown 格式的 OCR 结果。
    如果没有图片或 OCR 无结果，返回空字符串。
    """
    images = extract_images(topic)
    if not images:
        return ""

    if not VISION_OCR_BIN.exists():
        print(f"  vision_ocr 工具不存在: {VISION_OCR_BIN}", file=sys.stderr)
        return ""

    import tempfile
    ocr_parts = []
    for i, img_info in enumerate(images):
        url = img_info["url"]
        ext = ".png"
        if ".jpg" in url or "format/jpg" in url:
            ext = ".jpg"
        tmp_path = Path(tempfile.mktemp(suffix=ext))
        if not download_image(url, tmp_path):
            continue
        ocr_lines = run_vision_ocr(tmp_path)
        tmp_path.unlink(missing_ok=True)
        if not ocr_lines:
            continue
        layout = render_ocr_layout(ocr_lines)
        if layout:
            ocr_parts.append(f"**图片OCR（图{i+1}）**：\n\n```\n{layout}\n```")
    return "\n\n".join(ocr_parts)


def extract_comments(topic: dict) -> list[str]:
    """提取时间线接口自带的评论预览（作为完整评论接口失败时的回退）"""
    comments = []
    show_comments = topic.get("show_comments", [])
    for c in show_comments:
        owner = c.get("owner", {}).get("name", "")
        text = decode_e_tags(c.get("text", ""))
        if text:
            comments.append(f"{owner}：{text}")
    return comments


def fetch_comments(token: str, topic_id: str) -> list[dict]:
    """抓取评论接口所有可访问的评论；失败时中止导出，避免不完整数据。"""
    headers = {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"https://wx.zsxq.com/group/{GROUP_ID}",
        "Origin": "https://wx.zsxq.com",
    }

    all_comments: list[dict] = []
    seen_ids: set = set()
    end_time = None
    previous_cursor = None

    while True:
        url = f"{API_BASE.rsplit('/groups', 1)[0]}/topics/{topic_id}/comments?count=20&sort=desc"
        if end_time:
            url += f"&end_time={urllib.parse.quote(end_time, safe='')}"

        req = urllib.request.Request(url, headers=headers)
        data = None
        for retry in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("succeeded"):
                    break
                print(f"  评论接口失败(topic {topic_id}): {data.get('error')}，重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)
            except Exception as e:
                print(f"  评论请求异常(topic {topic_id}): {e}，重试 ({retry+1}/3)...", file=sys.stderr)
                import time; time.sleep(5)

        if data is None or not data.get("succeeded"):
            raise RuntimeError(f"评论接口连续重试失败 (topic {topic_id})，未生成不完整导出")

        comments = data["resp_data"].get("comments", [])
        for c in comments:
            cid = c.get("comment_id")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            all_comments.append(c)

        if not comments or len(comments) < 20:
            break

        end_time = comments[-1].get("create_time", "")
        if not end_time or end_time == previous_cursor:
            raise RuntimeError(f"评论分页游标未推进 (topic {topic_id})")
        previous_cursor = end_time

        import time
        time.sleep(2)

    all_comments.sort(key=lambda c: c.get("create_time", ""))
    return all_comments


def render_comments(comments: list[dict]) -> list[str]:
    """完整展开评论树，并以 API 的 repliee 指明回复对象。"""
    lines = []

    def walk(items: list[dict], depth: int = 0, parent: str = "") -> None:
        for c in items:
            owner = c.get("owner", {}).get("name", "")
            text = decode_e_tags(c.get("text", "") or "")
            indent = "　" * depth
            target = (c.get("repliee") or {}).get("name", "") or parent
            reply_label = f" 回复 {target}" if depth and target else ""
            if text:
                if owner == FOCUS_OWNER:
                    lines.append(f"{indent}**{FOCUS_OWNER}{reply_label}**：{text}")
                else:
                    lines.append(f"{indent}{owner}{reply_label}：{text}")
            replied = c.get("replied_comments") or []
            if replied:
                walk(replied, depth + 1, owner)

    walk(comments)
    return lines



class ArticleContent(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts = []
        self.images = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if self.depth:
            if tag not in ("img", "br", "hr", "input", "meta", "link"):
                self.depth += 1
            if tag in ("p", "br", "li", "h1", "h2", "h3"):
                self.parts.append("\n")
            if tag == "img" and (fields.get("src") or "").startswith("https://article-images.zsxq.com/"):
                url = fields["src"]
                if url not in self.images:
                    self.images.append(url)
                    self.parts.append(f"\n[长文图片 {len(self.images)}]\n")
        elif tag == "div" and "content" in (fields.get("class") or "").split() and "ql-editor" in (fields.get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            if tag in ("p", "li", "h1", "h2", "h3"):
                self.parts.append("\n")
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


def fetch_article(article: dict, token: str) -> str:
    """Include a linked ZSXQ article and OCR its image-only tables in the raw post."""
    article_id = article.get("article_id", "")
    if not re.fullmatch(r"[A-Za-z0-9]+", article_id):
        raise RuntimeError("站内长文缺少有效 article_id")
    url = f"https://articles.zsxq.com/inline_form/id_{article_id}.html"
    req = urllib.request.Request(url, headers={"Cookie": f"zsxq_access_token={token}",
                                          "User-Agent": IMAGE_DL_HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        html = response.read().decode("utf-8")
    content = ArticleContent()
    content.feed(html)
    text = "\n".join(line.strip() for line in "".join(content.parts).splitlines() if line.strip())
    if not text:
        raise RuntimeError(f"站内长文 {article_id} 无正文")
    sections = [f"**站内长文原文**：https://articles.zsxq.com/id_{article_id}.html", text]
    if content.images:
        if not VISION_OCR_BIN.exists():
            raise RuntimeError("站内长文含图片，但 vision_ocr 不可用")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for index, image_url in enumerate(content.images, 1):
                image = Path(tmp) / f"article-{index}.jpg"
                if not download_image(image_url, image):
                    raise RuntimeError(f"站内长文 {article_id} 图片 {index} 下载失败")
                layout = render_ocr_layout(run_vision_ocr(image))
                if not layout:
                    raise RuntimeError(f"站内长文 {article_id} 图片 {index} OCR 失败")
                sections.append(f"**长文图片OCR（图{index}）**：\n\n```\n{layout}\n```")
    return "\n\n".join(sections)


def format_topic(topic: dict, token: str | None = None) -> dict | None:
    """将单个 topic 格式化为输出格式；提供 token 时抓取完整评论"""
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
    tags = extract_tags(raw_text) + extract_tags(a_raw)
    files = extract_files(topic)

    topic_id = str(topic.get("topic_id", "")).strip()
    if not topic_id:
        raise RuntimeError("帖子缺少 topic_id，无法生成稳定分析 ID")

    # 提取日期和作者
    created = topic.get("create_time", "")
    if ttype == "q&a":
        owner = question.get("owner", {}).get("name", "") if question else ""
        answer_owner = answer.get("owner", {}).get("name", "") if answer else ""
    else:
        owner = talk.get("owner", {}).get("name", "") if talk else ""
        answer_owner = ""

    # 构建完整帖子文本
    if ttype == "q&a":
        full_text = ""
        if q_text:
            full_text += f"**{owner} 提问：**\n\n{q_text}\n\n"
        if a_text:
            if answer_owner == FOCUS_OWNER:
                full_text += f"**{FOCUS_OWNER}（重点关注）回答：**\n\n{a_text}"
            else:
                full_text += f"**{answer_owner} 回答：**\n\n{a_text}"
    else:
        full_text = text
        if talk.get("article"):
            if token is None:
                raise RuntimeError("站内长文需要认证 token 才能导出全文")
            full_text += "\n\n" + fetch_article(talk["article"], token)

    # 提取标题；提问帖只基于问题正文，避免把“某某提问”署名当作标题
    title_source = q_text if ttype == "q&a" else full_text
    title = extract_post_title(title_source, ttype)

    # 每篇帖子单独请求评论接口；时间线的 show_comments 仅为预览，不可替代全量评论。
    if token is None:
        comments = extract_comments(topic)
        focus_comment_count = 0
    else:
        comments_full = fetch_comments(token, topic_id)
        comments = render_comments(comments_full)
        # 只统计作者本人；普通用户“回复 三线文案大锅饭”不计入重点评论。
        focus_comment_count = sum(
            line.lstrip("　").startswith(f"**{FOCUS_OWNER}") for line in comments
        )
        import time
        time.sleep(COMMENT_SLEEP)

    # 对图片执行 OCR
    ocr_text = ocr_topic_images(topic)

    return {
        "topic_id": topic_id,
        "type": ttype,
        "created": created,
        "owner": owner,
        "answer_owner": answer_owner,
        "score": score,
        "title": title,
        "text": full_text,
        "tags": tags,
        "files": files,
        "comments": comments,
        "comments_truncated": token is None,
        "focus_comment_count": focus_comment_count,
        "ocr": ocr_text,
    }


def generate_markdown(
    posts: list[dict],
    qa_posts: list[dict],
    start_date: str,
    end_date: str,
    total_count: int,
    filtered_count: int,
    no_filter: bool,
) -> str:
    """生成 Markdown 文件内容，用户提问单独归入专属章节。"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    if no_filter:
        count_line = (
            f"> 星主帖子：{len(posts)} 篇 | 用户提问：{len(qa_posts)} 篇"
            f"（API 返回 {total_count} 条）"
        )
    else:
        count_line = (
            f"> 星主帖子：{len(posts)} 篇（评分过滤 {filtered_count} 篇）"
            f" | 用户提问：{len(qa_posts)} 篇（不参与评分过滤）"
        )

    lines = [
        "# 大锅饭与小伙伴的进步空间（原始数据）",
        "",
        f"> 来源：https://wx.zsxq.com/group/{GROUP_ID}",
        f"> 导出时间：{now}",
        f"> 日期范围：{start_date} ~ {end_date}",
        count_line,
        "",
        "---",
    ]

    def append_post(post: dict, question: bool = False) -> None:
        score_str = f"{post['score']}" if post["score"] is not None else "无"
        if post["created"]:
            dt = datetime.fromisoformat(post["created"])
            time_str = dt.strftime("%m-%d %H:%M")
        else:
            time_str = "未知"

        if question:
            lines.append(f"### ❓ ⭐ {score_str}分 | {time_str} | {post['title']} | ID `{post['topic_id']}`")
        else:
            lines.append(f"## ⭐ {score_str}分 | {time_str} | {post['title']} | ID `{post['topic_id']}`")
        lines.append("")
        lines.append(post["text"])

        if post["tags"]:
            lines.append("")
            lines.append(f"**标签**：{' '.join(post['tags'])}")

        if post["files"]:
            lines.append("")
            lines.append(f"**附件**：{', '.join(post['files'])}")

        if post.get("ocr"):
            lines.append("")
            lines.append(post["ocr"])

        if post["comments"]:
            lines.append("")
            lines.append("评论原文：")
            for comment in post["comments"]:
                lines.append(comment)

        lines.append("")
        lines.append("---")
        lines.append("")

    for post in posts:
        append_post(post)

    if qa_posts:
        lines.extend(
            [
                "## 🙋 用户提问",
                "",
                "> 本章节保留日期范围内抓取到的全部用户提问及回答，按发布时间降序排列，不参与能量评分过滤。",
                "",
                "---",
                "",
            ]
        )
        for post in qa_posts:
            append_post(post, question=True)

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="知识星球帖子导出")
    parser.add_argument("token", nargs="?", default=None, help="zsxq_access_token（可选，不提供则从 .env 读取）")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="token 所在的 .env 文件路径")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=5, help="最近N天（默认5天）")
    parser.add_argument("--no-filter", action="store_true", help="不按能量评分过滤，保留所有帖子")
    parser.add_argument("--min-score", type=float, default=None, help="最低能量评分过滤阈值（默认不过滤）")
    args = parser.parse_args()

    # 解析 token：优先使用命令行参数，否则从 .env 文件读取
    token = args.token
    if not token:
        token = read_token_from_env(args.env_file)
        if token:
            print(f"已从 {args.env_file} 读取 token")
        else:
            parser.error(
                "未提供 token，且无法从 .env 文件读取。\n"
                f"请确认 {args.env_file} 存在并包含 zsxq_access_token，\n"
                "或通过命令行参数传入 token。"
            )

    # 计算日期范围
    now = datetime.now(CST)
    if args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
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

    # 获取帖子
    topics = fetch_topics(token, start_date, end_date)
    print(f"API 返回 {len(topics)} 条帖子")

    # 格式化：星主帖子与用户提问分别保存
    all_posts = []
    qa_posts = []
    filtered_count = 0
    for topic in topics:
        # 日期过滤前置：避免为范围外帖子发起多余的评论接口请求
        ct = topic.get("create_time", "")
        if ct and (ct[:10] < start_date or ct[:10] > end_date):
            filtered_count += 1
            continue

        post = format_topic(topic, token)
        if post is None:
            continue

        # 用户提问全部保留到独立章节，不参与能量评分过滤
        if post["type"] == "q&a":
            qa_posts.append(post)
            continue

        # 能量评分过滤仅作用于星主帖子，且只在指定 --min-score 时生效
        if not no_filter:
            if post["score"] is None or post["score"] < min_score:
                filtered_count += 1
                continue

        all_posts.append(post)

    # 星主帖子：评分降序，同评分按时间降序；用户提问：按时间降序
    all_posts.sort(key=lambda post: post["created"], reverse=True)
    all_posts.sort(
        key=lambda post: post["score"] if post["score"] is not None else -1,
        reverse=True,
    )
    qa_posts.sort(key=lambda post: post["created"], reverse=True)

    print(
        f"保留 {len(all_posts)} 篇星主帖子"
        + (f"（评分过滤 {filtered_count} 篇）" if filtered_count > 0 else "")
    )
    print(f"保留用户提问（q&a）帖子：{len(qa_posts)} 篇，写入独立章节")
    kept = all_posts + qa_posts
    focus_total = sum(p["focus_comment_count"] for p in kept)
    print(f"评论抓取：{sum(len(p['comments']) for p in kept)} 条（其中 {FOCUS_OWNER} 重点评论 {focus_total} 条）")

    # 生成 Markdown
    md = generate_markdown(
        all_posts,
        qa_posts,
        start_date,
        end_date,
        len(topics),
        filtered_count,
        no_filter,
    )

    # 写入文件
    mmdd = now.strftime("%m%d")
    output_path = OUTPUT_DIR / f"{mmdd}_大锅饭_raw_{start_date}_{end_date}.md"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md, encoding="utf-8")

    print(f"\n导出完成！")
    print(f"文件路径：{output_path}")
    print(f"星主帖子数量：{len(all_posts)}")
    print(f"用户提问数量：{len(qa_posts)}（已写入独立章节）")
    if not no_filter:
        print(f"评分过滤：{filtered_count} 篇星主帖子（评分<{min_score}或无评分）")
    print(f"日期范围：{start_date} ~ {end_date}")
    print(f"\n可使用 zsxq-analyse skill 对 raw 文件进行浓缩总结")


if __name__ == "__main__":
    main()
