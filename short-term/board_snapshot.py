#!/usr/bin/env python3
"""Capture current Eastmoney board memberships for held stocks; not historical memberships."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

# The existing transport adapter is required for Eastmoney on this host.
import code_info  # noqa: F401
import efinance as ef


def snapshot(codes: list[str], now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        raise ValueError("Snapshot time must have a timezone")
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if not codes or len(codes) != len(set(codes)):
        raise ValueError("Codes must be nonempty and unique")
    stocks = {}
    for code in codes:
        frame = ef.stock.get_belong_board(code)
        if frame is None or frame.empty:
            raise ValueError(f"{code}: board membership unavailable")
        rows = frame.loc[frame["股票代码"].astype(str) == code, ["板块代码", "板块名称"]]
        if rows.empty:
            raise ValueError(f"{code}: board membership code mismatch")
        boards = {str(row.板块代码): str(row.板块名称) for row in rows.itertuples(index=False)}
        if len(boards) != len(rows):
            raise ValueError(f"{code}: duplicate board code")
        stocks[code] = boards
    return {"schema_version": 1, "fetched_at": now.isoformat(), "provider": "efinance/Eastmoney get_belong_board", "stocks": stocks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", type=Path, default=Path(__file__).with_name("hold"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    from plan_feedback import read_hold
    data = snapshot(read_hold(args.hold))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Captured {len(data['stocks'])} stocks at {data['fetched_at']}: {args.output}")


if __name__ == "__main__":
    main()
