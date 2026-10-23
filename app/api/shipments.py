import re
from fastapi import APIRouter, HTTPException, Query
from app.db import get_pool
from app.models import ShipmentListResponse, ShipmentOut
from app.repository import search_shipments, get_shipment, list_shipments

router = APIRouter(prefix="/api/shipments", tags=["shipments"])


@router.get("", response_model=ShipmentListResponse)
async def list_or_search(
    q: str | None = Query(None, description="Поиск: минимум 4 цифры из номера"),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    pool = await get_pool()
    if q:
        digits = re.sub(r"\D", "", q)
        if len(digits) < 4:
            raise HTTPException(400, "Нужно минимум 4 цифры для поиска")
        total, rows = await search_shipments(pool, digits[-4:], limit, offset)
    else:
        total, rows = await list_shipments(pool, status, limit, offset)

    return ShipmentListResponse(
        total=total, limit=limit, offset=offset,
        items=[ShipmentOut(**dict(r)) for r in rows],
    )


@router.get("/{order_id}", response_model=ShipmentOut)
async def get_one(order_id: str):
    pool = await get_pool()
    row = await get_shipment(pool, order_id)
    if not row:
        raise HTTPException(404, "Not found")
    return ShipmentOut(**dict(row))
