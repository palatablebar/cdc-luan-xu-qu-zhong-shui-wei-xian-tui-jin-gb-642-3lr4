"""核对页编排：正式快照是哪个批次、哪条记录被挡下、哪批正常数据仍可发布。"""
from __future__ import annotations

from ... import reasons
from ..m2_schema.coerce import iso_utc, parse_iso
from ..m4_watermark import future as future_mod
from . import publish, verify


def _batch_row(batch: dict | None) -> dict | None:
    if not batch:
        return None
    return {
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "status": batch["status"],
        "created_at": batch["created_at"],
        "watermark_utc": batch.get("watermark_utc"),
        "max_event_time": batch.get("max_event_time"),
        "note": batch.get("note", ""),
    }


def held_rows(store, batch_id: str, reference, tolerance,
              *, batch_reference=None) -> tuple[list[dict], list[dict]]:
    """本批被暂缓的记录；顺带算出按当前参考时刻已可放行的子集。

    「超前」按**本批采信时的参考时刻**算（与当时写下的明细一致），
    「已可放行」按**当前参考时刻**算（页面上的参考时钟）。
    """
    rows = store.load_events(batch_id, "held")
    ready = future_mod.release_ready(rows, reference, tolerance)
    ready_refs = {row["row_ref"] for row in ready}
    base = batch_reference or reference
    out = []
    for row in rows:
        when = parse_iso(row["event_time_utc"])
        skew = (future_mod.skew_seconds(when, base)
                if when is not None and base is not None else None)
        out.append({
            "row_ref": row["row_ref"],
            "table": row["table_name"], "pk": row["pk"],
            "version": row["version"], "op": row["op"],
            "event_time": row["event_time_utc"],
            "event_time_raw": row["event_time_raw"],
            "skew_seconds": skew,
            "skew_text": future_mod.human(skew) if skew else "—",
            "reason_code": row["reason_code"] or "W_FUTURE_HELD",
            "detail": row["detail"] or reasons.label("W_FUTURE_HELD"),
            "releasable": row["row_ref"] in ready_refs,
        })
    return out, [r for r in out if r["releasable"]]


def overview(store, *, settings, batch_id: str | None = None) -> dict:
    """核对页与 /api/audit 共用的一份快照：发布归属 + 被挡记录 + 核对结果。"""
    reference = settings.reference_time()
    tolerance = settings.future_tolerance
    official = publish.official(store)
    attempt = store.get_batch(batch_id) if batch_id else store.latest_any()
    if attempt is not None and attempt["status"] == "done":
        official = official or attempt

    attempt_rows, releasable = ([], [])
    if attempt is not None:
        totals = store.load_totals(attempt["batch_id"])
        batch_reference = parse_iso(totals.get("reference_utc"))
        attempt_rows, releasable = held_rows(store, attempt["batch_id"],
                                             reference, tolerance,
                                             batch_reference=batch_reference)

    published_id = official["batch_id"] if official else None
    audit = verify.audit_payload(store, published_id) if published_id else None
    return {
        "reference_utc": iso_utc(reference),
        "horizon_utc": iso_utc(future_mod.horizon(reference, tolerance)),
        "tolerance_minutes": settings.future_tolerance_minutes,
        "official": _batch_row(official),
        "attempt": _batch_row(attempt),
        "attempt_is_official": bool(attempt and published_id == attempt["batch_id"]),
        "held": attempt_rows,
        "held_count": len(attempt_rows),
        "releasable": releasable,
        "releasable_count": len(releasable),
        "audit": audit,
        "publish": _publish_view(store, attempt, official, attempt_rows),
    }


def _publish_view(store, attempt, official, held) -> dict:
    if attempt is None:
        return {"state": "empty", "label": "还没有批次", "note": ""}
    status = attempt["status"]
    if status == "held_only":
        return {"state": "held_only", "label": "本批未发布",
                "note": f"全部 {len(held)} 条被暂缓，正式快照保持 "
                        f"{official['batch_id'] if official else '—'}"}
    if status == "empty_only":
        return {"state": "empty_only", "label": "本批未发布",
                "note": f"本次没有可应用事件，正式快照保持 "
                        f"{official['batch_id'] if official else '—'}"}
    if status == "failed":
        return {"state": "failed", "label": "本批失败",
                "note": "正式快照保持上一版；按失败阶段续跑"}
    if status == "done":
        note = (f"{len(held)} 条被挡下，其余正常数据已发布"
                if held else "本批即正式快照")
        return {"state": "published", "label": "已发布", "note": note}
    return {"state": status or "unknown", "label": status or "—", "note": ""}


def page_context(store, *, settings, batch_id: str | None = None) -> dict:
    data = overview(store, settings=settings, batch_id=batch_id)
    data["window_rows"] = [
        {"name": "迟到窗口",
         "expr": f"event_time < 水位线 → 迟到但应用（{settings.late_window_minutes} 分钟）",
         "side": "半开"},
        {"name": "时钟保护",
         "expr": f"event_time > 参考 + {settings.future_tolerance_minutes} 分钟 → 暂缓",
         "side": "整条挡下"},
        {"name": "快照窗口", "expr": "event_time ≤ as_of → 进正式快照", "side": "含右端"},
    ]
    return data
