from datetime import date, datetime
from pydantic import BaseModel, ConfigDict


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    direction: str | None
    address: str | None
    weight: float | None
    ship_date: date | None
    plan_arrival: date | None
    carrier: str | None
    vehicle: str | None
    status: str
    current_lat: float | None
    current_lng: float | None
    last_position_update: datetime | None
    comment: str | None


class ShipmentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ShipmentOut]
