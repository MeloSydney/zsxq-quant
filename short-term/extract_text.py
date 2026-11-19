#!/usr/bin/env python3
"""把 dgf/duoyu/brucegold 流水线产出的 final HTML 提取为纯文本，供 short-term 持仓决策阅读。

支持两种卡片格式：
- post-card（dgf/duoyu）：data-ts、data-post-id、.summary、details.raw-block
- sub-card（brucegold）：meta 发布时间、视频 URL、section.analysis、details.transcript

跳过图片/base64、评论区、样式脚本。用法：
    python3 extract_text.py <final.html>
"""
import html as htmlmod
import re
import sys


def strip_tags(s: str) -> str:
    s = re.sub(r"<style.*?</style>|<script.*?</script>", "", s, flags=re.S)
    s = re.sub(r"data:image/[^\"']+", "", s)  # 丢弃 base64 图片数据
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmlmod.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def grab(block: str, pattern: str) -> str:
    m = re.search(pattern, block, flags=re.S)
    return strip_tags(m.group(1)) if m else ""


def main(path: str) -> None:
    s = open(path, encoding="utf-8").read()
    out = []

    # post-card format (dgf/duoyu)
    for c in re.split(r'<article class="post-card"', s)[1:]:
        ts_m = re.search(r'data-ts="([^"]+)"', c)
        id_m = re.search(r'data-post-id="([^"]+)"', c)
        post_id = id_m.group(1) if id_m else "?"
        ts = ts_m.group(1) if ts_m else "?"
        title = grab(c, r"<h3>(.*?)</h3>")
        stance = grab(c, r'<p class="market-stance"[^>]*>(.*?)</p>')
        summary = grab(c, r'<div class="summary">(.*?)</div>')
        raw = grab(c, r'<details class="raw-block"[^>]*>.*?<pre>(.*?)</pre>')
        if len(raw) > 700:
            raw = raw[:700] + " …(截断)"
        out.append(f"### [{ts}] {title} | ID {post_id}\n态度行: {stance}\n总结: {summary}\n原文: {raw}\n")

    # sub-card format (brucegold)
    for c in re.split(r'<article class="sub-card"', s)[1:]:
        meta_m = re.search(r"发布时间：(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", c)
        ts = meta_m.group(1).replace("T", " ")[:16] if meta_m else "?"
        url_m = re.search(r'href="(https://www\.douyin\.com/video/(\d+))"', c)
        post_id = url_m.group(2) if url_m else "?"
        title = grab(c, r"<h3>(.*?)</h3>")
        stance = grab(c, r'<p class="market-stance"[^>]*>(.*?)</p>')
        summary = grab(c, r'<h4>总结归纳</h4>\s*<p>(.*?)</p>')
        raw = grab(c, r'<details class="transcript"[^>]*>.*?<ol>(.*?)</ol>')
        if len(raw) > 700:
            raw = raw[:700] + " …(截断)"
        out.append(f"### [{ts}] {title} | ID {post_id}\n态度行: {stance}\n总结: {summary}\n原文: {raw}\n")

    print("\n".join(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法: extract_text.py <final.html>")
    main(sys.argv[1])
