"""Focused offline tests for point-in-time short-term forecasts."""
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import board_snapshot
import code_info
import plan_feedback as plan

TZ = ZoneInfo("Asia/Shanghai")


def at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def setup_inputs(root: Path):
    duoyu = root / "duoyu_2026-11-12_2026-11-16.html"
    dgf = root / "dgf_2026-11-12_2026-11-16.html"
    card = '<article class="post-card" data-post-id="p1" data-ts="2026-11-13 14:00"><p class="market-stance" data-source="analysis"><strong>市场态度：+1</strong></p></article>'
    duoyu.write_text(card)
    dgf.write_text(card)
    hold = root / "hold"
    hold.write_text("159583 # ETF\n")
    decisions = root / "decisions.json"
    decisions.write_text(json.dumps({"159583": {"action": "add", "evidence": "11-13 post p1 +1 sector"}}))
    return duoyu, dgf, decisions, hold

@contextmanager
def preserved_before_open(*files: Path):
    """Model preopen file birth/creation times independently of test runtime."""
    original_stat = Path.stat
    def snapshot_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path not in files:
            return result
        return SimpleNamespace(st_mtime=result.st_mtime, st_ctime=at(16).timestamp(),
                               st_birthtime=at(16).timestamp())
    with patch.object(Path, "stat", snapshot_stat):
        yield


def test_freeze_immutable_snapshot_and_holiday_review():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        feedback = plan.feedback_path(forecast, at(18, 15, 5))
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 17): (101., 1.), date(2026, 11, 18): (102., 0.99)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
            assert frozen["baseline_session"] == "2026-11-13"
            assert frozen["target_horizon"] == "next_two_observed_trading_sessions"
            assert "target_sessions" not in frozen
            assert plan.load_record(forecast, "prediction") == frozen
            original = forecast.read_bytes()
            decisions.write_text(json.dumps({"159583": {"action": "add", "evidence": "11-13 post p1 +1 sector"}}))
            second = root / "1116_2026-11-16_0910_prediction.md"
            plan.freeze(duoyu, dgf, decisions, hold, second, at(16, 9, 10))
            assert not forecast.exists()
            second_bytes = second.read_bytes()
            fb2 = plan.feedback_path(second, at(18, 14))
            partial = plan.review(second, fb2, at(18, 14))
            assert partial["target_sessions"] == ["2026-11-17"]
            assert partial["return_window"] == "partial_one_of_two_sessions"
            reviewed = plan.review(second, fb2, at(18, 15, 5))
            assert reviewed["target_sessions"] == ["2026-11-17", "2026-11-18"]
            assert reviewed["individual"]["159583"]["signed_score"] == 2
            assert reviewed["aggregate"]["signed_score_sum"] == 2
            assert second.read_bytes() == second_bytes
            with pytest.raises(ValueError, match="already settled"):
                plan.review(second, plan.feedback_path(second, at(19, 15, 5)), at(19, 15, 5))


def test_reject_current_market_and_future_posts():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value={date(2026, 11, 13): (100., 0.)}), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            forecast = root / "1116_2026-11-16_0900_prediction.md"
            with pytest.raises(ValueError, match="already open or past"):
                plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16, 9, 54))
            duoyu.write_text(duoyu.read_text().replace("2026-11-13 14:00", "2026-11-17 14:00"))
            with pytest.raises(ValueError, match="post timestamp after source cutoff"):
                plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))


def test_postclose_freezes_next_session_once():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        history = {date(2026, 11, 16): (100., 0.)}
        forecast = root / "1116_2026-11-17_1600_prediction.md"
        archived = root / "1115_2026-11-16_1355_prediction.md"
        archived_record = {"record_type": "prediction", "provenance": "intraday_archived_unfrozen",
                           "frozen_at": "2026-11-15T22:51:36.513154+08:00",
                           "frozen_at_origin": "synthetic_test_only",
                           "recorded_at": at(16, 13, 55).isoformat(), "first_target_session": "2026-11-17"}
        archived.write_text("# Archived intraday draft\n\n" + plan.RECORD_START
                            + json.dumps(archived_record) + plan.RECORD_END)
        archived_bytes = archived.read_bytes()
        with patch.object(plan, "next_session", return_value=date(2026, 11, 17)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16, 16))
            assert frozen["first_target_session"] == "2026-11-17"
            assert frozen["baseline_session"] == "2026-11-16"
            assert archived.read_bytes() == archived_bytes
            morning_duoyu = root / "duoyu_2026-11-13_2026-11-17.html"
            morning_dgf = root / "dgf_2026-11-13_2026-11-17.html"
            morning_duoyu.write_text(duoyu.read_text())
            morning_dgf.write_text(dgf.read_text())
            revision = root / "1117_2026-11-17_0900_prediction.md"
            updated = plan.freeze(morning_duoyu, morning_dgf, decisions, hold, revision, at(17), target_session=date(2026, 11, 17))
            assert updated["first_target_session"] == "2026-11-17"
            assert not forecast.exists()
            assert archived.read_bytes() == archived_bytes
            assert plan.archive_status(root, date(2026, 11, 17))["pending"] == [revision, archived]
            assert [path for path in root.glob("*_2026-11-17_*_prediction.md")
                    if path != archived] == [revision]
            updated_bytes = revision.read_bytes()
            later = root / "1117_2026-11-17_0910_prediction.md"
            plan.freeze(morning_duoyu, morning_dgf, decisions, hold, later, at(17, 9, 10), target_session=date(2026, 11, 17))
            assert not revision.exists() and later.exists()
            assert plan.archive_status(root, date(2026, 11, 17))["pending"] == [later, archived]
            assert plan.load_record(later, "prediction")["first_target_session"] == "2026-11-17"
            assert archived.read_bytes() == archived_bytes
            assert later.read_bytes()


def test_preopen_revision_only_latest_version_settles():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        old = root / "1116_2026-11-16_0900_prediction.md"
        latest = root / "1116_2026-11-16_0910_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (98., 0.),
                   date(2026, 11, 17): (97., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            plan.freeze(duoyu, dgf, decisions, hold, old, at(16))
            assert old.exists()
            decisions.write_text(json.dumps({"159583": {"action": "reduce", "evidence": "New preopen source changes direction"}}))
            plan.freeze(duoyu, dgf, decisions, hold, latest, at(16, 9, 10))
            assert not old.exists()
            assert plan.archive_status(root, date(2026, 11, 16))["latest"][0] == latest
            assert plan.archive_status(root, date(2026, 11, 16))["pending"] == [latest]
            assert plan.review_pending(root, at(17, 15, 5)) == [(latest, "settled")]
        feedback = plan.load_record(plan.feedback_path(latest, at(17, 15, 5)), "feedback")
        assert feedback["individual"]["159583"]["action"] == "reduce"
        assert feedback["forecast_sha256"] == hashlib.sha256(latest.read_bytes()).hexdigest()
        assert not old.exists()
        assert not plan.feedback_path(old, at(17, 15, 5)).exists()


def test_intraday_issue_uses_only_preopen_files_and_next_full_sessions():
    import os
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        history = {date(2026, 11, 16): (100., 0.), date(2026, 11, 17): (80., 0.),
                   date(2026, 11, 18): (98., 0.), date(2026, 11, 19): (97., 0.)}
        output = root / "1117_2026-11-18_1000_prediction.md"
        for source in (duoyu, dgf):
            os.utime(source, (at(16).timestamp(), at(16).timestamp()))
        with preserved_before_open(duoyu, dgf), patch.object(plan, "next_session", return_value=date(2026, 11, 18)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, output, at(17, 10))
            assert frozen["provenance"] == "late_preopen_snapshot"
            assert frozen["source_cutoff"] == at(17, 9, 30).isoformat()
            assert frozen["first_target_session"] == "2026-11-18"
            assert frozen["baseline_session"] == "2026-11-16"
            later = root / "1117_2026-11-18_1100_prediction.md"
            plan.freeze(duoyu, dgf, decisions, hold, later, at(17, 11))
            assert not output.exists()
            feedback = plan.review(later, plan.feedback_path(later, at(19, 15, 5)), at(19, 15, 5))
            assert feedback["target_sessions"] == ["2026-11-18", "2026-11-19"]
            assert feedback["return_window"] == "includes_issue_session"
            assert feedback["individual"]["159583"]["baseline_to_second_target_return_percent"] == -3.0
            assert "含出具当日" in plan.feedback_path(later, at(19, 15, 5)).read_text()


def test_intraday_issue_rejects_updated_sources_and_open_posts():
    import os
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        output = root / "1117_2026-11-18_1000_prediction.md"
        os.utime(dgf, (at(16).timestamp(), at(16).timestamp()))
        with preserved_before_open(duoyu, dgf), patch.object(plan, "next_session", return_value=date(2026, 11, 18)):
            os.utime(duoyu, (at(17, 9, 31).timestamp(), at(17, 9, 31).timestamp()))
            with pytest.raises(ValueError, match="not preserved before 09:30"):
                plan.freeze(duoyu, dgf, decisions, hold, output, at(17, 10))
            duoyu.write_text(duoyu.read_text().replace("2026-11-13 14:00", "2026-11-17 09:30"))
            os.utime(duoyu, (at(16).timestamp(), at(16).timestamp()))
            with pytest.raises(ValueError, match="post timestamp after source cutoff"):
                plan.freeze(duoyu, dgf, decisions, hold, output, at(17, 10))
        assert not output.exists()


def test_intraday_archive_scores_same_session_only_after_close():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        forecast = root / "1115_2026-11-16_1355_prediction.md"
        record = {"record_type": "prediction", "schema_version": 2,
                  "provenance": "intraday_archived_unfrozen",
                  "frozen_at": "2026-11-15T22:51:36.513154+08:00",
                  "frozen_at_origin": "synthetic_test_only",
                  "recorded_at": at(16, 13, 55).isoformat(),
                  "cutoff": at(16, 13, 55).isoformat(), "source_cutoff": None,
                  "baseline_session": "2026-11-13",
                  "first_target_session": "2026-11-16", "target_horizon": "archived_intraday_same_session_close",
                  "scoring": {"version": 1, "neutral_band_percent": 0.5},
                  "decisions": {"159583": {"action": "reduce", "evidence": "Original intraday decision",
                                            "baseline": {"date": "2026-11-13", "close": 100.}}}}
        forecast.write_text("# Original intraday archive\n\n" + plan.RECORD_START + json.dumps(record) + plan.RECORD_END)
        quotes = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (80., 0.),
                  date(2026, 11, 17): (99., 0.), date(2026, 11, 18): (98., 0.)}
        state = plan.archive_status(root, date(2026, 11, 16))
        assert record["frozen_at_origin"] == "synthetic_test_only" and record["source_cutoff"] is None
        assert plan.load_record(forecast, "prediction")["recorded_at"] == at(16, 13, 55).isoformat()
        assert state["latest"][0] == forecast and state["today"] is None
        assert plan.archive_status(root, date(2026, 11, 15))["today"] is None
        genuine = root / "1116_2026-11-17_1200_prediction.md"
        genuine_record = {"record_type": "prediction", "provenance": "frozen",
                          "frozen_at": at(16, 12).isoformat()}
        genuine.write_text("# Genuine prediction\n\n" + plan.RECORD_START
                           + json.dumps(genuine_record) + plan.RECORD_END)
        state = plan.archive_status(root, date(2026, 11, 16))
        assert state["latest"][0] == forecast
        assert state["today"][0] == genuine
        assert state["pending"] == [forecast, genuine]
        genuine.unlink()
        with patch.object(plan, "prices", return_value=quotes):
            assert plan.review_pending(root, at(16, 14))[0][1].startswith("pending:")
            assert not list(root.glob("*_feedback.md"))
            settled = plan.review(forecast, plan.feedback_path(forecast, at(17, 0, 20)), at(17, 0, 20))
        assert settled["target_sessions"] == ["2026-11-16"]
        assert settled["return_window"] == "includes_pre_archive_session_move"
        assert settled["individual"]["159583"]["prior_close_to_session_close_return_percent"] == -20.0
        assert settled["individual"]["159583"]["signed_score"] == 2
        assert "含留档前走势" in plan.feedback_path(forecast, at(17, 0, 20)).read_text()


def test_settlement_blocked_when_no_new_sessions_available():
    # A partial first-session feedback may be replaced after the second close.
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.),
                   date(2026, 11, 17): (102., 0.)}
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            plan.freeze( duoyu, dgf, decisions, hold, forecast, at(16))
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            plan.review(forecast, plan.feedback_path(forecast, at(17, 14)), at(17, 14))
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            full = plan.review(forecast, plan.feedback_path(forecast, at(18, 9)), at(18, 9))
            assert full["target_sessions"] == ["2026-11-16", "2026-11-17"]
            with pytest.raises(ValueError, match="already settled"):
                plan.review(forecast, plan.feedback_path(forecast, at(19, 9)), at(19, 9))


def test_v1_settlement_and_v2_rescore_preserve_archive_and_pending(capsys, monkeypatch):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        forecast = root / "1115_2026-11-16_1355_prediction.md"
        decisions = {f"00000{index}": {"action": "reduce" if index <= 3 else "hold",
                                         "evidence": "archived decision",
                                         "baseline": {"date": "2026-11-13", "close": 100.}}
                     for index in range(1, 8)}
        frozen = {"record_type": "prediction", "schema_version": 2,
                  "provenance": "intraday_archived_unfrozen",
                  "frozen_at": at(15, 22).isoformat(), "recorded_at": at(16, 13, 55).isoformat(),
                  "cutoff": at(16, 13, 55).isoformat(), "source_html": None,
                  "baseline_session": "2026-11-13", "first_target_session": "2026-11-16",
                  "target_horizon": "archived_intraday_same_session_close",
                  "scoring": {"version": 1, "neutral_band_percent": 0.5}, "decisions": decisions}
        forecast.write_text("# Archived\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        original = forecast.read_bytes()
        quotes = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (98., 0.),
                  date(2026, 11, 17): (97., 0.)}
        old_path = plan.feedback_path(forecast, at(17, 0, 20))
        new_path = plan.rescore_path(forecast, at(18, 15, 5), version=2)
        with patch.object(plan, "prices", return_value=quotes), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            old = plan.review(forecast, old_path, at(17, 0, 20))
            old_bytes = old_path.read_bytes()
            newer = plan._evaluate(forecast, new_path, at(18, 15, 5), 2, rescoring=True)
            assert newer["aggregate"]["coverage"]["scored"] == 3
            assert newer["aggregate"]["coverage"]["abstained"] == 4
            assert old["aggregate"]["signed_score_mean"] is not None
            assert newer["source_scoring_version"] == 1
            assert newer["evaluation_version"] == 2
        assert forecast.read_bytes() == original and old_path.read_bytes() == old_bytes
        assert "score-v2" in new_path.name
        assert "原预测记录评分版本：v1；本次评估政策版本：v2" in new_path.read_text()
        assert "含留档前走势" in new_path.read_text()
        state = plan.archive_status(root, date(2026, 11, 18))
        assert state["latest_feedback"][0] == new_path
        assert state["pending"] == []
        with patch.object(plan, "prices", return_value=quotes):
            assert plan.review_pending(root, at(19, 15, 5)) == []
        monkeypatch.setattr("sys.argv", ["plan_feedback.py", "status", "--archive", str(root)])
        plan.main()
        assert str(new_path) in capsys.readouterr().out


def test_rescore_does_not_count_as_initial_settlement(capsys, monkeypatch):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        frozen = {"record_type": "prediction", "schema_version": 2, "provenance": "frozen",
                  "frozen_at": at(16).isoformat(), "cutoff": at(16).isoformat(),
                  "baseline_session": "2026-11-13", "target_horizon": "next_two_observed_trading_sessions",
                  "scoring": {"version": 1, "neutral_band_percent": 0.5},
                  "decisions": {"159583": {"action": "hold", "evidence": "wait",
                                            "baseline": {"close": 100.}}}}
        forecast.write_text("# Archived\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        quotes = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.),
                  date(2026, 11, 17): (103., 0.)}
        with patch.object(plan, "prices", return_value=quotes), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            evaluation = plan.rescore(forecast, plan.rescore_path(forecast, at(18, 15, 5)), at(18, 15, 5))
            assert evaluation["aggregate"]["signed_score_mean"] == -1
            assert plan.archive_status(root, date(2026, 11, 18))["pending"] == [forecast]
            monkeypatch.setattr("sys.argv", ["plan_feedback.py", "status", "--archive", str(root)])
            plan.main()
            assert "mean score -1.0000" in capsys.readouterr().out
            original = plan.review(forecast, plan.feedback_path(forecast, at(19, 15, 5)), at(19, 15, 5))
        assert original["evaluation_version"] == 1
        assert original["individual"]["159583"]["signed_score"] == -2
        assert plan.archive_status(root, date(2026, 11, 19))["latest_feedback"][0] == plan.feedback_path(forecast, at(19, 15, 5))


def test_score_sign_and_hold_neutral_band():
    assert [plan.score("add", change) for change in (-2, -0.75, 0, 0.75, 2)] == [-2, -1, 0, 1, 2]
    assert [plan.score("reduce", change) for change in (-2, -0.75, 0, 0.75, 2)] == [2, 1, 0, -1, -2]
    assert [plan.score("hold", change) for change in (0, 0.5, 0.75, 1.5, 3)] == [2, 2, 1, -1, -2]

def test_v2_hold_abstention_sideways_coverage_and_board_correlation():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        hold.write_text("000001\n000002\n000003\n000004\n")
        decisions.write_text(json.dumps({
            "000001": {"action": "hold", "evidence": "risk control"},
            "000002": {"action": "hold", "hold_intent": "sideways", "evidence": "flat range"},
            "000003": {"action": "add", "evidence": "up", "board_evidence": [{"scope": "shared"}]},
            "000004": {"action": "reduce", "evidence": "down", "board_evidence": [{"scope": "shared"}]}}))
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.),
                   date(2026, 11, 17): (103., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            with pytest.raises(ValueError, match="Board evidence requires"):
                plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
            decisions_data = json.loads(decisions.read_text())
            for code in ("000003", "000004"):
                decisions_data[code].pop("board_evidence")
            decisions.write_text(json.dumps(decisions_data))
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert frozen["scoring"]["version"] == 4
        assert frozen["turnover_captured_at"] == at(16).isoformat()
        assert "hold_intent" not in frozen["decisions"]["000001"]
        assert frozen["decisions"]["000002"]["hold_intent"] == "sideways"
        frozen["scoring"]["version"] = 2
        frozen["decisions"]["000003"]["board_evidence"] = [{"scope": "shared"}, {"scope": "shared"}]
        frozen["decisions"]["000004"]["board_evidence"] = [{"scope": "shared"}]
        forecast.write_text("# Frozen\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            reviewed = plan.review(forecast, plan.feedback_path(forecast, at(17, 15, 5)), at(17, 15, 5))
        assert reviewed["individual"]["000001"]["signed_score"] is None
        assert reviewed["individual"]["000002"]["signed_score"] == -2
        assert reviewed["aggregate"]["coverage"] == {"total": 4, "scored": 3, "abstained": 1,
            "direction": {"scored": 2, "abstained": 0}, "sideways": {"scored": 1, "abstained": 1}}
        assert reviewed["aggregate"]["correlated_board_scopes"] == [{"scope": "shared", "codes": ["000003", "000004"], "count": 2}]
        assert reviewed["target_turnover"]["sessions"]["2026-11-16"]["status"] == "unavailable"
        assert reviewed["aggregate"]["signed_score_mean"] == -0.6667
        feedback_text = plan.feedback_path(forecast, at(17, 15, 5)).read_text()
        assert "评分覆盖：3/4；弃权：1" in feedback_text
        assert "目标日成交额（结算后诊断，不参与评分）" in feedback_text


def test_freeze_turnover_trend_uses_only_completed_sessions():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        history = {date(2026, 11, 12): (99., 0.), date(2026, 11, 13): (100., 0.),
                   date(2026, 11, 16): (101., 0.), date(2026, 11, 17): (102., 0.)}
        volumes = {date(2026, 11, 12): 10000., date(2026, 11, 13): 12000.,
                   date(2026, 11, 16): 15000., date(2026, 11, 17): 14000.}
        def volume(day):
            return {"status": "available", "trade_date": day.isoformat(), "total_amount_100m_cny": volumes[day]}
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", side_effect=volume) as queried:
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
            assert queried.call_count == 2
            assert frozen["turnover_context"]["preceding_session"] == "2026-11-12"
            assert frozen["turnover_context"]["change_percent"] == 20.0
            reviewed = plan.review(forecast, plan.feedback_path(forecast, at(17, 15, 5)), at(17, 15, 5))
        assert reviewed["target_turnover"]["sessions"]["2026-11-16"]["total_amount_100m_cny"] == 15000.
        assert "较基准日 +25.00%" in plan.feedback_path(forecast, at(17, 15, 5)).read_text()


def test_hold_only_v2_abstains_and_legacy_v1_hold_still_scores():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        decisions.write_text(json.dumps({"159583": {"action": "hold", "evidence": "wait"}}))
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.), date(2026, 11, 17): (103., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        frozen["scoring"]["version"] = 2
        forecast.write_text("# Legacy v2\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            reviewed = plan.review(forecast, plan.feedback_path(forecast, at(17, 15, 5)), at(17, 15, 5))
        assert reviewed["aggregate"]["signed_score_mean"] is None
        assert reviewed["aggregate"]["coverage"]["abstained"] == 1
        legacy = root / "1116_2026-11-16_0910_prediction.md"
        frozen["scoring"]["version"] = 1
        legacy.write_text("# Legacy\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", side_effect=AssertionError("legacy must not query turnover")):
            previous = plan.review(legacy, plan.feedback_path(legacy, at(18, 9)), at(18, 9))
        assert previous["individual"]["159583"]["signed_score"] == -2
        assert "coverage" not in previous["aggregate"]

def test_invalid_hold_intent_rejected_before_market_queries():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", side_effect=AssertionError("invalid intent must not query prices")):
            for action, intent in (("hold", "risk_control"), ("add", "sideways")):
                decisions.write_text(json.dumps({"159583": {"action": action, "evidence": "why", "hold_intent": intent}}))
                with pytest.raises(ValueError, match="hold_intent"):
                    plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert not forecast.exists()


def test_target_turnover_only_after_both_completed_closes():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.), date(2026, 11, 17): (102., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}) as captured:
            reviewed = plan.review(forecast, plan.feedback_path(forecast, at(17, 14)), at(17, 14))
            assert reviewed["target_sessions"] == ["2026-11-16"]
            assert reviewed["return_window"] == "partial_one_of_two_sessions"
            assert reviewed["individual"]["159583"]["signed_score"] == 2
            captured.assert_called_once_with(date(2026, 11, 16))
        assert plan.feedback_path(forecast, at(17, 14)).exists()


def test_code_info_filters_intraday(monkeypatch):
    import pandas as pd
    frame = pd.DataFrame({"日期": ["2026-11-13", "2026-11-16"], "股票名称": ["ETF", "ETF"],
                          "收盘": [100., 200.], "涨跌幅": [0., 100.]})
    class Clock:
        @staticmethod
        def now(_tz):
            return at(16, 9, 54)
    monkeypatch.setattr(code_info, "datetime", Clock)
    import efinance as ef
    monkeypatch.setattr(ef.stock, "get_quote_history", lambda _code: frame)
    assert "2026-11-13 收100" in code_info.resolve("159583")
    assert "2026-11-16" not in code_info.resolve("159583")


def test_board_snapshot_and_citations_do_not_conflate_subsectors(monkeypatch):
    import pandas as pd
    memberships = {"601869": ("BK1136", "光通信模块"), "300750": ("BK0989", "储能概念")}
    monkeypatch.setattr(board_snapshot.ef.stock, "get_belong_board", lambda code: pd.DataFrame([
        {"股票代码": code, "板块代码": memberships[code][0], "板块名称": memberships[code][1]}]))
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        hold.write_text("601869\n300750\n")
        card = ('<article class="post-card" data-post-id="p1" data-ts="2026-11-13 14:00">'
                '<p class="market-stance" data-source="analysis"><strong>市场态度：-1</strong>'
                '｜光通信｜未来两个交易日</p></article>')
        dgf.write_text(card)
        duoyu.write_text(card.replace("光通信", "大储能").replace("市场态度：-1", "市场态度：+1"))
        snapshot = board_snapshot.snapshot(["601869", "300750"], at(16, 8, 59))
        assert snapshot["stocks"]["601869"]["BK1136"] == "光通信模块"
        for code, source, scope in (("601869", "dgf", "光通信"), ("300750", "duoyu", "大储能")):
            cited = {"source": source, "post_id": "p1", "board_code": memberships[code][0],
                     "scope": scope, "rationale": "板块归属只作相关性证据"}
            decision = {"action": "hold", "evidence": "人工判断", "board_evidence": [cited]}
            current = json.loads(decisions.read_text()) if code == "300750" else {}
            current[code] = decision
            decisions.write_text(json.dumps(current, ensure_ascii=False))
        boards = root / "boards.json"
        boards.write_text(json.dumps(snapshot, ensure_ascii=False))
        history = {date(2026, 11, 13): (100., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, root / "1116_2026-11-16_0900_prediction.md", at(16), boards)
        assert frozen["decisions"]["601869"]["board_evidence"][0]["relevance"] == "related"
        assert frozen["decisions"]["300750"]["board_evidence"][0]["relevance"] == "unverified"
        assert frozen["board_snapshot"]["fetched_at"] == snapshot["fetched_at"]
        cited = json.loads(decisions.read_text())
        cited["601869"]["board_evidence"][0]["post_id"] = "nonexistent"
        decisions.write_text(json.dumps(cited, ensure_ascii=False))
        with pytest.raises(ValueError, match="board citation"):
            plan.board_citations(cited, snapshot, {"duoyu": duoyu, "dgf": dgf}, at(16))


def test_preopen_calendar_and_pending_feedback():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 17): (101., 1.),
                   date(2026, 11, 18): (102., 0.99)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 15)):
            with pytest.raises(ValueError, match="already open or past"):
                plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert not forecast.exists()
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
            original = forecast.read_bytes()
            with patch.object(plan, "prices", side_effect=AssertionError("status must not query quotes")):
                intraday = plan.archive_status(root, date(2026, 11, 16))
            assert intraday["today"][0] == forecast
            previous = plan.archive_status(root, date(2026, 11, 17))
            assert previous["latest"][0] == forecast
            assert previous["latest_feedback"] is None
            assert intraday["pending"] == [forecast]
            assert intraday["feedback"] == []
            assert forecast.read_bytes() == original
            with pytest.raises(ValueError, match="Replacement must follow"):
                plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
            assert plan.review_pending(root, at(17, 15, 5)) == [(forecast, "settled")]
            assert plan.review_pending(root, at(18, 15, 5)) == [(forecast, "settled")]
            assert plan.review_pending(root, at(19, 15, 5)) == []
            assert plan.load_record(plan.feedback_path(forecast, at(18, 15, 5)), "feedback")["forecast_sha256"]
            state = plan.archive_status(root, date(2026, 11, 18))
            assert state["today"] is None and state["pending"] == []
            assert state["latest"][0] == forecast
            assert state["latest_feedback"][0] == plan.feedback_path(forecast, at(18, 15, 5))
            assert len(state["feedback"]) == 1
            assert forecast.read_bytes() == original


def test_v3_hold_decision_utility_thresholds_and_legacy_score():
    assert [plan.hold_decision_score(change) for change in
            (-2.01, -2, -1.01, -1, -0.51, -0.5, 0, 0.5, 0.51, 1, 1.01, 2, 2.01)] == [
                -2, -1, -1, 0, 0, 1, 1, 1, 0, 0, -1, -1, -2]
    assert plan.score("hold", 0) == 2
    assert plan.score("hold", 0.75) == 1
    assert plan.score("add", 2) == 2 and plan.score("reduce", -2) == 2


def test_v3_archive_full_hold_coverage_and_immutable_versioned_rescore():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        forecast = root / "1115_2026-11-16_1355_prediction.md"
        decisions = {f"00000{index}": {"action": "reduce" if index <= 3 else "hold",
                                         "evidence": "archived decision", "baseline": {"close": 100.},
                                         **({"hold_intent": "sideways"} if index == 7 else {})}
                     for index in range(1, 8)}
        frozen = {"record_type": "prediction", "schema_version": 2,
                  "provenance": "intraday_archived_unfrozen", "frozen_at": at(15, 22).isoformat(),
                  "recorded_at": at(16, 13, 55).isoformat(), "cutoff": at(16, 13, 55).isoformat(),
                  "source_html": None, "baseline_session": "2026-11-13",
                  "first_target_session": "2026-11-16", "target_horizon": "archived_intraday_same_session_close",
                  "scoring": {"version": 1, "neutral_band_percent": 0.5}, "decisions": decisions}
        forecast.write_text("# Archived\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        original_bytes = forecast.read_bytes()
        closes = {"000001": 98., "000002": 102., "000003": 100.,
                  "000004": 100., "000005": 101.5, "000006": 98.5, "000007": 99.2}
        def archived_prices(code, _start, _end):
            return {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (closes[code], 0.)}
        v1_path = plan.feedback_path(forecast, at(17, 0, 20))
        v2_path = plan.rescore_path(forecast, at(18, 15, 5), version=2)
        v3_path = plan.rescore_path(forecast, at(19, 15, 5), version=3)
        with patch.object(plan, "prices", side_effect=archived_prices), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            v1 = plan.review(forecast, v1_path, at(17, 0, 20))
            v2 = plan._evaluate(forecast, v2_path, at(18, 15, 5), 2, rescoring=True)
            prior_bytes = (v1_path.read_bytes(), v2_path.read_bytes())
            v3 = plan._evaluate(forecast, v3_path, at(19, 15, 5), 3, rescoring=True)
            with pytest.raises(ValueError, match="already evaluated under this scoring version"):
                plan._evaluate(forecast, plan.rescore_path(forecast, at(20, 15, 5), version=3), at(20, 15, 5), 3, rescoring=True)
        assert v1["aggregate"]["count"] == 7 and "coverage" not in v1["aggregate"]
        assert v2["aggregate"]["coverage"]["scored"] == 4
        assert v2["aggregate"]["coverage"]["abstained"] == 3
        assert v3["source_scoring_version"] == 1 and v3["evaluation_version"] == 3
        assert v3["provenance"] == "intraday_archived_unfrozen"
        assert v3["return_window"] == "includes_pre_archive_session_move"
        assert v3["target_sessions"] == ["2026-11-16"]
        assert v3["aggregate"]["coverage"] == {"total": 7, "scored": 7, "abstained": 0}
        assert v3["aggregate"]["direction"] == {"count": 3, "signed_score_sum": 0, "signed_score_mean": 0.0}
        assert v3["aggregate"]["hold"] == {"count": 4, "signed_score_sum": -1, "signed_score_mean": -0.25,
            "outcome_counts": {"no_change_reasonable": 1, "missed_add": 1, "missed_reduce": 2}}
        assert v3["aggregate"]["signed_score_mean"] == -0.1429
        assert [v3["individual"][f"00000{index}"]["signed_score"] for index in range(4, 8)] == [1, -1, -1, 0]
        assert v3["individual"]["000007"]["hold_intent"] == "sideways"
        assert v3["individual"]["000005"]["outcome_label"] == "missed_add"
        assert "原有持仓仍受益" in v3["individual"]["000005"]["feedback"]
        assert "非实际盈亏" in v3_path.read_text() and "score-v3" in v3_path.name
        assert forecast.read_bytes() == original_bytes
        assert (v1_path.read_bytes(), v2_path.read_bytes()) == prior_bytes

def test_v4_hold_threshold_is_symmetric_and_preserves_v3_feedback():
    assert [plan.hold_decision_score(change, version=4) for change in
            (-4.01, -4, -3.0532, -2.6679, -1, -0.5, 0, 0.5, 1, 2.6679, 3.0532, 4, 4.01)] == [
                -2, -1, -1, -1, 0, 1, 1, 1, 0, -1, -1, -1, -2]
    assert plan.hold_decision_score(3.0532, version=3) == -2
    assert plan.hold_decision_score(-2.6679, version=3) == -2

    with TemporaryDirectory() as directory:
        root = Path(directory)
        forecast = root / "1119_2026-11-19_0628_prediction.md"
        frozen = {"record_type": "prediction", "schema_version": 2,
                  "provenance": "frozen", "frozen_at": at(19, 6, 28).isoformat(),
                  "cutoff": at(19, 6, 28).isoformat(), "source_html": {},
                  "baseline_session": "2026-11-18", "first_target_session": "2026-11-19",
                  "target_horizon": "next_two_observed_trading_sessions",
                  "scoring": {"version": 3, "neutral_band_percent": 0.5},
                  "decisions": {"300308": {"action": "hold", "evidence": "wait", "baseline": {"close": 100.}},
                                "300750": {"action": "hold", "evidence": "wait", "baseline": {"close": 100.}}}}
        forecast.write_text("# Forecast\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        closes = {"300308": 103.0532, "300750": 97.3321}
        def historical_prices(code, _start, _end):
            return {date(2026, 11, 18): (100., 0.), date(2026, 11, 19): (closes[code], 0.)}
        at_review = at(19, 15, 5)
        with patch.object(plan, "prices", side_effect=historical_prices), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            old_path = plan.feedback_path(forecast, at_review)
            old = plan.review(forecast, old_path, at_review)
            old_bytes = old_path.read_bytes()
            new_path = plan.rescore_path(forecast, at_review)
            new = plan.rescore(forecast, new_path, at_review)
        assert old["evaluation_version"] == 3
        assert [old["individual"][code]["signed_score"] for code in closes] == [-2, -2]
        assert new["source_scoring_version"] == 3 and new["evaluation_version"] == 4
        assert [new["individual"][code]["signed_score"] for code in closes] == [-1, -1]
        assert new["aggregate"]["coverage"]["scored"] == 2
        assert "-1 up to 4%" in new["score_formula"] and "score-v4" in new_path.name
        assert old_path.read_bytes() == old_bytes


def test_new_v4_freeze_settlement_scores_hold_without_sideways_intent():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        decisions.write_text(json.dumps({"159583": {"action": "hold", "evidence": "wait"}}))
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (101., 0.),
                   date(2026, 11, 17): (103., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert frozen["scoring"]["version"] == 4
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing-module"):
            reviewed = plan.review(forecast, plan.feedback_path(forecast, at(17, 15, 5)), at(17, 15, 5))
        assert reviewed["source_scoring_version"] == reviewed["evaluation_version"] == 4
        assert reviewed["aggregate"]["coverage"] == {"total": 1, "scored": 1, "abstained": 0}
        assert reviewed["aggregate"]["direction"]["signed_score_mean"] is None
        assert reviewed["aggregate"]["hold"]["signed_score_mean"] == -1
        assert reviewed["individual"]["159583"]["outcome_label"] == "missed_add"

def test_feedback_uses_frozen_opposing_author_posts_not_later_html():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        bullish, bearish = "12345678901234567", "98765432109876543"
        template = ('<article class="post-card" data-post-id="{post}" data-ts="2026-11-13 14:00">'
                    '<p class="market-stance" data-source="analysis"><strong>市场态度：{score}</strong>'
                    '｜大盘整体｜未来两个交易日 — 依据：星主判断</p></article>')
        duoyu.write_text(template.format(post=bullish, score="+1"))
        dgf.write_text(template.format(post=bearish, score="-1"))
        decisions.write_text(json.dumps({"159583": {"action": "hold",
            "evidence": f"多鱼 ID {bullish}；反向大锅饭 ID {bearish}",
            "post_refs": [{"source": "duoyu", "post_id": bullish}, {"source": "dgf", "post_id": bearish}]}}))
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (102., 0.),
                   date(2026, 11, 17): (103., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert [ref["score"] for ref in frozen["decisions"]["159583"]["post_refs"]] == [1, -1]
        duoyu.write_text(template.format(post=bullish, score="-2"))
        dgf.write_text(template.format(post=bearish, score="+2"))
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing"):
            feedback = plan.review(forecast, plan.feedback_path(forecast, at(16, 15, 5)), at(16, 15, 5))
        refs = feedback["individual"]["159583"]["post_attribution"]
        assert [(ref["source"], ref["score"], ref["alignment"]) for ref in refs] == [
            ("duoyu", 1, "方向相符"), ("dgf", -1, "方向相悖")]
        text = plan.feedback_path(forecast, at(16, 15, 5)).read_text()
        assert f"ID {bullish}" in text and f"ID {bearish}" in text

def test_market_background_is_frozen_and_distinct_from_stock_attribution():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        def card(post_id, score, scope, hour="14:00"):
            return (f'<article class="post-card" data-post-id="{post_id}" data-ts="2026-11-13 {hour}">'
                    f'<p class="market-stance" data-source="analysis"><strong>市场态度：{score:+d}</strong>'
                    f'｜{scope}｜未来两个交易日 — 依据：星主判断</p></article>')
        duoyu.write_text(card("p1", -2, "科技板块") + card("p2", -1, "大盘整体"))
        dgf.write_text(card("p3", 1, "大盘整体"))
        decisions.write_text(json.dumps({"159583": {"action": "hold", "evidence": "基于市场情绪保持仓位", "post_refs": []}}))
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (102., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        assert frozen["market_context"]["duoyu"]["all_mean"] == -1.5
        assert frozen["market_context"]["duoyu"]["broad_mean"] == -1
        duoyu.write_text(card("p1", 2, "大盘整体") + card("p2", 2, "大盘整体"))
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing"):
            feedback = plan.review(forecast, plan.feedback_path(forecast, at(16, 15, 5)), at(16, 15, 5))
        assert feedback["individual"]["159583"]["post_attribution"] == []
        text = plan.feedback_path(forecast, at(16, 15, 5)).read_text()
        assert "多鱼可比大盘：-1.00（1篇）→与该股价格方向相悖" in text
        assert "大锅饭可比大盘：+1.00（1篇）→与该股价格方向相符" in text
        assert "多鱼：-1.50（2篇）" in text


def test_freeze_rejects_missing_structured_post_reference():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        decisions.write_text(json.dumps({"159583": {"action": "hold", "evidence": "ID 12345678901234567 -1"}}))
        with pytest.raises(ValueError, match="post_refs"):
            plan.freeze(duoyu, dgf, decisions, hold, root / "1116_2026-11-16_0900_prediction.md", at(16))

def test_legacy_board_snapshot_yields_partial_attribution():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        duoyu, dgf, decisions, hold = setup_inputs(root)
        forecast = root / "1116_2026-11-16_0900_prediction.md"
        history = {date(2026, 11, 13): (100., 0.), date(2026, 11, 16): (102., 0.)}
        with patch.object(plan, "next_session", return_value=date(2026, 11, 16)), patch.object(plan, "prices", return_value=history), patch.object(plan, "turnover_snapshot", return_value={"status": "unavailable"}):
            frozen = plan.freeze(duoyu, dgf, decisions, hold, forecast, at(16))
        frozen["decisions"]["159583"].pop("post_refs")
        frozen["decisions"]["159583"]["board_evidence"] = [{"source": "dgf", "post_id": "p1",
            "post_time": "2026-11-13 14:00", "post_stance": "市场态度：-1 ｜光通信｜未来两个交易日 — 依据：看跌",
            "relevance": "related"}]
        forecast.write_text("# Frozen\n\n" + plan.RECORD_START + json.dumps(frozen) + plan.RECORD_END)
        with patch.object(plan, "prices", return_value=history), patch.object(plan, "TURNOVER_MODULE", root / "missing"):
            feedback = plan.review(forecast, plan.feedback_path(forecast, at(16, 15, 5)), at(16, 15, 5))
        row = feedback["individual"]["159583"]
        assert row["attribution_status"] == "partial_frozen_board_only"
        assert row["post_attribution"][0]["alignment"] == "方向相悖"
        assert "仅引用了预测时冻结的板块帖子" in plan.feedback_path(forecast, at(16, 15, 5)).read_text()
