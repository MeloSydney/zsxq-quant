"""Filter manifest: which raw posts survive into scoring and rendering.

Manifest shape: {post_id: {"keep": bool, "reason": str}}
  - keep=True  → 帖子进入打分与渲染（reason 可为空）
  - keep=False → 帖子被排除，不打分、不渲染（reason 必须非空，由审阅者给出）

排除的「实质回答」等语义判定属于审阅者/各源适配器；本模块只负责 schema、
覆盖守恒校验与 pass_all 便利函数。
"""

import json
import os
from pathlib import Path
import tempfile

ENTRY_FIELDS = frozenset(("keep", "reason"))


def pass_all(ids) -> dict[str, dict]:
    """DGF 形状：全部 keep，无排除。"""
    return {str(i): {"keep": True, "reason": ""} for i in ids}


def validate_manifest(manifest, ids) -> tuple[list[str], dict[str, int]]:
    """校验 manifest 对 ids 的覆盖守恒；返回 (errors, counts)。

    counts = {"keep": K, "exclude": E, "total": K + E}；errors 非空时 counts 仍给出
    可统计的部分。每个 id 必须恰好出现一次；exclude 的 reason 必须非空。
    """
    errors: list[str] = []
    str_ids = [str(i) for i in ids]
    if not isinstance(manifest, dict):
        return [f"filter manifest 必须是对象，实际为 {type(manifest).__name__}"], {
            "keep": 0, "exclude": 0, "total": 0,
        }
    id_set = set(str_ids)
    missing = id_set - manifest.keys()
    extra = manifest.keys() - id_set
    if missing:
        errors.append(f"filter manifest 缺少帖子: {sorted(missing)}")
    if extra:
        errors.append(f"filter manifest 含 raw 之外的帖子: {sorted(extra)}")
    keep = exclude = 0
    for post_id, entry in manifest.items():
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            errors.append(f"帖子 {post_id}：manifest 条目必须且只能包含 keep、reason")
            continue
        if type(entry["keep"]) is not bool:
            errors.append(f"帖子 {post_id}：keep 必须是布尔值，实际为 {entry['keep']!r}")
            continue
        reason = entry["reason"]
        if not isinstance(reason, str):
            errors.append(f"帖子 {post_id}：reason 必须是字符串")
            continue
        if entry["keep"]:
            keep += 1
        else:
            exclude += 1
            if not reason.strip():
                errors.append(f"帖子 {post_id}：被排除帖的 reason 不得为空")
    return errors, {"keep": keep, "exclude": exclude, "total": keep + exclude}


def kept_ids(manifest, ids) -> list[str]:
    """按 raw 顺序返回 keep 的 post_id；manifest 需已通过 validate_manifest。"""
    return [str(i) for i in ids if manifest.get(str(i), {}).get("keep")]


def load(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("filter manifest JSON 必须是对象 {post_id: {keep, reason}}")
    return data


def save(manifest: dict, path: Path) -> None:
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(manifest, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
