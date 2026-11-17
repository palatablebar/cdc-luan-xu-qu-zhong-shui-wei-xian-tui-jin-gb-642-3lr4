"""导出编排：把一次运行的证据行、隔离、冲突、快照与清单拼成可下载文件。"""
from __future__ import annotations

from ... import errors, reasons
from ..m5_snapshot import service as m5_service
from . import csv_out, json_out, manifest_out

DATASETS = {
    "events": {"label": "事件证据", "filename": "events.csv",
               "columns": csv_out.EVENT_COLUMNS},
    "quarantine": {"label": "隔离区", "filename": "quarantine.csv",
                   "columns": csv_out.QUARANTINE_COLUMNS},
    "conflicts": {"label": "冲突表", "filename": "conflicts.csv",
                  "columns": csv_out.CONFLICT_COLUMNS},
    "snapshot": {"label": "快照", "filename": "snapshot.csv",
                 "columns": csv_out.SNAPSHOT_COLUMNS},
    "manifest": {"label": "运行清单", "filename": "manifest.json", "columns": ()},
    "snapshot_json": {"label": "快照 JSON", "filename": "snapshot.json",
                      "columns": ()},
}


def _batch(store, batch_id: str | None) -> dict:
    batch = store.get_batch(batch_id) if batch_id else store.latest_any()
    if batch is None:
        raise errors.NotFound("还没有批次，先采信一次", code="E_NO_BATCH")
    return batch


def _snapshot_batch(store, batch_id: str | None) -> dict:
    """快照导出永远指向正式版本；暂缓/失败批次只有证据，不算快照。"""
    batch = _batch(store, batch_id) if batch_id else store.latest_any()
    if batch is None:
        raise errors.NotFound("还没有批次，先采信一次", code="E_NO_BATCH")
    if batch.get("status") == "done":
        return batch
    published = store.latest_published()
    return published or batch


def rows_for(store, batch_id: str, kind: str) -> list[list]:
    if kind == "snapshot":
        target = _snapshot_batch(store, batch_id)
        return csv_out.snapshot_rows(store.load_snapshot(target["batch_id"]))
    rows = store.load_events(batch_id)
    if kind == "events":
        return csv_out.event_rows(rows)
    if kind == "quarantine":
        return csv_out.quarantine_rows([r for r in rows if r["row_class"] == "quarantine"])
    if kind == "conflicts":
        return csv_out.conflict_rows([r for r in rows if r["row_class"] == "conflict"])
    raise errors.BadRequest(f"未知导出集 {kind!r}", code="E_BAD_DATASET")


def render_csv(store, batch_id: str, kind: str) -> tuple[bytes, str]:
    meta = DATASETS.get(kind)
    if meta is None or not meta["columns"]:
        raise errors.BadRequest(f"未知导出集 {kind!r}", code="E_BAD_DATASET")
    name = _snapshot_batch(store, batch_id)["batch_id"] if kind == "snapshot" \
        else batch_id
    rows = rows_for(store, name, kind)
    text = csv_out.to_csv(meta["columns"], rows)
    return csv_out.encode_csv(text), f"{name}-{meta['filename']}"


def render_manifest(store, batch_id: str) -> tuple[bytes, str]:
    payload = manifest_out.build(store, batch_id)
    text = json_out.dumps(payload)
    return text.encode("utf-8"), f"{batch_id}-manifest.json"


def render_snapshot_json(store, batch_id: str) -> tuple[bytes, str]:
    target = _snapshot_batch(store, batch_id)["batch_id"]
    batch = store.get_batch(target) or {}
    summary = m5_service.summarize(store, target)
    rows = m5_service.page_rows(store, target)
    pending = m5_service.pending_rows(store, target)
    reconcile = [_plain(r) for r in m5_service.reconcile_rows(store, target)]
    payload = json_out.snapshot_document(batch=batch, summary=summary, rows=rows,
                                         pending=pending, reconcile=reconcile)
    return json_out.dumps(payload).encode("utf-8"), f"{target}-snapshot.json"


def catalog(store, batch_id: str | None = None) -> list[dict]:
    batch = _batch(store, batch_id)
    rows = store.load_events(batch["batch_id"])
    snapshot_batch = _snapshot_batch(store, None)["batch_id"]
    counts = {
        "events": len(rows),
        "quarantine": sum(1 for r in rows if r["row_class"] == "quarantine"),
        "conflicts": sum(1 for r in rows if r["row_class"] == "conflict"),
        "snapshot": len(store.load_snapshot(snapshot_batch)),
        "manifest": 1,
        "snapshot_json": 1,
    }
    out = []
    for key, meta in DATASETS.items():
        owner = snapshot_batch if key in ("snapshot", "snapshot_json") \
            else batch["batch_id"]
        out.append({"kind": key, "label": meta["label"],
                    "filename": f"{owner}-{meta['filename']}",
                    "rows": counts.get(key, 0),
                    "columns": len(meta["columns"])})
    return out


def summaries(store, batch_id: str) -> dict:
    from ..m8_audit import verify

    batch = _batch(store, batch_id)
    totals = store.load_totals(batch["batch_id"])
    audit = verify.audit_payload(store, batch["batch_id"])
    official = store.latest_published()
    return {
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "status": batch.get("status"),
        "official_batch_id": official["batch_id"] if official else None,
        "input_digest": batch["input_digest"],
        "watermark_utc": batch.get("watermark_utc"),
        "totals": {k: v for k, v in totals.items() if k != "reconcile_json"},
        "datasets": catalog(store, batch_id),
        "audit": audit,
        "empty_hint": reasons.label("X_EMPTY_SELECTION")
        if not store.load_events(batch["batch_id"]) else None,
    }


def _plain(row: dict) -> dict:
    from decimal import Decimal

    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
