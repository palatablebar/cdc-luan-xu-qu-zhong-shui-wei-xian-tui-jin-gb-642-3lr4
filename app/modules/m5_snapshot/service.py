"""快照编排：从已应用事件重建、落库、给页面/导出提供行。

两个安全闸门：

1. 只有正式（`done`）批次才允许重算落库；`held_only` / `failed` 批次只留证据，
   不能顶掉上一版正式快照；
2. 落库前先核对「已采信内容 vs 已落库快照」，不一致（恢复中断可能丢记录）就拒绝，
   旧快照原样保留，由核对页给出差异。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors
from ..m2_schema.coerce import iso_utc, parse_iso
from ..m8_audit import verify
from . import apply as apply_mod
from . import materialize


def applied_events(store, batch_id: str) -> list[apply_mod.Applied]:
    return [apply_mod.Applied.from_row(row)
            for row in store.load_events(batch_id, "applied")]


def as_of_of(store, batch_id: str):
    batch = store.get_batch(batch_id) or {}
    return parse_iso(batch.get("watermark_utc"))


def official_batch(store) -> dict | None:
    return store.latest_published()


def build(store, batch_id: str) -> materialize.Snapshot:
    events = applied_events(store, batch_id)
    as_of = as_of_of(store, batch_id)
    if as_of is None:
        as_of = max((e.event_time for e in events), default=None)
    return materialize.materialize(events, as_of)


def rebuild(store, batch_id: str) -> materialize.Snapshot:
    """可重复调用：同一 as_of 重算快照，行数不变、合计不变。

    非正式批次（`held_only` / `failed`）与核对不一致的批次一律拒绝，
    保证「恢复失败时保留旧版正式快照」。
    """
    batch = store.get_batch(batch_id) or {}
    status = batch.get("status")
    if status and status != "done":
        official = official_batch(store)
        code = {"held_only": "B_HELD_ONLY",
                "empty_only": "B_EMPTY_KEEP_SNAPSHOT"}.get(
                    status, "B_FAILED_KEEP_SNAPSHOT")
        raise errors.Conflict(
            "该批次不是正式批次，正式快照保持上一版",
            code=code,
            detail={"batch_id": batch_id, "status": status,
                    "official_batch_id": official["batch_id"] if official else None,
                    "official_watermark": (official or {}).get("watermark_utc")})
    verify.ensure_consistent(store, batch_id)
    snapshot = build(store, batch_id)
    totals = snapshot.totals()
    store.replace_snapshot(batch_id, snapshot.rows, totals)
    return snapshot


def page_rows(store, batch_id: str) -> list[dict]:
    rows = []
    for row in store.load_snapshot(batch_id):
        rows.append({
            "key": f"{row['table_name']}/{row['pk']}", "table": row["table_name"],
            "pk": row["pk"], "version": row["version"],
            "kind": "pending" if row["pending"] else (
                "tombstone" if row["op"] == "delete" else "active"),
            "op": row["op"], "amount": row["amount"], "qty": row["qty"],
            "event_time": row["event_time_utc"], "row_ref": row["row_ref"],
            "late": row["late"], "pending": row["pending"],
            "payload": row["payload"],
        })
    return rows


def blank_summary() -> dict:
    """空态：页面渲染用的零值摘要（金额仍是两位字符串）。"""
    return {
        "as_of": None, "keys_total": 0, "snapshot_keys": 0, "active_keys": 0,
        "tombstone_keys": 0, "pending_keys": 0, "pending_only": 0,
        "pending_events": 0, "drift_keys": 0, "rows": 0,
        "amount_snapshot": Decimal("0.00"), "amount_pending": Decimal("0.00"),
        "amount_applied": Decimal("0.00"), "amount_drift": Decimal("0.00"),
    }


def summarize(store, batch_id: str) -> dict:
    totals = store.load_totals(batch_id)
    rows = store.load_snapshot(batch_id)
    active = sum(1 for r in rows if r["op"] != "delete" and not r["pending"])
    tomb = sum(1 for r in rows if r["op"] == "delete")
    pending = sum(1 for r in rows if r["pending"])
    return {
        "as_of": totals.get("snapshot_as_of"),
        "keys_total": _int(totals.get("keys_total")) or (len(rows)
                                                          + _int(totals.get("pending_only_keys"))),
        "snapshot_keys": _int(totals.get("snapshot_keys")) or len(rows),
        "active_keys": _int(totals.get("active_keys")) or active,
        "tombstone_keys": _int(totals.get("tombstone_keys")) or tomb,
        "pending_keys": pending,
        "pending_only": _int(totals.get("pending_only_keys")),
        "pending_events": _int(totals.get("pending_events")),
        "drift_keys": _int(totals.get("drift_keys")),
        "amount_snapshot": _dec(totals.get("amount_snapshot")),
        "amount_pending": _dec(totals.get("amount_pending")),
        "amount_applied": _dec(totals.get("amount_applied")),
        "amount_drift": _dec(totals.get("amount_drift")),
        "rows": len(rows),
    }


def reconcile_rows(store, batch_id: str) -> list[dict]:
    events = applied_events(store, batch_id)
    as_of = as_of_of(store, batch_id)
    if as_of is None and events:
        as_of = max(e.event_time for e in events)
    return materialize.reconcile(materialize.materialize(events, as_of))


def pending_rows(store, batch_id: str) -> list[dict]:
    events = applied_events(store, batch_id)
    as_of = as_of_of(store, batch_id)
    if as_of is None and events:
        as_of = max(e.event_time for e in events)
    return materialize.pending_rows(events, as_of)


def state_rows(store, batch_id: str) -> list[dict]:
    return apply_mod.state_rows(apply_mod.build_states(applied_events(store, batch_id)))


def money(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0.00")
    return Decimal(str(value))


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dec(value) -> Decimal:
    return money(value)


def window_rows() -> list[dict]:
    return [
        {"side": "含右端", "expr": "event_time ≤ as_of → 进快照",
         "note": f"as_of = 最大 event_time − {config.LATE_WINDOW_MINUTES} 分钟"},
        {"side": "半开", "expr": "(as_of, +inf) → 待定",
         "note": "待定键本次不入快照"},
    ]


def as_of_text(store, batch_id: str) -> str:
    value = as_of_of(store, batch_id)
    return iso_utc(value) or "—"
