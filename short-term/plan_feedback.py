#!/usr/bin/env python3
"""Freeze decisions after a close or before the next open; score after two closes.

Usage:
  .venv/bin/python plan_feedback.py freeze DUOYU DGF --decisions decisions.json [--boards boards.json]
  .venv/bin/python plan_feedback.py review-pending

Records are immutable Markdown with a machine-readable JSON block. Human
decisions are never reconstructed from later posts or market prices.
"""

import argparse
from datetime import date, datetime, time, timedelta
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import urllib.request
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from rolling_forecast import TURNOVER_MODULE, prices, turnover_snapshot


def board_citations(picks: dict, boards: dict, sources: dict[str, Path], now: datetime) -> dict:
    """Verify provenance; membership alone never proves a post applies to a stock."""
    captured = datetime.fromisoformat(boards["fetched_at"])
    if (boards.get("schema_version") != 1 or boards.get("provider") != "efinance/Eastmoney get_belong_board"
            or captured.tzinfo is None or captured.astimezone(SHANGHAI).date() != now.date() or captured > now):
        raise ValueError("Board snapshot must be captured today before freeze")
    posts = {}
    for source, path in sources.items():
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        posts[source] = {}
        for card in soup.select("article.post-card"):
            stance = card.select_one('p.market-stance[data-source="analysis"]')
            if stance is not None:
                posts[source][card.get("data-post-id")] = (card.get("data-ts"), stance.get_text(" ", strip=True))
        for card in soup.select("article.sub-card"):
            stance = card.select_one('p.market-stance[data-source="analysis"]')
            meta = card.select_one("p.meta")
            if stance is None or meta is None:
                continue
            m = re.search(r"发布时间：(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", meta.get_text(" ", strip=True))
            link = card.select_one("a[href*='douyin.com/video/']")
            if m and link:
                vid = link.get("href", "").rstrip("/").split("/")[-1]
                posts[source][vid] = (f"{m.group(1)} {m.group(2)}", stance.get_text(" ", strip=True))
    result = {}
    for code, item in picks.items():
        stock_boards = boards.get("stocks", {}).get(code)
        if not isinstance(stock_boards, dict) or not stock_boards:
            raise ValueError(f"{code}: missing board membership")
        citations = item.get("board_evidence", [])
        if not isinstance(citations, list):
            raise ValueError(f"{code}: board_evidence must be a list")
        result[code] = []
        for cite in citations:
            if not isinstance(cite, dict):
                raise ValueError(f"{code}: malformed board citation")
            source, post_id, board_code, scope = (cite.get(key) for key in ("source", "post_id", "board_code", "scope"))
            rationale = cite.get("rationale")
            if (source not in posts or post_id not in posts[source] or board_code not in stock_boards
                    or not isinstance(scope, str) or len(scope.strip()) < 2
                    or not isinstance(rationale, str) or not rationale.strip()):
                raise ValueError(f"{code}: board citation needs real source/post/board and rationale")
            stamp, stance = posts[source][post_id]
            if scope not in stance or datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI) > now:
                raise ValueError(f"{code}: cited scope not in post stance or post is in future")
            board_name = stock_boards[board_code]
            relevance = ("exact" if scope == board_name else "related" if scope in board_name or board_name in scope
                         else "unverified")
            result[code].append({"source": source, "post_id": post_id, "post_time": stamp,
                                 "post_stance": stance, "scope": scope, "board_code": board_code,
                                 "board_name": board_name, "relevance": relevance, "rationale": rationale.strip()})
    return result

def post_citations(picks: dict, sources: dict[str, Path], cutoff: datetime) -> dict:
    """Freeze cited author judgments, not later HTML or free-text guesses."""
    from rolling_forecast import detailed_cards
    posts = {}
    result = {}
    for code, item in picks.items():
        refs = item.get("post_refs", [])
        if not isinstance(refs, list):
            raise ValueError(f"{code}: post_refs must be a list")
        result[code] = []
        seen = set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"source", "post_id"}:
                raise ValueError(f"{code}: post_refs need source and post_id")
            source, post_id = ref["source"], ref["post_id"]
            if source in sources and source not in posts:
                posts[source] = detailed_cards(sources[source])
            if source not in posts or post_id not in posts[source] or (source, post_id) in seen:
                raise ValueError(f"{code}: missing or duplicate cited post {source}/{post_id}")
            seen.add((source, post_id))
            post = posts[source][post_id]
            if post.timestamp.replace(tzinfo=SHANGHAI) > cutoff:
                raise ValueError(f"{code}: cited post after source cutoff")
            result[code].append({"source": source, "post_id": post_id,
                                 "post_time": post.timestamp.isoformat(), "score": post.score,
                                 "scope": post.scope, "timescale": post.timescale})
    return result

def market_context(sources: dict[str, Path], cutoff: datetime, late: bool) -> dict:
    """Freeze sentiment without promoting sector posts to broad-market evidence."""
    from rolling_forecast import detailed_cards, market_eligible
    context = {}
    for source, path in sources.items():
        posts = detailed_cards(path, require_scope=False)
        visible = [(post_id, post) for post_id, post in posts.items()
                   if (post.timestamp.replace(tzinfo=SHANGHAI) < cutoff if late else
                       post.timestamp.replace(tzinfo=SHANGHAI) <= cutoff)]
        broad = [(post_id, post) for post_id, post in visible if market_eligible(post)]
        context[source] = {
            "all_count": len(visible),
            "all_mean": round(sum(post.score for _, post in visible) / len(visible), 4) if visible else None,
            "broad_count": len(broad),
            "broad_mean": round(sum(post.score for _, post in broad) / len(broad), 4) if broad else None,
            "broad_posts": [{"post_id": post_id, "post_time": post.timestamp.isoformat(),
                             "score": post.score, "scope": post.scope, "timescale": post.timescale}
                            for post_id, post in broad],
        }
    return context

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEUTRAL_BAND_PERCENT = 0.5
ARCHIVE = Path("/Users/melo/doc-Dgf/short-term/_prediction")
CALENDAR_CACHE = Path(__file__).resolve().parent / "trade_cal_cache.json"

# Increment only when the evaluation policy changes. Historical prediction scoring
# versions remain frozen; rescore explicitly selects this policy, never a date.
CURRENT_EVALUATION_VERSION = 4


def _load_calendar_cache() -> dict[str, str]:
    if not CALENDAR_CACHE.exists():
        return {}
    try:
        data = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {key: str(value) for key, value in data.items() if str(value) in ("0", "1")}


def _save_calendar_cache(cache: dict[str, str]) -> None:
    CALENDAR_CACHE.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")


def _fetch_calendar(day: date, end: date) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("daily_trade_sum", TURNOVER_MODULE)
    if spec is None or spec.loader is None:
        raise ValueError("Tushare module unavailable for exchange calendar")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = module.load_token(module.DEFAULT_ENV_FILE)
    payload = {"api_name": "trade_cal", "token": token,
               "params": {"exchange": "SSE", "start_date": day.strftime("%Y%m%d"),
                          "end_date": end.strftime("%Y%m%d")}, "fields": "cal_date,is_open"}
    request = urllib.request.Request(module.API_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise ValueError(f"Tushare trade_cal unavailable: {result.get('code')} {result.get('msg', '')}")
    rows = (result.get("data") or {}).get("items") or []
    fetched = {}
    for stamp, flag in rows:
        session = date.fromisoformat(f"{str(stamp)[:4]}-{str(stamp)[4:6]}-{str(stamp)[6:8]}")
        if not day <= session <= end or str(flag) not in ("0", "1"):
            raise ValueError("Tushare trade_cal invalid session row")
        fetched[session.isoformat()] = str(flag)
    return fetched


def _calendar_map(day: date, end: date) -> dict[str, str]:
    """Return is_open flags for [day, end], using the local cache and only
    hitting Tushare when a needed date is missing (calendar rarely changes)."""
    cache = _load_calendar_cache()
    span = [day + timedelta(days=offset) for offset in range((end - day).days + 1)]
    missing = [d for d in span if d.isoformat() not in cache]
    if missing:
        fetched = _fetch_calendar(min(missing), end)
        cache.update(fetched)
        _save_calendar_cache(cache)
    return {d.isoformat(): cache[d.isoformat()] for d in span}


def next_session(day: date, require_open_today: bool = False) -> date:
    """Confirm the next Shanghai session from the cached exchange calendar."""
    end = day + timedelta(days=45)
    calendar = _calendar_map(day, end)
    today_confirmed = calendar.get(day.isoformat()) == "1"
    sessions = [date.fromisoformat(key) for key, flag in calendar.items()
                if flag == "1" and day < date.fromisoformat(key) <= end]
    if require_open_today and not today_confirmed:
        raise ValueError("Today is not a confirmed Shanghai trading session")
    if not sessions:
        raise ValueError("Tushare trade_cal did not confirm next session")
    return min(sessions)


def complete(day: date, now: datetime) -> bool:
    return day < now.date() or (day == now.date() and now.time() >= time(15, 5))


def read_hold(path: Path) -> list[str]:
    codes = [line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()]
    codes = [code for code in codes if code]
    if not codes or len(codes) != len(set(codes)) or any(not code.isdigit() for code in codes):
        raise ValueError("Hold list empty, duplicated, or invalid")
    return codes




RECORD_START = "## Machine-readable record\n\n````json\n"
RECORD_END = "\n````\n"


def load_record(path: Path, kind: str) -> dict:
    text = path.read_text(encoding="utf-8")
    if text.count(RECORD_START) != 1 or not text.endswith(RECORD_END):
        raise ValueError(f"{path}: malformed Markdown record")
    data = json.loads(text.split(RECORD_START, 1)[1][:-len(RECORD_END)])
    if data.get("record_type") != kind:
        raise ValueError(f"{path}: expected {kind} record")
    return data



def save_new(path: Path, payload: dict) -> None:
    created = payload["frozen_at"] if payload["record_type"] == "prediction" else payload["reviewed_at"]
    if path.suffix != ".md" or not path.name.startswith(datetime.fromisoformat(created).strftime("%m%d_")):
        raise ValueError("Markdown filename must start with its creation MMDD_")
    if payload["record_type"] == "prediction":
        heading = "# 盘中延迟出具预测（仅盘前输入）" if payload.get("provenance") == "late_preopen_snapshot" else "# 两交易日持仓预测"
        lines = [heading, "", f"出具时间：{created}",
                 f"基准交易日：{payload['baseline_session']}；首个目标交易日：{payload['first_target_session']}",
                 f"输入截止：{payload.get('source_cutoff', created)}", ""]
        for code, item in payload["decisions"].items():
            lines.extend([f"### {code} — {item['action']}",
                          f"收盘基准：{item['baseline']['close']}；判断依据：{json.dumps(item['evidence'], ensure_ascii=False)}", ""])
    else:
        provenance = payload.get("provenance", "frozen")
        heading = ("# 盘中留档回顾评分（非冻结预测反馈）" if provenance == "intraday_archived_unfrozen"
                   else "# 盘中延迟出具预测反馈（含出具当日行情）" if provenance == "late_preopen_snapshot"
                   else "# 两交易日预测反馈")
        lines = [heading, "", f"复盘时间：{created}",
                 f"原预测：{payload['forecast_path']}（SHA-256：{payload['forecast_sha256']}）",
                 f"原预测记录评分版本：v{payload.get('source_scoring_version', payload['scoring_version'])}；本次评估政策版本：v{payload.get('evaluation_version', payload['scoring_version'])}",
                 f"证据类型：{provenance}；不同类型不得合并统计",
                 f"实际交易日：{', '.join(payload['target_sessions'])}", ""]
        for code, item in payload["individual"].items():
            label = (f"{payload['baseline_session']} 基准至 {payload['target_sessions'][0]} 收盘涨跌（含留档前走势）" if provenance == "intraday_archived_unfrozen"
                     else "基准日至第二目标日涨跌（含出具当日）" if provenance == "late_preopen_snapshot" else "两日涨跌")
            field = ("prior_close_to_session_close_return_percent" if provenance == "intraday_archived_unfrozen"
                     else "baseline_to_second_target_return_percent" if provenance == "late_preopen_snapshot" else "two_session_return_percent")
            rating = "弃权" if item.get("signed_score") is None else f"{item['signed_score']:+d}"
            lines.extend([f"### {code} — {item['action']}",
                          f"{label}：{item[field]:+.4f}%；评分：{rating}", ""])
            refs = item.get("post_attribution", [])
            if refs:
                for ref in refs:
                    source = {"duoyu": "多鱼", "dgf": "大锅饭", "brucegold": "布鲁斯（字幕/未经人工复核）"}.get(ref["source"], ref["source"])
                    lines.append(f"  帖子对照：{source} {ref['post_time']} ID {ref['post_id']} "
                                 f"({ref['scope']} / {ref['timescale']}, {ref['score']:+d}) — {ref['alignment']}"
                                 + (f"；板块关联 {ref['board_relevance']}" if 'board_relevance' in ref else ""))
                if item.get("attribution_status") == "partial_frozen_board_only":
                    lines.append("  仅引用了预测时冻结的板块帖子；其他文本依据未冻结结构化引用，无法归因")
            else:
                lines.append("  帖子对照：预测时未冻结结构化帖子引用，无法归因" if item.get("attribution_status") == "legacy_unavailable"
                             else "  帖子对照：该决策未引用可归因的方向帖")
            context = payload.get("market_context")
            if context:
                for name, entry in context.items():
                    broad_mean = entry["broad_mean"]
                    source = {"duoyu": "多鱼", "dgf": "大锅饭", "brucegold": "布鲁斯（字幕/未经人工复核）"}.get(name, name)
                    if broad_mean is None:
                        lines.append(f"  {source}可比大盘：来源缺口，不能判断与该股是否相悖")
                        continue
                    change = item[field]
                    band = payload["neutral_band_percent"]
                    alignment = ("中性信号" if broad_mean == 0 else "价格落在中性带" if abs(change) <= band else
                                 "方向相符" if (broad_mean > 0) == (change > 0) else "方向相悖")
                    lines.append(f"  {source}可比大盘：{broad_mean:+.2f}（{entry['broad_count']}篇）→与该股价格{alignment}；市场背景对照，非个股命中")
            lines.append("")
        context = payload.get("market_context")
        if context:
            lines.append("冻结五日全帖情绪（混合范围和期限，仅作背景，不作逐股方向判断）：")
            for name, entry in context.items():
                source = {"duoyu": "多鱼", "dgf": "大锅饭", "brucegold": "布鲁斯（字幕/未经人工复核）"}.get(name, name)
                mean = entry["all_mean"]
                lines.append(f"- {source}：{mean:+.2f}（{entry['all_count']}篇）" if mean is not None else f"- {source}：无样本")
            lines.append("同一大盘信号用于多只持仓是相关样本，不计作多次独立验证；旧档无冻结市场背景时不读取后来的 HTML 回填。")
            lines.append("")
        mean = payload['aggregate']['signed_score_mean']
        lines.extend([f"平均评分：{mean:+.4f}" if mean is not None else "平均评分：无可评分决策", ""])
        version = payload.get("evaluation_version", payload["scoring_version"])
        if version in (3, 4):
            aggregate = payload["aggregate"]
            direction, hold = aggregate["direction"], aggregate["hold"]
            lines.append(f"决策效用评分（非实际盈亏或横盘预测）：总计 {aggregate['count']} 项，均分 {mean:+.4f}；方向 {direction['count']} 项，均分 {direction['signed_score_mean']:+.4f}" if direction['count'] else f"决策效用评分（非实际盈亏或横盘预测）：总计 {aggregate['count']} 项，均分 {mean:+.4f}；方向 0 项，均分不可用")
            lines.append(f"维持仓位 {hold['count']} 项，均分 {hold['signed_score_mean']:+.4f}" if hold['count'] else "维持仓位 0 项，均分不可用")
            lines.append("维持仓位诊断：" + "；".join(f"{label} {hold['outcome_counts'][label]} 项" for label in ("no_change_reasonable", "missed_add", "missed_reduce")))
            for code, item in payload["individual"].items():
                if item["action"] == "hold":
                    lines.append(f"{code}：{item['outcome_label']} — {item['feedback']}")
            lines.append("")
        if version in (2, 3, 4):
            if version in (3, 4):
                coverage = aggregate["coverage"]
                lines.append(f"评分覆盖：{coverage['scored']}/{coverage['total']}；弃权：{coverage['abstained']}；方向判断：{direction['count']}；维持仓位：{hold['count']}")
            else:
                coverage = payload["aggregate"]["coverage"]
                lines.append(f"评分覆盖：{coverage['scored']}/{coverage['total']}；弃权：{coverage['abstained']}；方向判断：{coverage['direction']['scored']}；明确横盘：{coverage['sideways']['scored']}")
            for group in payload["aggregate"]["correlated_board_scopes"]:
                lines.append(f"共同板块线索（非独立样本）：{group['scope']} — {', '.join(group['codes'])}")
            baseline_volume = payload["baseline_turnover"]
            lines.append(f"基准日 {payload['baseline_session']} 成交额：{baseline_volume['total_amount_100m_cny']} 亿元" if baseline_volume and baseline_volume.get("status") == "available" else "基准日成交额：不可用")
            lines.append("目标日成交额（结算后诊断，不参与评分）：")
            for day, snapshot in payload["target_turnover"]["sessions"].items():
                if snapshot["status"] != "available":
                    lines.append(f"- {day}：不可用（{snapshot.get('reason', '未提供原因')}）")
                    continue
                change = (f"；较基准日 {((float(snapshot['total_amount_100m_cny']) / float(baseline_volume['total_amount_100m_cny'])) - 1) * 100:+.2f}%"
                          if baseline_volume and baseline_volume.get("status") == "available" and float(baseline_volume["total_amount_100m_cny"]) > 0 else "")
                lines.append(f"- {day}：{snapshot['total_amount_100m_cny']} 亿元{change}")
            lines.append("")
    text = "\n".join(lines) + RECORD_START + json.dumps(payload, ensure_ascii=False, indent=2) + RECORD_END
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def score(action: str, return_percent: float, band: float = NEUTRAL_BAND_PERCENT) -> int:
    """Signed [-2,2]: action alignment, 0 for directional moves in neutral band.

    add/reduce: 0 if |r|<=band; otherwise sign(action*r) times
    (1 if |r|<=2*band else 2). hold: +2 inside band,
    +1 up to 2*band, -1 up to 4*band, -2 beyond.
    """
    magnitude = abs(return_percent)
    if action == "hold":
        return 2 if magnitude <= band else 1 if magnitude <= 2 * band else -1 if magnitude <= 4 * band else -2
    if action not in ("add", "reduce"):
        raise ValueError(f"Invalid action: {action}")
    if magnitude <= band:
        return 0
    direction = 1 if (return_percent > 0) == (action == "add") else -1
    return direction * (1 if magnitude <= 2 * band else 2)

def hold_decision_score(return_percent: float, band: float = NEUTRAL_BAND_PERCENT,
                        version: int = 3) -> int:
    """Symmetric opportunity-cost proxy; v3 remains immutable for old settlements."""
    magnitude = abs(return_percent)
    severe = 4 * band if version == 3 else 8 * band if version == 4 else None
    if severe is None:
        raise ValueError(f"Unsupported hold scoring version: {version}")
    return 1 if magnitude <= band else 0 if magnitude <= 2 * band else -1 if magnitude <= severe else -2


def freeze(duoyu: Path, dgf: Path, decisions: Path, hold: Path,
           output: Path, now: datetime | None = None, boards: Path | None = None,
           target_session: date | None = None, trust_preopen_source: bool = False,
           brucegold: Path | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("Freeze timestamp must include a timezone")
    now = now.astimezone(SHANGHAI)
    late = time(9, 30) <= now.time() < time(15, 5)
    source_cutoff = datetime.combine(now.date(), time(9, 30), SHANGHAI) if late else now
    if now.time() >= time(15, 5) or late:
        target = target_session or next_session(now.date(), require_open_today=late)
    else:
        target = target_session or next_session(now.date() - timedelta(days=1))
    if target < now.date() or (late and target == now.date()):
        raise ValueError("Target session is already open or past")
    expected_name = f"{now:%m%d}_{target:%Y-%m-%d}_{now:%H%M}_prediction.md"
    if output.name != expected_name:
        raise ValueError(f"Prediction filename must be {expected_name}")
    existing = [(path, load_record(path, "prediction")) for path in output.parent.glob(f"*_{target:%Y-%m-%d}_*_prediction.md")]
    existing = [(path, data) for path, data in existing if data.get("provenance", "frozen") != "intraday_archived_unfrozen"]
    previous = None
    superseded_feedback = []
    if existing:
        if len(existing) != 1:
            raise ValueError(f"Multiple predictions found for target {target}")
        previous = existing[0]
        issued = datetime.fromisoformat(previous[1]["frozen_at"])
        if issued >= now or previous[0] == output:
            raise ValueError("Replacement must follow the existing forecast in a new minute")
        for fb_path in output.parent.glob("*_feedback.md"):
            fb = load_record(fb_path, "feedback")
            if fb.get("forecast_path") == str(previous[0].resolve()):
                if len(fb.get("target_sessions", [])) >= 2:
                    raise ValueError("Cannot replace a fully evaluated forecast (2 sessions settled)")
                superseded_feedback.append(fb_path)
    codes = read_hold(hold)
    picks = json.loads(decisions.read_text(encoding="utf-8"))
    if not isinstance(picks, dict) or set(picks) != set(codes):
        raise ValueError("One explicit decision required for every held code, no extras")
    for code, item in picks.items():
        if not isinstance(item, dict) or item.get("action") not in ("add", "reduce", "hold") or not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ValueError(f"{code}: action and nonempty evidence required")
        if item.get("hold_intent") not in (None, "sideways") or ("hold_intent" in item and item["action"] != "hold"):
            raise ValueError(f"{code}: hold_intent must be sideways on a hold decision")
        cited_ids = set(re.findall(r"\b\d{17,20}\b", item["evidence"]))
        tracked_ids = {ref.get("post_id") for ref in item.get("post_refs", []) if isinstance(ref, dict)}
        if cited_ids - tracked_ids:
            raise ValueError(f"{code}: cited post IDs require matching post_refs")
    if boards is None and any(item.get("board_evidence") for item in picks.values()):
        raise ValueError("Board evidence requires a current --boards snapshot")
    if late and boards is not None:
        board_stat = boards.stat()
        if (max(board_stat.st_mtime, board_stat.st_ctime, getattr(board_stat, "st_birthtime", float("inf")))
                > source_cutoff.timestamp()):
            raise ValueError("Intraday board snapshot was not preserved before 09:30")
    board_data = json.loads(boards.read_text(encoding="utf-8")) if boards else None
    source_map = {"duoyu": duoyu, "dgf": dgf, **({"brucegold": brucegold} if brucegold else {})}
    citations = board_citations(picks, board_data, source_map, source_cutoff) if board_data else {}
    # Date range must be an actually complete five-day analysis period; never repurpose
    # today's final content to manufacture an old prediction.
    spans = [re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.html$", path.name) for path in (duoyu, dgf)]
    if not all(spans) or spans[0].groups() != spans[1].groups():
        raise ValueError("Final HTML periods must match")
    first, last = (date.fromisoformat(part) for part in spans[0].groups())
    if (last - first).days != 4 or last not in ((now.date(), now.date() - timedelta(days=1)) if late else (now.date(),)):
        raise ValueError("Final HTML must cover the current five-day issue or a preopen prior-day issue")
    from rolling_forecast import cards
    for path in (duoyu, dgf) + ((brucegold,) if brucegold else ()):
        if late and not trust_preopen_source:
            stat = path.stat()
            if (max(stat.st_mtime, stat.st_ctime, getattr(stat, "st_birthtime", float("inf")))
                    > source_cutoff.timestamp()):
                raise ValueError(f"{path}: final HTML was not preserved before 09:30")
        for stamp, _ in cards(path).values():
            post_time = stamp.replace(tzinfo=SHANGHAI)
            if (late and post_time >= source_cutoff) or (not late and post_time > now):
                raise ValueError(f"{path}: post timestamp after source cutoff")
    post_refs = post_citations(picks, source_map, source_cutoff)
    # efinance history confirms the previous completed session, never weekday arithmetic.
    history = prices(codes[0], now.date() - timedelta(days=45), now.date())
    observed = [day for day in history if complete(day, now)]
    if not observed:
        raise ValueError("No confirmed preceding complete market session")
    prior = max(observed)
    if not complete(prior, now):
        raise ValueError("Baseline market close not yet complete")
    baselines = {}
    for code in codes:
        quotes = history if code == codes[0] else prices(code, now.date() - timedelta(days=45), now.date())
        eligible = [day for day in quotes if day <= prior and complete(day, now)]
        if not eligible or max(eligible) != prior or quotes[prior][0] <= 0:
            raise ValueError(f"{code}: missing immediately preceding completed close")
        baselines[code] = {"date": prior.isoformat(), "close": quotes[prior][0]}
    if previous and set(previous[1].get("decisions", {})) != set(codes):
        raise ValueError("Preopen revision must retain holdings code set")
    preceding = max((day for day in history if day < prior and complete(day, now)), default=None)
    baseline_turnover = turnover_snapshot(prior)
    preceding_turnover = turnover_snapshot(preceding) if preceding is not None else None
    turnover_change = None
    if (preceding_turnover and preceding_turnover.get("status") == "available"
            and baseline_turnover.get("status") == "available"
            and float(preceding_turnover["total_amount_100m_cny"]) > 0):
        turnover_change = round((float(baseline_turnover["total_amount_100m_cny"]) /
                                 float(preceding_turnover["total_amount_100m_cny"]) - 1) * 100, 4)
    payload = {
        "record_type": "prediction", "schema_version": 2, "frozen_at": now.isoformat(), "cutoff": now.isoformat(),
        "provenance": "late_preopen_snapshot" if late else "frozen",
        "source_cutoff": source_cutoff.isoformat(),
        **({"preopen_source_mtime_trusted": True} if late and trust_preopen_source else {}),
        "first_target_session": target.isoformat(),
        "source_html": {label: {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                        for label, path in (("duoyu", duoyu), ("dgf", dgf)) + ((("brucegold", brucegold),) if brucegold else ())},
        "market_context": market_context(source_map, source_cutoff, late),
        "target_horizon": "next_two_observed_trading_sessions",
        "scoring": {"version": CURRENT_EVALUATION_VERSION, "neutral_band_percent": NEUTRAL_BAND_PERCENT,
                    "formula": "add/reduce: 0 within 0.5%, signed 1 up to 1%, signed 2 beyond; hold decision utility vs incremental exposure: +1 within 0.5%, 0 up to 1%, -1 up to 4%, -2 beyond; all holds scored regardless of hold_intent"},
        "baseline_session": prior.isoformat(), "turnover": baseline_turnover,
        "turnover_context": {"preceding_session": preceding.isoformat() if preceding else None,
                             "preceding_turnover": preceding_turnover, "change_percent": turnover_change},
        "turnover_captured_at": now.isoformat(),
        "board_snapshot": board_data,
        "decisions": {code: {"action": picks[code]["action"], "evidence": picks[code]["evidence"].strip(),
                              **({"hold_intent": "sideways"} if picks[code].get("hold_intent") == "sideways" else {}),
                              "post_refs": post_refs[code],
                              "board_evidence": citations.get(code, []), "baseline": baselines[code]} for code in codes},
    }
    save_new(output, payload)
    if previous:
        for fb_path in superseded_feedback:
            fb_path.unlink()
        previous[0].unlink()
    return payload

def feedback_path(forecast: Path, now: datetime) -> Path:
    return forecast.with_name(now.strftime("%m%d_%Y-%m-%d_") + forecast.stem + "_feedback.md")

def rescore_path(forecast: Path, now: datetime, version: int = CURRENT_EVALUATION_VERSION) -> Path:
    return forecast.with_name(now.strftime("%m%d_%Y-%m-%d_") + forecast.stem + f"_score-v{version}_feedback.md")


def rescore(forecast: Path, output: Path, now: datetime | None = None) -> dict:
    """Append one evaluation per forecast SHA and policy version; never replace settlement."""
    now = now or datetime.now(SHANGHAI)
    return _evaluate(forecast, output, now, CURRENT_EVALUATION_VERSION, rescoring=True)


def review(forecast: Path, output: Path, now: datetime | None = None) -> dict:
    """Settle once under the scoring policy frozen in the prediction."""
    now = now or datetime.now(SHANGHAI)
    return _evaluate(forecast, output, now, None, rescoring=False)


def _evaluate(forecast: Path, output: Path, now: datetime,
              evaluation_version: int | None, rescoring: bool) -> dict:
    if now.tzinfo is None:
        raise ValueError("Review timestamp must include a timezone")
    now = now.astimezone(SHANGHAI)
    expected = rescore_path(forecast, now, version=evaluation_version) if rescoring else feedback_path(forecast, now)
    if output != expected:
        raise ValueError(f"Feedback filename must be {expected.name}")
    if output.exists():
        output.unlink()  # allow same-day re-settlement with more data
    forecast_sha = hashlib.sha256(forecast.read_bytes()).hexdigest()
    prior_settlements = []
    for prior_feedback in forecast.parent.glob("*_feedback.md"):
        previous = load_record(prior_feedback, "feedback")
        if rescoring:
            if (previous.get("forecast_sha256") == forecast_sha
                    and previous.get("evaluation_version", previous.get("scoring_version", 1)) == evaluation_version):
                raise ValueError("Forecast already evaluated under this scoring version")
        elif previous["forecast_path"] == str(forecast.resolve()) and previous.get("evaluation_kind", "settlement") == "settlement":
            prior_settlements.append((prior_feedback, previous))
    frozen = load_record(forecast, "prediction")
    provenance = frozen.get("provenance", "frozen")
    if provenance == "intraday_archived_unfrozen":
        if (frozen.get("recorded_at") != frozen.get("cutoff") or frozen.get("source_html")
                or not frozen.get("first_target_session")):
            raise ValueError("Intraday archive requires real recorded time, first target and unavailable source snapshots")
    elif provenance == "late_preopen_snapshot":
        source_cutoff = datetime.fromisoformat(frozen["source_cutoff"])
        issued = datetime.fromisoformat(frozen["frozen_at"])
        if (source_cutoff.tzinfo is None or issued.tzinfo is None or source_cutoff >= issued
                or source_cutoff.astimezone(SHANGHAI).time() != time(9, 30)
                or frozen.get("cutoff") != frozen["frozen_at"] or len(frozen.get("source_html", {})) < 2):
            raise ValueError("Invalid late preopen snapshot provenance")
    elif provenance != "frozen":
        raise ValueError("Unsupported prediction provenance")
    horizon = frozen.get("target_horizon")
    expected_horizon = ("archived_intraday_same_session_close" if provenance == "intraday_archived_unfrozen"
                        else "next_two_observed_trading_sessions")
    source_version = frozen.get("scoring", {}).get("version")
    scoring_version = evaluation_version if rescoring else source_version
    # Dispatch by explicit policy version; future revisions add a branch here and
    # in the scoring/aggregate sections, without rewriting past evaluations.
    if (horizon != expected_horizon or source_version not in (1, 2, 3, 4) or scoring_version not in (1, 2, 3, 4)
            or (source_version in (2, 3, 4) and (frozen.get("schema_version") != 2 or provenance == "intraday_archived_unfrozen"))):
        raise ValueError("Unsupported prediction target horizon or scoring version")
    cutoff = datetime.fromisoformat(frozen["cutoff"])
    if cutoff.tzinfo is None or cutoff >= now:
        raise ValueError("Invalid prediction cutoff")
    baseline_day = date.fromisoformat(frozen["baseline_session"])
    if cutoff.astimezone(SHANGHAI).date() < baseline_day:
        raise ValueError("Prediction cutoff precedes baseline session")
    same_session = provenance == "intraday_archived_unfrozen"
    if same_session and date.fromisoformat(frozen["first_target_session"]) != cutoff.astimezone(SHANGHAI).date():
        raise ValueError("Intraday archive target must be its actual recorded session")
    results = {}
    target_days = None
    for code, pick in frozen["decisions"].items():
        quotes = prices(code, baseline_day, now.date())
        # Exclude today's intraday quote; determine sessions from observed prices.
        first_eligible = (date.fromisoformat(frozen["first_target_session"])
                          if provenance in ("intraday_archived_unfrozen", "late_preopen_snapshot")
                          else baseline_day + timedelta(days=1))
        actual_days = sorted(day for day in quotes if day >= first_eligible and complete(day, now))
        if baseline_day not in quotes or len(actual_days) < 1:
            raise ValueError(f"{code}: target session close not yet available")
        first = actual_days[0]
        if provenance in ("intraday_archived_unfrozen", "late_preopen_snapshot") and first != first_eligible:
            raise ValueError(f"{code}: first target session close not available")
        selected = [first] if same_session else actual_days[:2]
        if target_days is not None and target_days != selected:
            raise ValueError(f"{code}: observed target sessions differ between holdings")
        target_days = selected
        if not same_session and cutoff >= datetime.combine(first, time(9, 30), SHANGHAI):
            raise ValueError("Prediction cutoff is after first target open")
        baseline = float(pick["baseline"]["close"])
        if baseline <= 0 or quotes[baseline_day][0] != baseline:
            raise ValueError(f"{code}: baseline historical close disagrees with frozen close")
        ending = quotes[target_days[-1]][0]
        change = (ending / baseline - 1) * 100
        rating = (None if scoring_version == 2 and pick["action"] == "hold" and pick.get("hold_intent") != "sideways"
                  else hold_decision_score(change, frozen["scoring"]["neutral_band_percent"], scoring_version)
                  if scoring_version in (3, 4) and pick["action"] == "hold"
                  else score(pick["action"], change, frozen["scoring"]["neutral_band_percent"]))
        band = frozen["scoring"]["neutral_band_percent"]
        refs = pick.get("post_refs")
        attribution = []
        status = "available" if refs is not None else "legacy_unavailable"
        if refs is None:
            refs = []
            for citation in pick.get("board_evidence", []):
                stance = citation.get("post_stance", "")
                match = re.search(r"市场态度：([+-]?[0-2])\s*｜([^｜]+)｜([^—]+)", stance)
                if match and citation.get("post_id") and citation.get("post_time"):
                    refs.append({"source": citation["source"], "post_id": citation["post_id"],
                                 "post_time": citation["post_time"], "score": int(match[1]),
                                 "scope": match[2].strip(), "timescale": match[3].strip(),
                                 "board_relevance": citation.get("relevance", "unverified")})
            if refs:
                status = "partial_frozen_board_only"
        if isinstance(refs, list):
            for ref in refs:
                signed = ref["score"]
                alignment = ("无方向评分" if signed == 0 else "价格落在中性带" if abs(change) <= band else
                             "方向相符" if (signed > 0) == (change > 0) else "方向相悖")
                attribution.append({**ref, "alignment": alignment})
        results[code] = {"action": pick["action"], "evidence": pick["evidence"],
                         "baseline_close": baseline, ("target_close" if same_session else "second_close"): ending,
                         ("prior_close_to_session_close_return_percent" if same_session else
                          "baseline_to_second_target_return_percent" if provenance == "late_preopen_snapshot" else
                          "two_session_return_percent"): round(change, 4),
                         "signed_score": rating, "post_attribution": attribution,
                         "attribution_status": status}
        if scoring_version in (3, 4) and pick["action"] == "hold":
            band = frozen["scoring"]["neutral_band_percent"]
            label = ("no_change_reasonable" if abs(change) <= band else
                     "missed_add" if change > 0 else "missed_reduce")
            explanation = {"no_change_reasonable": "波动处于中性区间，维持仓位避免了不必要的交易。",
                           "missed_add": "上涨时未增加增量仓位；原有持仓仍受益，并非错过全部涨幅。",
                           "missed_reduce": "下跌时未降低增量敞口，未避免相应回撤。"}[label]
            results[code]["outcome_label"] = label
            results[code]["feedback"] = f"维持仓位相对调整增量敞口的机会成本代理评分（非实际盈亏、非横盘预测）：{explanation}"
            if "hold_intent" in pick:
                results[code]["hold_intent"] = pick["hold_intent"]
        if scoring_version == 2:
            results[code]["scoring_status"] = "abstained" if rating is None else "scored"
            results[code]["scoring_category"] = "sideways" if pick["action"] == "hold" else "direction"
            if pick.get("hold_intent") == "sideways":
                results[code]["hold_intent"] = "sideways"
    for prior_path, prior_record in prior_settlements:
        prior_sessions = len(prior_record.get("target_sessions", []))
        if prior_sessions >= len(target_days):
            raise ValueError(f"Prediction already settled with {prior_sessions} target sessions (≥ {len(target_days)} now available)")
        prior_path.unlink()  # Replace partial settlement with more complete one
    if scoring_version in (2, 3, 4):
        scored = [row["signed_score"] for row in results.values() if row["signed_score"] is not None]
        groups = {}
        for code, pick in frozen["decisions"].items():
            for scope in {cite["scope"].strip() for cite in pick.get("board_evidence", []) if isinstance(cite.get("scope"), str) and cite["scope"].strip()}:
                groups.setdefault(scope, []).append(code)
        correlated = [{"scope": scope, "codes": sorted(codes), "count": len(codes)}
                      for scope, codes in sorted(groups.items()) if len(codes) > 1]
        turnover = {"captured_at": now.isoformat(), "sessions": {}}
        for day in target_days:
            turnover["sessions"][day.isoformat()] = (turnover_snapshot(day) if TURNOVER_MODULE.is_file() else
                {"trade_date": day.isoformat(), "status": "unavailable", "reason": "Tushare turnover module missing"})
        if scoring_version == 2:
            coverage = {"total": len(results), "scored": len(scored), "abstained": len(results) - len(scored),
                        "direction": {"scored": sum(row["scoring_status"] == "scored" and row["scoring_category"] == "direction" for row in results.values()),
                                      "abstained": sum(row["scoring_status"] == "abstained" and row["scoring_category"] == "direction" for row in results.values())},
                        "sideways": {"scored": sum(row["scoring_status"] == "scored" and row["scoring_category"] == "sideways" for row in results.values()),
                                     "abstained": sum(row["scoring_status"] == "abstained" and row["scoring_category"] == "sideways" for row in results.values())}}
        else:
            coverage = {"total": len(results), "scored": len(results), "abstained": 0}
    if scoring_version in (3, 4):
        direction_scores = [row["signed_score"] for row in results.values() if row["action"] != "hold"]
        hold_scores = [row["signed_score"] for row in results.values() if row["action"] == "hold"]
        aggregate_v3 = {"count": len(scored), "signed_score_sum": sum(scored),
                        "signed_score_mean": round(sum(scored) / len(scored), 4) if scored else None,
                        "coverage": coverage, "correlated_board_scopes": correlated,
                        "direction": {"count": len(direction_scores), "signed_score_sum": sum(direction_scores),
                                      "signed_score_mean": round(sum(direction_scores) / len(direction_scores), 4) if direction_scores else None},
                        "hold": {"count": len(hold_scores), "signed_score_sum": sum(hold_scores),
                                 "signed_score_mean": round(sum(hold_scores) / len(hold_scores), 4) if hold_scores else None,
                                 "outcome_counts": {label: sum(row.get("outcome_label") == label for row in results.values())
                                                    for label in ("no_change_reasonable", "missed_add", "missed_reduce")}}}
    payload = {"record_type": "feedback", "schema_version": 2, "reviewed_at": now.isoformat(),
               "forecast_path": str(forecast.resolve()),
               "forecast_sha256": forecast_sha,
               "provenance": provenance, "evaluation_kind": "rescore" if rescoring else "settlement",
               "source_scoring_version": source_version, "evaluation_version": scoring_version,
               "scoring_version": scoring_version,
               "market_context": frozen.get("market_context"),
               "neutral_band_percent": frozen["scoring"]["neutral_band_percent"],
               "target_sessions": [day.isoformat() for day in target_days],
               "baseline_session": baseline_day.isoformat(),
               "return_window": ("includes_pre_archive_session_move" if same_session else
                                 "includes_issue_session" if provenance == "late_preopen_snapshot" else
                                 "partial_one_of_two_sessions" if len(target_days) < 2 else
                                 "two_observed_target_sessions"),
               "score_formula": ("band=0.5%: add/reduce directional; hold decision utility: +1 within band, 0 up to 1%, -1 up to 4%, -2 beyond; all holds scored; not realized P&L" if scoring_version == 4 else "band=0.5%: add/reduce directional; hold decision utility vs incremental exposure change: +1 within band, 0 up to 1%, -1 up to 2%, -2 beyond; all holds scored; not realized P&L or sideways forecast" if scoring_version == 3 else "band=0.5%: add/reduce directional; hold abstains unless hold_intent=sideways, then use legacy hold bands" if scoring_version == 2 else "band=0.5%: add/reduce 0 within band, +/-1 up to 1%, +/-2 beyond; hold +2 within band, +1 up to 1%, -1 up to 2%, -2 beyond"),
               "individual": results,
               "aggregate": ({"count": len(results), "signed_score_sum": sum(scored),
                              "signed_score_mean": round(sum(scored) / len(scored), 4) if scored else None,
                              "coverage": coverage, "correlated_board_scopes": correlated} if scoring_version == 2 else
                             aggregate_v3 if scoring_version in (3, 4) else
                             {"count": len(results), "signed_score_sum": sum(row["signed_score"] for row in results.values()),
                              "signed_score_mean": round(sum(row["signed_score"] for row in results.values()) / len(results), 4)})}
    if scoring_version in (2, 3, 4):
        payload["target_turnover"] = turnover
        payload["baseline_turnover"] = frozen.get("turnover")
        payload["turnover_context"] = frozen.get("turnover_context")
    save_new(output, payload)
    return payload

def archive_status(archive: Path, day: date) -> dict:
    """Read archived records without quotes, scoring, or writes."""
    predictions = [(path, load_record(path, "prediction")) for path in archive.glob("*_prediction.md")]
    feedback = [(path, load_record(path, "feedback")) for path in archive.glob("*_feedback.md")]
    predictions.sort(key=lambda row: datetime.fromisoformat(
        row[1]["recorded_at"] if row[1].get("provenance") == "intraday_archived_unfrozen"
        else row[1]["frozen_at"]), reverse=True)
    today = [(path, data) for path, data in predictions if data.get("provenance") != "intraday_archived_unfrozen"
             and datetime.fromisoformat(data["frozen_at"]).astimezone(SHANGHAI).date() == day]
    feedback.sort(key=lambda row: (datetime.fromisoformat(row[1]["reviewed_at"]), row[0].name), reverse=True)
    reviewed = {data["forecast_path"]: (path, data) for path, data in reversed(feedback)}
    settled = {data["forecast_path"] for _, data in feedback if data.get("evaluation_kind", "settlement") == "settlement"}
    latest = predictions[0] if predictions else None
    latest_feedback = reviewed.get(str(latest[0].resolve())) if latest else None
    if latest_feedback and latest_feedback[1]["forecast_sha256"] != hashlib.sha256(latest[0].read_bytes()).hexdigest():
        raise ValueError("Latest feedback does not match frozen forecast")
    return {"today": today[0] if today else None, "latest": latest,
            "latest_feedback": latest_feedback,
            "pending": [path for path, _ in predictions if str(path.resolve()) not in settled],
            "feedback": feedback}


def review_pending(archive: Path, now: datetime | None = None) -> list[tuple[Path, str]]:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("Review timestamp must include a timezone")
    now = now.astimezone(SHANGHAI)
    outcomes = []
    for forecast in sorted(archive.glob("*_prediction.md")):
        try:
            review(forecast, feedback_path(forecast, now), now)
        except ValueError as exc:
            msg = str(exc)
            if "already settled" in msg:
                continue  # fully-settled predictions are not pending; skip silently
            if "target session close not yet available" in msg or "无行情数据" in msg:
                outcomes.append((forecast, f"pending: {msg}"))
            else:
                raise
        else:
            outcomes.append((forecast, "settled"))
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("freeze")
    make.add_argument("duoyu", type=Path)
    make.add_argument("dgf", type=Path)
    make.add_argument("--brucegold", type=Path, default=None, help="BruceGold 字幕汇总 HTML（可选第三源）")
    make.add_argument("--decisions", type=Path, required=True)
    make.add_argument("--hold", type=Path, default=Path(__file__).with_name("hold"))
    make.add_argument("--boards", type=Path, help="Current efinance board snapshot for cited board evidence")
    make.add_argument("--archive", type=Path, default=ARCHIVE)
    make.add_argument("--target-date", type=date.fromisoformat,
                      help="Explicit target session YYYY-MM-DD when the exchange calendar API is unavailable")
    make.add_argument("--trust-preopen-source", action="store_true",
                      help="Late mode only: skip the source file-mtime gate after verifying all posts predate 09:30; post-time content check still enforced and the override is recorded")
    settle = sub.add_parser("review")
    settle.add_argument("forecast", type=Path)
    settle.add_argument("--archive", type=Path, default=ARCHIVE)
    reevaluate = sub.add_parser("rescore", help="Append current-policy evaluation without altering settlement")
    reevaluate.add_argument("forecast", type=Path)
    reevaluate.add_argument("--archive", type=Path, default=ARCHIVE)
    pending = sub.add_parser("review-pending")
    pending.add_argument("--archive", type=Path, default=ARCHIVE)
    status = sub.add_parser("status", help="Read frozen predictions and feedback without settlement")
    status.add_argument("--archive", type=Path, default=ARCHIVE)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            now = datetime.now(SHANGHAI)
            args.archive.mkdir(parents=True, exist_ok=True)
            target = (args.target_date if args.target_date else
                      next_session(now.date() - timedelta(days=1) if now.time() < time(9, 30) else now.date(),
                                   require_open_today=time(9, 30) <= now.time() < time(15, 5)))
            output = args.archive / f"{now:%m%d}_{target:%Y-%m-%d}_{now:%H%M}_prediction.md"
            payload = freeze(args.duoyu, args.dgf, args.decisions, args.hold, output, now=now,
                             boards=args.boards, target_session=target,
                             trust_preopen_source=args.trust_preopen_source,
                             brucegold=args.brucegold)
            print(f"Frozen {len(payload['decisions'])} decisions: {output}")
        elif args.command == "status":
            state = archive_status(args.archive, datetime.now(SHANGHAI).date())
            if state["latest"]:
                forecast, frozen = state["latest"]
                provenance = frozen.get("provenance", "frozen")
                label = ("intraday archive (not frozen)" if provenance == "intraday_archived_unfrozen"
                         else "late issue (preopen inputs only)" if provenance == "late_preopen_snapshot"
                         else "created today" if state["today"] else "created earlier")
                created = (frozen["recorded_at"] if provenance == "intraday_archived_unfrozen"
                           else frozen["frozen_at"])
                print(f"Latest prediction [{label}]: {forecast} ({created})")
                print(f"  baseline {frozen['baseline_session']}; first target {frozen.get('first_target_session', 'observed sessions')}; horizon {frozen['target_horizon']}")
                for code, pick in frozen["decisions"].items():
                    print(f"  {code} {pick['action']}: {pick['evidence']}")
                if state["latest_feedback"]:
                    path, settled = state["latest_feedback"]
                    print(f"  latest evaluation: {path}; sessions {', '.join(settled['target_sessions'])}")
                else:
                    print("  pending settlement")
            else:
                print("Latest frozen prediction: none")
            print(f"Pending forecasts: {len(state['pending'])}")
            for forecast in state["pending"]:
                print(f"  {forecast}")
            print(f"Archived feedback: {len(state['feedback'])}")
            if state["feedback"]:
                feedback, record = state["feedback"][0]
                mean = record["aggregate"]["signed_score_mean"]
                score_text = f"{mean:+.4f}" if mean is not None else "unavailable (all abstained)"
                print(f"Latest feedback [{record.get('provenance', 'frozen')}, evaluation v{record.get('evaluation_version', record['scoring_version'])}]: {feedback} ({record['reviewed_at']}); mean score {score_text}")
        elif args.command == "review-pending":
            for forecast, status in review_pending(args.archive):
                print(f"{forecast.name}: {status}")
        else:
            now = datetime.now(SHANGHAI)
            output = args.archive / (rescore_path(args.forecast, now) if args.command == "rescore" else feedback_path(args.forecast, now)).name
            payload = (rescore if args.command == "rescore" else review)(args.forecast, output, now)
            for code, result in payload["individual"].items():
                field = ("prior_close_to_session_close_return_percent" if payload["provenance"] == "intraday_archived_unfrozen" else
                         "baseline_to_second_target_return_percent" if payload["provenance"] == "late_preopen_snapshot" else
                         "two_session_return_percent")
                rating = "abstained" if result["signed_score"] is None else f"{result['signed_score']:+d}"
                print(f"{code} {result['action']}: {result[field]:+.2f}% score {rating}")
            print(f"Aggregate (evaluation v{payload['evaluation_version']}): {payload['aggregate']} -> {output}")
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
