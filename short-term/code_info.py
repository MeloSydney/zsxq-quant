#!/usr/bin/env python3
"""用 efinance 解析持仓代码：名称 + 最近行情，供 short-term 决策做文本匹配参考。

用法: .venv/bin/python code_info.py <code> [<code> ...]
每个代码输出一行: 代码 名称 | 最近3个交易日 收盘/涨跌幅
单个代码解析失败不终止，输出解析失败并继续。

注意: eastmoney 的 WAF 会掐断 python TLS 栈的连接（urllib3 与标准库
http.client 均会被断连，curl 正常）。因此在 efinance 的共享 session 上挂了
一个走 curl 子进程的 HTTPAdapter（含 3 次重试）；efinance 的 API 层不受
影响，勿移除该兼容层。
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import subprocess
import sys
import time as clock

import requests
import requests.adapters
from efinance.shared import session as _ef_session

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


class _CurlAdapter(requests.adapters.HTTPAdapter):
    """用 curl 子进程发送请求，规避 eastmoney 对 python TLS 栈的断连。"""

    def send(self, request, timeout=None, **kwargs):
        t = int(timeout if isinstance(timeout, (int, float)) else 30)
        cmd = ["curl", "-s", "-m", str(t), "-A", _UA,
               "-w", "\n__HTTP_STATUS__%{http_code}"]
        if request.method and request.method.upper() not in ("GET", "HEAD"):
            cmd += ["-X", request.method.upper()]
        if request.body:
            cmd += ["--data-binary", "@-"]
        cmd.append(request.url)
        last_err = b""
        for attempt in range(3):  # 间歇性掐连接，重试即可通过
            p = subprocess.run(cmd, input=request.body or b"", capture_output=True)
            out = p.stdout
            if out and b"__HTTP_STATUS__" in out:
                body, _, status = out.rpartition(b"__HTTP_STATUS__")
                code = int(status.strip() or b"200")
                resp = requests.Response()
                resp.status_code = code
                resp._content = body.rstrip(b"\n")
                resp.url = request.url
                resp.request = request
                return resp
            last_err = p.stderr[:200]
            if attempt == 2:
                raise requests.exceptions.ConnectionError(f"curl failed: {last_err!r}")
            clock.sleep(1.5)
        raise requests.exceptions.ConnectionError("unreachable")


_ef_session.headers.update({"User-Agent": _UA})
_ef_session.mount("https://", _CurlAdapter())
_ef_session.mount("http://", _CurlAdapter())


def resolve(code: str) -> str:
    import efinance as ef  # 导入时机在 monkey-patch 之后

    try:
        h = ef.stock.get_quote_history(code)
        if h is None or h.empty:
            return f"{code} 解析失败(无数据)"
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        # efinance can return a current-day intraday snapshot labelled as 收盘.
        latest_complete = now.date() if now.time() >= time(15, 5) else now.date() - timedelta(days=1)
        h = h[h["日期"].astype(str) <= latest_complete.isoformat()]
        if h.empty:
            return f"{code} 解析失败(尚无完整收盘行情)"
        name = str(h["股票名称"].iloc[0])
        rows = h.tail(3)[["日期", "收盘", "涨跌幅"]].itertuples(index=False)
        recent = "; ".join(f"{r.日期} 收{r.收盘:g} {r.涨跌幅:+.2f}%" for r in rows)
        return f"{code} {name} | {recent}"
    except Exception as e:  # 网络/API 异常不终止
        return f"{code} 解析失败({type(e).__name__}: {e})"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: code_info.py <code> [<code> ...]")
    for c in sys.argv[1:]:
        print(resolve(c))
