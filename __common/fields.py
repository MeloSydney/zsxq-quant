"""Pure metadata fields shared by the two ZSXQ exporters."""

import re
import urllib.parse


def extract_score(text: str) -> float | None:
    """从文本中提取能量评分"""
    m = re.search(r"能量评分(\d+(?:\.\d+)?)分", text)
    if m:
        return float(m.group(1))
    return None


def extract_tags(text: str) -> list[str]:
    """从原始文本中提取标签"""
    tags = []
    for m in re.finditer(r'<e type="hashtag"[^>]*title="([^"]*?)"\s*/>', text):
        tags.append(urllib.parse.unquote(m.group(1)))
    return tags


def extract_files(topic: dict) -> list[str]:
    """提取附件文件名"""
    files = []
    talk = topic.get("talk", {})
    for f in talk.get("files", []):
        files.append(f.get("name", ""))
    return files


def extract_post_title(text: str, ttype: str) -> str:
    """提取帖子标题（15字以内）"""
    # 去掉能量评分前缀
    cleaned = re.sub(r"\*\*能量评分\d+(?:\.\d+)?分[。、]?（[^）]*）?\*\*", "", text)
    cleaned = re.sub(r"能量评分\d+(?:\.\d+)?分[。、]?（[^）]*）?", "", cleaned)
    cleaned = cleaned.strip()

    # 研报类：提取《》中的研报主题
    m = re.search(r"《([^》]+)》", cleaned)
    if m:
        title = m.group(1)
        return title[:15]

    # 提问类：提取问题核心
    if ttype == "q&a" or "？" in cleaned or "?" in cleaned:
        qm = re.search(r"[，。！\n]([^，。！？\n]{2,15}[？?])", cleaned)
        if qm:
            return qm.group(1)[:15]
        # 取前15字
        first = re.split(r"[。\n]", cleaned)[0].strip()
        return first[:15]

    # 取第一句
    first = re.split(r"[。\n]", cleaned)[0].strip()
    if len(first) > 15:
        first = first[:15]
    return first or "未命名"
