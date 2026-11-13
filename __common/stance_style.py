"""Shared reviewed-stance presentation for source-specific ZSXQ HTML pages.

Only the annotation CSS and its idempotent style installation are common;
source adapters own card discovery, placement, and page layout.
"""

import re

STANCE_CSS = """.market-stance[data-source="analysis"] { margin:1rem 0; padding:.8rem 1rem; border:1px solid var(--line); border-left:4px solid var(--gold,var(--star-line)); border-radius:7px; background:var(--summary,var(--star-bg)); color:var(--text); line-height:1.75; }
.market-stance[data-source="analysis"] strong { color:var(--gold,var(--star-text)); }
.market-stance[data-source="analysis"] .stance-origin { color:var(--muted); font-size:.88em; }
"""


def install_stance_style(html: str, marker: str) -> str:
    """Replace only this adapter's managed style block, rejecting broken markers."""
    begin = f"/* {marker}:begin */"
    end = f"/* {marker}:end */"
    block = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
    if html.count("</style>") != 1 or html.count(begin) != html.count(end) or html.count(begin) > 1:
        raise ValueError("HTML 缺少唯一的样式插入点或已有市场态度样式损坏")
    if begin in html and len(block.findall(html)) != 1:
        raise ValueError("已有市场态度样式标记损坏")
    html = block.sub("", html)
    return html.replace("</style>", begin + "\n" + STANCE_CSS + end + "\n</style>", 1)
