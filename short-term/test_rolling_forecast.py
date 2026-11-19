"""Consumer-facing summaries must not confuse a sector post with the A-share market."""

import sys
from datetime import date

import rolling_forecast as rolling


def _html(path, posts):
    path.write_text("<html>" + "".join(
        f'<article class="post-card" data-post-id="{identifier}" data-ts="2026-11-13 13:00">'
        f'<p class="market-stance" data-source="analysis"><strong>市场态度：{score:+d}</strong>'
        f'｜{scope}｜未来两个交易日 — 依据：独立判断</p></article>'
        for identifier, score, scope in posts
    ) + "</html>")


def test_market_direction_excludes_sector_and_balances_sources(tmp_path, monkeypatch, capsys):
    duoyu = tmp_path / "1116-多鱼朋友圈_2026-11-12_2026-11-16.html"
    dgf = tmp_path / "1116_大锅饭_2026-11-12_2026-11-16.html"
    _html(duoyu, [("market", -2, "大盘整体/仓位管理"),
                   ("sector1", 2, "光通信"), ("sector2", 2, "存储"),
                   ("sector3", 2, "新能源")])
    _html(dgf, [("other-market", 1, "大盘整体")])
    monkeypatch.setattr(rolling, "previous_complete_session", lambda _codes, _now: None)
    monkeypatch.setattr(sys, "argv", ["rolling_forecast.py", str(duoyu), str(dgf), "--codes", "300308", "--window-days", "5"])
    rolling.main()
    output = capsys.readouterr().out
    assert "多鱼 全帖: 4篇，均分 +1.00" in output
    assert "多鱼" in output and "可比大盘信号: 1篇，均分 -2.00" in output
    assert "合并大盘方向: 看跌（双源均分再等权 -0.50" in output
    assert "样本不足" in output


def test_market_direction_needs_both_sources(tmp_path, monkeypatch, capsys):
    duoyu = tmp_path / "1116-多鱼朋友圈_2026-11-12_2026-11-16.html"
    dgf = tmp_path / "1116_大锅饭_2026-11-12_2026-11-16.html"
    _html(duoyu, [("neutral-news", 0, "大盘整体")])
    _html(dgf, [("market", -1, "大盘整体")])
    monkeypatch.setattr(rolling, "previous_complete_session", lambda _codes, _now: date(2026, 11, 13))
    monkeypatch.setattr(rolling, "turnover_snapshot", lambda _prior: {"status": "unavailable", "reason": "no dated data"})
    monkeypatch.setattr(sys, "argv", ["rolling_forecast.py", str(duoyu), str(dgf), "--codes", "300308", "--window-days", "5"])
    duoyu.write_text(duoyu.read_text().replace("依据：独立判断", "依据：方向信息不足：仅资讯转述"))
    rolling.main()
    assert "合并大盘方向: 来源缺口；不以单一来源代替双源" in capsys.readouterr().out
