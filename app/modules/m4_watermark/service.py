"""水位页编排：把落库的水位序列还原成时间轴刻度与迟到表。

暂缓（held）记录不在水位序列里（它们不推进水位），单独从事件表取出，供首页
与水位页在「水位」之外并列展示，说明为什么水位没有跟着未来时间跳。
"""
from __future__ import annotations

from ... import config
from ..m2_schema.coerce import parse_iso
from . import late


def _rows(store, batch_id: str) -> list[dict]:
    return store.load_watermarks(batch_id)


def summary(store, batch_id: str) -> dict:
    rows = _rows(store, batch_id)
    if not rows:
        return {"final": None, "max_event_time": None, "late": 0, "on_time": 0,
                "total": 0, "late_rate": 0.0, "worst_behind_minutes": 0,
                "boundary_note": "", "held": held_count(store, batch_id)}
    final = max(r["watermark_utc"] for r in rows)
    max_event = max(r["event_time_utc"] for r in rows)
    marks = [r["watermark_utc"] for r in rows]
    late_rows = [r for r in rows if r["late"]]
    worst = max((r["behind_seconds"] for r in late_rows), default=0)
    return {
        "final": final,
        "max_event_time": max_event,
        "min_event_time": min(r["event_time_utc"] for r in rows),
        "min_watermark": min(marks),
        "late": len(late_rows),
        "on_time": len(rows) - len(late_rows),
        "total": len(rows),
        "late_rate": round(len(late_rows) / len(rows), 4) if rows else 0.0,
        "worst_behind_minutes": late.minutes(worst),
        "boundary_note": "event_time 严格小于水位线才算迟到",
        "held": held_count(store, batch_id),
    }


def held_count(store, batch_id: str) -> int:
    return len(store.load_events(batch_id, "held"))


def held_rows(store, batch_id: str) -> list[dict]:
    """被挡下的记录：核对页的简版，放在水位页说明「水位为何没跳」。"""
    out = []
    for row in store.load_events(batch_id, "held"):
        out.append({
            "row_ref": row["row_ref"],
            "table": row["table_name"], "pk": row["pk"],
            "version": row["version"], "op": row["op"],
            "event_time": row["event_time_utc"],
            "event_time_raw": row["event_time_raw"],
            "reason_code": row["reason_code"] or "W_FUTURE_HELD",
            "detail": row["detail"],
        })
    return sorted(out, key=lambda r: r["event_time"] or "")


def reference_of(store, batch_id: str) -> dict:
    totals = store.load_totals(batch_id)
    return {
        "reference": totals.get("reference_utc"),
        "horizon": totals.get("future_horizon_utc"),
        "held": int(totals.get("held", 0) or 0),
    }


def timeline(store, batch_id: str) -> dict:
    rows = _rows(store, batch_id)
    if not rows:
        return {"ticks": [], "final": None, "max": None, "min": None,
                "late_window": config.LATE_WINDOW_MINUTES, "held": []}
    events = [parse_iso(r["event_time_utc"]) for r in rows]
    marks = [parse_iso(r["watermark_utc"]) for r in rows]
    ticks = []
    for row, event_time, mark in zip(rows, events, marks):
        ticks.append({
            "seq": row["seq"], "ref": row["row_ref"],
            "event": event_time.isoformat() if event_time else None,
            "mark": mark.isoformat() if mark else None,
            "late": bool(row["late"]),
            "behind": row["behind_seconds"],
        })
    final = max(marks)
    held = [{"ref": row["row_ref"], "event": row["event_time"]}
            for row in held_rows(store, batch_id)]
    return {
        "ticks": ticks,
        "final": final.isoformat() if final else None,
        "max": max(events).isoformat() if events else None,
        "min": min(min(events), min(marks)).isoformat() if events else None,
        "late_window": config.LATE_WINDOW_MINUTES,
        "late": sum(1 for r in rows if r["late"]),
        "on_time": sum(1 for r in rows if not r["late"]),
        "held": held,
    }


def late_table(store, batch_id: str) -> list[dict]:
    rows = [r for r in _rows(store, batch_id) if r["late"]]
    out = []
    for row in rows:
        out.append({
            "row_ref": row["row_ref"],
            "event_time": row["event_time_utc"],
            "watermark": row["watermark_utc"],
            "behind_minutes": late.minutes(row["behind_seconds"]),
            "behind_text": late.behind_text(row["behind_seconds"]),
            "band": late.classify_minutes(row["behind_seconds"],
                                           config.LATE_WINDOW_MINUTES),
        })
    return sorted(out, key=lambda r: -r["behind_minutes"])


def advance_points(store, batch_id: str) -> list[dict]:
    """水位推进点：只保留真正抬高水位的刻度，时间轴上打点用。"""
    rows = _rows(store, batch_id)
    out = []
    best: str | None = None
    for row in rows:
        if best is None or row["watermark_utc"] > best:
            best = row["watermark_utc"]
            out.append({"seq": row["seq"], "ref": row["row_ref"],
                        "watermark": row["watermark_utc"],
                        "event_time": row["event_time_utc"]})
    return out


def window_rows() -> list[dict]:
    return late.window_table(config.LATE_WINDOW_MINUTES)


def clock_rows(tolerance_minutes: int) -> list[dict]:
    return late.clock_table(config.LATE_WINDOW_MINUTES, tolerance_minutes)
