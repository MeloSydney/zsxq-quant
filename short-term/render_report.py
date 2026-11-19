#!/usr/bin/env python3
"""Render the short-term three-part markdown report to standalone HTML.

Usage:
    python3 render_report.py <input.md> <output.html>

Supports the subset used by the short-term output template: #/##/#### headings,
tables, bullet lists, bold, inline code, code blocks, --- rules. Zero deps.
Atomic write.
"""
import html
import os
import re
import sys
import tempfile
from datetime import datetime


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def render(md: str) -> str:
    lines = md.splitlines()
    out, i, in_code = [], 0, False
    code_buf = []

    def flush_para(buf):
        if buf:
            out.append("<p>" + "<br>".join(inline(x) for x in buf) + "</p>")
            buf.clear()

    para = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf, in_code = [], False
            else:
                flush_para(para)
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        s = line.strip()
        if not s:
            flush_para(para)
        elif s.startswith("|"):
            flush_para(para)
            # gather table block
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
            if body:
                t = "<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in body[0]) + "</tr></thead><tbody>"
                for r in body[1:]:
                    t += "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                out.append(t + "</tbody></table>")
            continue
        elif s.startswith("####"):
            flush_para(para)
            out.append(f"<h4>{inline(s.lstrip('#').strip())}</h4>")
        elif s.startswith("###"):
            flush_para(para)
            out.append(f"<h3>{inline(s.lstrip('#').strip())}</h3>")
        elif s.startswith("##"):
            flush_para(para)
            out.append(f"<h2>{inline(s.lstrip('#').strip())}</h2>")
        elif s.startswith("#"):
            flush_para(para)
            out.append(f"<h1>{inline(s.lstrip('#').strip())}</h1>")
        elif s == "---":
            flush_para(para)
            out.append("<hr>")
        elif s.startswith("- "):
            flush_para(para)
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        else:
            para.append(s)
        i += 1
    flush_para(para)
    return "\n".join(out)


CSS = """
:root{--bg:#f4f1ea;--card:#fffdf8;--border:#f4dfd2;--accent:#fff4cc;--fg:#2b2620;--muted:#8a7f6e}
@media (prefers-color-scheme:dark){:root{--bg:#171613;--card:#22201c;--border:#492b20;--accent:#3b3018;--fg:#e8e2d6;--muted:#a2988a}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif}
main{max-width:980px;margin:0 auto}
h1{font-size:22px;border-bottom:2px solid var(--border);padding-bottom:8px}
h2{font-size:18px;margin-top:28px;background:var(--accent);padding:6px 10px;border-radius:6px}
h3,h4{margin:16px 0 6px}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px;background:var(--card)}
th,td{border:1px solid var(--border);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--accent)}
code{background:var(--card);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:13px}
pre{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:12px;overflow:auto}
ul{padding-left:22px}li{margin:3px 0}
hr{border:0;border-top:1px solid var(--border);margin:20px 0}
.meta{color:var(--muted);font-size:12px;text-align:right;margin-top:24px}
"""


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: render_report.py <input.md> <output.html>")
    src, dst = sys.argv[1], sys.argv[2]
    md = open(src, encoding="utf-8").read()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           "<title>短线情绪与持仓报告</title><style>" + CSS + "</style></head><body><main>"
           + render(md) + f'<p class="meta">rendered {stamp} · short-term render_report.py</p>'
           "</main></body></html>")
    d = os.path.dirname(os.path.abspath(dst))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)
    print(f"written: {dst}")


if __name__ == "__main__":
    main()
