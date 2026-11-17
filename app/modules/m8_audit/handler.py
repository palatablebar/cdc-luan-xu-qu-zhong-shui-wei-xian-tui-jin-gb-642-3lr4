"""M8 入口：核对页 —— 正式快照归属、被挡下的记录、仍可发布的数据。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 发布核对"])


@router.get("/api/audit")
def api_audit(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    data = service.overview(ctx.store, settings=ctx.settings, batch_id=batch_id)
    return _plain(data)


@router.get("/audit")
def page_audit(request: Request, batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)):
    data = service.page_context(ctx.store, settings=ctx.settings, batch_id=batch_id)
    return render(request, "audit.html", page="audit", batch_id=batch_id,
                  overview=_plain(data), window_rows=data["window_rows"],
                  page_title="发布核对")


def _plain(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
