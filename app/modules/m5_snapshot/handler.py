"""M5 入口：物化快照、读快照、待定窗口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M5 快照物化"])


@router.post("/api/snapshot")
def api_snapshot(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    """不指定批次时物化正式快照；显式指定非正式批次会被拒绝（保留旧版）。"""
    if batch_id:
        batch = ctx.store.get_batch(batch_id)
    else:
        batch = ctx.store.latest_published() or ctx.store.latest_any()
    if batch is None:
        return {"batch_id": None, "rows": 0, "summary": {}}
    snapshot = service.rebuild(ctx.store, batch["batch_id"])
    official = service.official_batch(ctx.store)
    return {
        "batch_id": batch["batch_id"],
        "as_of": service.as_of_text(ctx.store, batch["batch_id"]),
        "rows": len(snapshot.rows),
        "counters": snapshot.counters,
        "amounts": {k: str(v) for k, v in snapshot.amounts.items()},
        "reconcile": [_jsonable(row) for row in service.reconcile_rows(
            ctx.store, batch["batch_id"])],
        "summary": service.summarize(ctx.store, batch["batch_id"]),
        "official_batch_id": official["batch_id"] if official else None,
        "audit": _audit(ctx.store, batch["batch_id"]),
    }


@router.get("/api/snapshot")
def api_snapshot_read(ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch()
    if batch is None:
        return {"batch_id": None, "rows": [], "summary": {}}
    # 默认读正式快照：当前尝试未发布时，返回上一版正式快照并注明
    official = service.official_batch(ctx.store)
    target = official if official is not None else batch
    return {
        "batch_id": target["batch_id"],
        "as_of": service.as_of_text(ctx.store, target["batch_id"]),
        "summary": service.summarize(ctx.store, target["batch_id"]),
        "rows": [{**row, "amount": None if row["amount"] is None else str(row["amount"])}
                 for row in service.page_rows(ctx.store, target["batch_id"])],
        "pending": service.pending_rows(ctx.store, target["batch_id"]),
        "reconcile": [_jsonable(row) for row in service.reconcile_rows(
            ctx.store, target["batch_id"])],
        "audit": _audit(ctx.store, target["batch_id"]),
        "attempt_batch_id": batch["batch_id"],
        "attempt_status": batch["status"],
        "is_official": official is not None
        and official["batch_id"] == target["batch_id"],
        "official_batch_id": official["batch_id"] if official else None,
    }


@router.get("/snapshot")
def page_snapshot(request: Request, batch_id: str | None = None,
                  ctx: Ctx = Depends(get_ctx)):
    attempt = ctx.current_batch(batch_id)
    official = service.official_batch(ctx.store)
    # 页面读正式快照；显式选中的批次若本身已发布则照它显示，否则提示保持上一版
    if attempt is not None and attempt.get("status") == "done":
        view = attempt
    else:
        view = official
    layers = ctx.layers(view["batch_id"]) if view else {
        "inputs": [], "events": [], "snapshot": [], "watermarks": [],
        "stages": [], "totals": {}}
    summary = (service.summarize(ctx.store, view["batch_id"]) if view
               else service.blank_summary())
    is_official = bool(view and official is not None
                       and official["batch_id"] == view["batch_id"])
    stale_attempt = bool(attempt and (official is None
                                      or attempt["batch_id"] != official["batch_id"]))
    held_count = int((layers.get("totals") or {}).get("held", 0) or 0)
    return render(request, "snapshot.html", batch=view, page="snapshot",
                  summary=summary,
                  rows=service.page_rows(ctx.store, view["batch_id"])[:300]
                  if view else [],
                  pending=service.pending_rows(ctx.store, view["batch_id"])
                  if view else [],
                  reconcile=service.reconcile_rows(ctx.store, view["batch_id"])
                  if view else [],
                  window_rows=service.window_rows(),
                  official=official,
                  attempt=attempt,
                  is_official=is_official,
                  stale_attempt=stale_attempt,
                  held_count=held_count,
                  totals=layers["totals"], inputs=layers["inputs"],
                  page_title="快照")


def _jsonable(row: dict) -> dict:
    from decimal import Decimal

    out = {}
    for key, value in row.items():
        out[key] = str(value) if isinstance(value, Decimal) else value
    return out


def _audit(store, batch_id: str) -> dict:
    from ..m8_audit import verify

    return verify.audit_payload(store, batch_id)
