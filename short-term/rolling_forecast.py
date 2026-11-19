#!/usr/bin/env python3
"""Historical calendar-day sentiment windows; market overview only, not holding forecasts."""

import argparse
from datetime import date, datetime, time, timedelta
import importlib.util
import json
from pathlib import Path
import re
from typing import NamedTuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


TURNOVER_MODULE = Path("/Users/melo/src-skills/dailyTradeSum/me1o-tushare/scripts/fetch_daily_trade_sum.py")
TURNOVER_ROOT = Path("/Users/melo/doc-Dgf/Tushare")


def turnover_snapshot(prior_day: date, root: Path = TURNOVER_ROOT) -> dict:
    """Get only the known preceding complete session, with at most one API request."""
    unavailable = {"trade_date": prior_day.isoformat(), "status": "unavailable"}
    if not TURNOVER_MODULE.is_file():
        return {**unavailable, "reason": "Tushare turnover module missing"}
    spec = importlib.util.spec_from_file_location("daily_trade_sum", TURNOVER_MODULE)
    if spec is None or spec.loader is None:
        return {**unavailable, "reason": "Tushare turnover module cannot load"}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        path = root / prior_day.strftime("%m%d") / "daily_trade_sum.csv"
        report = path.with_name("daily_trade_sum_validation.json")
        if path.is_file() and report.is_file():
            validation = json.loads(report.read_text(encoding="utf-8"))
            if validation.get("status") == "passed":
                rows = module.read_csv(path)
                matching = [row for row in rows if row["trade_date"] == prior_day.isoformat()]
                if len(matching) == 1 and not module.validate_records(matching, prior_day, 1):
                    return {**matching[0], "status": "available", "source": str(path)}
        # Never scan backwards over holidays; prior_day comes from observed market history.
        token = module.load_token(module.DEFAULT_ENV_FILE)
        row = module.summarize(module.api_call(token, prior_day.strftime("%Y%m%d")), prior_day.strftime("%Y%m%d"))
        if row is None or module.validate_records([row], prior_day, 1):
            return {**unavailable, "reason": "No valid Tushare daily stock turnover"}
        return {**row, "status": "available", "source": "Tushare daily API"}
    except Exception as exc:
        return {**unavailable, "reason": f"{type(exc).__name__}: {exc}"}


def previous_complete_session(codes: list[str], cutoff: datetime) -> date | None:
    """Observed exchange session only; never treat today's intraday quote as close."""
    end = cutoff.date() if cutoff.time() >= time(15, 5) else cutoff.date() - timedelta(days=1)
    for code in codes[:1]:
        try:
            history = prices(code, end - timedelta(days=35), end)
            dates = [day for day in history if day <= end]
            return max(dates) if dates else None
        except Exception:
            return None
    return None


class PostSignal(NamedTuple):
    timestamp: datetime
    score: int
    scope: str
    timescale: str
    evidence: str


def market_eligible(post: PostSignal) -> bool:
    """Only an explicit broad-market, two-trading-day judgment is comparable."""
    return not (post.score == 0 and post.evidence.startswith("方向信息不足")) and post.timescale == "未来两个交易日" and (
        post.scope == "大盘整体" or post.scope.startswith("大盘整体/") or
        (post.scope.startswith("大盘整体（") and post.scope.endswith("）"))
    )


def direction(mean: float) -> str:
    return "看涨" if mean > 0 else "看跌" if mean < 0 else "中性"


def detailed_cards(path: Path, *, require_scope: bool = True) -> dict[str, PostSignal]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    found = {}
    for card in soup.select("article.post-card, article.sub-card"):
        post_id = card.get("data-post-id")
        stamp = card.get("data-ts")
        if post_id is None or stamp is None:
            # BruceGold sub-card: extract from meta paragraph
            meta = card.select_one("p.meta")
            if meta:
                meta_text = meta.get_text(" ", strip=True)
                m = re.search(r"发布时间：(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", meta_text)
                if m:
                    stamp = f"{m.group(1)} {m.group(2)}"
                link = card.select_one("a[href*='douyin.com/video/']")
                if link:
                    post_id = link.get("href", "").rstrip("/").split("/")[-1]
        stance = card.select_one('p.market-stance[data-source="analysis"]')
        if not post_id or not stamp or stance is None:
            raise ValueError(f"{path}: 卡片缺少 ID、时间或市场态度")
        strong = stance.select_one("strong")
        match = re.fullmatch(r"市场态度：([+-]?[0-2])", strong.get_text(strip=True)) if strong else None
        if not match:
            raise ValueError(f"{path}: 帖子 {post_id} 态度格式错误")
        text = stance.get_text("", strip=True).split(strong.get_text(strip=True), 1)[-1]
        details = re.match(r"^｜([^｜]+)｜(.+?)\s*—\s*依据：", text)
        if require_scope and not details:
            raise ValueError(f"{path}: 帖子 {post_id} 缺少范围或时间尺度")
        if post_id in found:
            raise ValueError(f"{path}: 重复帖子 {post_id}")
        scope, timescale = (details.group(1).strip(), details.group(2).strip()) if details else ("", "")
        evidence = text.split("依据：", 1)[1].strip() if details else ""
        found[post_id] = PostSignal(datetime.strptime(stamp, "%Y-%m-%d %H:%M"), int(match.group(1)), scope, timescale, evidence)
    return found


def cards(path: Path) -> dict[str, tuple[datetime, int]]:
    """Retain the timestamp/score pair used by feedback freezing."""
    return {post_id: (post.timestamp, post.score) for post_id, post in detailed_cards(path, require_scope=False).items()}

def prices(code: str, start: date, end: date) -> dict[date, tuple[float, float]]:
    import code_info  # noqa: F401  — mount curl adapter before efinance calls
    import efinance as ef
    history = ef.stock.get_quote_history(
        code, beg=(start - timedelta(days=12)).strftime("%Y%m%d"),
        end=end.strftime("%Y%m%d"), fqt=0,
    )
    if history is None or history.empty:
        raise ValueError(f"{code}: 无行情数据")
    return {
        date.fromisoformat(str(row.日期)): (float(row.收盘), float(row.涨跌幅))
        for row in history[["日期", "收盘", "涨跌幅"]].itertuples(index=False)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("duoyu", type=Path)
    parser.add_argument("dgf", type=Path)
    parser.add_argument("--brucegold", type=Path, default=None, help="BruceGold 字幕汇总 HTML（可选第三源）")
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--window-days", type=int, default=3, metavar="N", help="每个滚动窗口的自然日数（1–5，默认 3）")
    args = parser.parse_args()
    if not 1 <= args.window_days <= 5:
        parser.error("--window-days 必须在 1 到 5 之间")
    names = (args.duoyu.name, args.dgf.name)
    ranges = [re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.html$", name) for name in names]
    if not all(ranges) or ranges[0].groups() != ranges[1].groups():
        parser.error("两份 final HTML 的日期范围不一致或文件名不符合约定")
    start, end = (date.fromisoformat(part) for part in ranges[0].groups())
    if (end - start).days != 4:
        parser.error("需要同一期连续 5 个自然日的 HTML")
    sources = {"多鱼": detailed_cards(args.duoyu), "大锅饭": detailed_cards(args.dgf)}
    if args.brucegold:
        sources["布鲁斯"] = detailed_cards(args.brucegold)
    source_label = "三源" if len(sources) == 3 else "双源"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    prior = previous_complete_session(args.codes, now)
    print("第一部分：市场概览（历史窗口仅回溯性情绪，不是持仓预测）")
    if prior is None:
        print("沪深 A 股股票成交额：不可用（无法确认上一完整交易日）")
    else:
        snapshot = turnover_snapshot(prior)
        if snapshot["status"] == "available":
            print(f"上一完整交易日 {prior} 沪市 {float(snapshot['shanghai_amount_100m_cny']):.2f}、深市 {float(snapshot['shenzhen_amount_100m_cny']):.2f}、合计 {float(snapshot['total_amount_100m_cny']):.2f} 亿元（不含 ETF、债券、北交所；{snapshot['source']}）")
        else:
            print(f"上一完整交易日 {prior} 沪深 A 股股票成交额：不可用（{snapshot['reason']}）")
    for offset in range(6 - args.window_days):
        first = start + timedelta(days=offset)
        target = first + timedelta(days=args.window_days)
        cutoff = datetime.combine(target, time(9, 30))
        print(f"\n窗口 {first} ~ {target - timedelta(days=1)} → {target}（自然日次日代理目标；非下一交易日）")
        if target <= end:
            print("  仅纳入目标日09:30前帖子；09:30起排除。HTML 包含目标日期，不保证当天帖子完整。")
        else:
            print("  目标日不在五日 HTML 范围内：无法纳入目标日09:30前帖子；仅用此前已收录帖子预测。")
        print("  大盘合并口径：仅大盘整体（含兼及其他领域）、未来两个交易日；其他范围或期限只进入全帖分布。")
        source_means = []
        eligible_scores = []
        source_counts = []
        for label, source in sources.items():
            posts = [post for post in source.values() if first <= post.timestamp.date() and post.timestamp < cutoff]
            scores = [post.score for post in posts]
            counts = {score: scores.count(score) for score in range(-2, 3)}
            average = sum(scores) / len(scores) if scores else None
            print(f"  {label} 全帖: {len(scores)}篇，均分 {average:+.2f} / {direction(average)}" if average is not None else f"  {label} 全帖: 0篇，无信号")
            print(f"    分布 -2/-1/0/+1/+2: {'/'.join(str(counts[x]) for x in range(-2, 3))}")
            eligible = [post.score for post in posts if market_eligible(post)]
            if eligible:
                mean = sum(eligible) / len(eligible)
                source_counts.append(len(eligible))
                source_means.append(mean)
                eligible_scores.extend(eligible)
                print(f"    可比大盘信号: {len(eligible)}篇，均分 {mean:+.2f} / {direction(mean)}")
            else:
                print("    可比大盘信号: 0篇，来源缺口")
        if len(source_means) != len(sources):
            print(f"  合并大盘方向: 来源缺口；不以单一来源代替{source_label}")
        else:
            mean = sum(source_means) / len(source_means)
            caveats = []
            if all(c == 1 for c in source_counts):
                caveats.append("每源仅一篇，样本不足")
            elif 1 in source_counts:
                caveats.append("至少一源仅一篇，样本不足")
            if min(eligible_scores) < 0 < max(eligible_scores):
                caveats.append("正负信号抵消，方向较弱")
            if mean != 0 and f"{mean:+.2f}" in ("+0.00", "-0.00"):
                caveats.append("均分在显示精度内接近零，方向较弱")
            if mean == 0:
                caveats.append("均分为零，无明确方向")
            note = f"；{'；'.join(caveats)}" if caveats else ""
            print(f"  合并大盘方向: {direction(mean)}（{source_label}均分再等权 {mean:+.2f}；各源样本见上{note}）")


if __name__ == "__main__":
    main()
