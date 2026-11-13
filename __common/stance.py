"""Validated, human-reviewed market stances shared by both ZSXQ analysis paths.

This module never infers a stance from source content. Reviewers supply records;
source adapters decide which posts require coverage and where annotations appear.
"""

from dataclasses import dataclass
from html import escape
import json
import re
from pathlib import Path


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
SCORES = frozenset((-2, -1, 0, 1, 2))
FIELDS = frozenset(("post_id", "score", "scope", "timescale", "evidence"))


@dataclass(frozen=True)
class Stance:
    post_id: str
    score: int
    scope: str
    timescale: str
    evidence: str


def validate(records: list[Stance], post_ids: list[str]) -> dict[str, Stance]:
    if not post_ids or len(post_ids) != len(set(post_ids)) or any(not ID.fullmatch(identifier) for identifier in post_ids):
        raise ValueError("原始帖子 ID 为空、重复或无效")
    indexed: dict[str, Stance] = {}
    for record in records:
        if not isinstance(record.post_id, str) or not ID.fullmatch(record.post_id):
            raise ValueError(f"无效的帖子 ID：{record.post_id!r}")
        if type(record.score) is not int or record.score not in SCORES:
            raise ValueError(f"帖子 {record.post_id} 的市场态度分不在五档内")
        for field in ("scope", "timescale", "evidence"):
            value = getattr(record, field)
            if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
                raise ValueError(f"帖子 {record.post_id} 的 {field} 为空或无效")
        if record.post_id in indexed:
            raise ValueError(f"帖子 {record.post_id} 的市场态度重复")
        indexed[record.post_id] = record
    missing, extra = set(post_ids) - indexed.keys(), indexed.keys() - set(post_ids)
    if missing or extra:
        raise ValueError(f"市场态度覆盖不完整：缺少 {sorted(missing)}；多余 {sorted(extra)}")
    return indexed


def load_json(path: Path) -> list[Stance]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("市场态度 JSON 必须是记录数组")
    records = []
    for item in data:
        if not isinstance(item, dict) or item.keys() != FIELDS:
            raise ValueError("市场态度记录必须且只能包含 post_id、score、scope、timescale、evidence")
        records.append(Stance(**item))
    return records


def stance_html(record: Stance, *, origin: str) -> str:
    """One escaped, visually consistent annotation; page-level layout stays source-specific."""
    score = f"{record.score:+d}" if record.score else "0"
    return (
        '<p class="market-stance" data-source="analysis">'
        f'<span class="stance-origin">{escape(origin, quote=True)}</span><br>'
        f'<strong>市场态度：{score}</strong>｜{escape(record.scope, quote=True)}｜'
        f'{escape(record.timescale, quote=True)} — 依据：{escape(record.evidence, quote=True)}</p>'
    )
