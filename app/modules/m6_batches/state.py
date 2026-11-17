"""批次状态：批次键、参数规范化、水位持久化与陈旧判定。

批次键 = 输入摘要 + 规则版本 + 参数 的 sha256 前 10 位，用作 batch_id。
同一输入重跑命中同一 batch_id，直接复用已落库结果（幂等）。

水位持久化：水位只允许前进。若本次输入的最大 event_time 低于已持久化水位线，
说明拿到的是旧导出，按陈旧批次拒绝，不让它覆盖已经推进过的状态（TRAP-26）。
"""
from __future__ import annotations

import hashlib
import json

from ... import config
from ..m2_schema.coerce import parse_iso


def normalize_params(params: dict | None) -> dict:
    clean = {}
    for key in sorted((params or {})):
        value = params[key]
        if value is None or value == "":
            continue
        clean[str(key)] = value
    return clean


def batch_key(input_digest: str, rule_version: str, params: dict | None = None,
              rule_params: dict | None = None,
              hold_signature: list[str] | None = None) -> str:
    """批次键 = 输入摘要 + 规则版本 + 参数 + 规则参数 + 暂缓集合签名。

    - 规则参数进键：迟到窗口或未来容差变了，同一输入也要按新规则重算；
    - 暂缓集合签名进键：参考时钟追上、有记录被放行时，结果确实不同，必须算新
      批次；而没有记录被放行时签名不变，重跑仍命中同一批次（幂等、不重复）；
    - 参考时刻本身**不进键**：否则每次重跑都会因为墙钟不同而新建批次。
    """
    body = "|".join([
        input_digest,
        rule_version,
        json.dumps(normalize_params(params), sort_keys=True, ensure_ascii=False),
        json.dumps(normalize_params(rule_params), sort_keys=True, ensure_ascii=False),
        json.dumps(sorted(hold_signature or []), ensure_ascii=False),
    ])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]


def batch_id_for(key: str) -> str:
    return f"B-{key}"


def stored_watermark(store) -> str | None:
    return store.watermark_high()


def is_stale(max_event_time, stored) -> bool:
    """输入的最大 event_time 落后于已持久化水位线即为陈旧。"""
    if not stored or not max_event_time:
        return False
    left = parse_iso(max_event_time)
    right = parse_iso(stored)
    if left is None or right is None:
        return False
    return left < right


def watermark_note(stored: str | None, current: str | None) -> dict:
    if not stored:
        return {"state": "first", "stored": None, "current": current}
    if not current:
        return {"state": "kept", "stored": stored, "current": None}
    left, right = parse_iso(current), parse_iso(stored)
    if left is not None and right is not None and left < right:
        return {"state": "regress", "stored": stored, "current": current}
    return {"state": "advance", "stored": stored, "current": current}


def params_text(params: dict | None) -> str:
    clean = normalize_params(params)
    if not clean:
        return "默认"
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


def window_label(settings) -> str:
    return f"{settings.late_window_minutes} 分钟 / {config.WINDOW_NOTE}"
