from datetime import datetime

from pydantic import BaseModel


class ReviewOut(BaseModel):
    id: int
    ozon_review_id: str
    product_id: str | None
    product_name: str | None
    author_name: str | None
    rating: int
    review_text: str | None
    review_created_at: datetime | None
    status: str
    is_archived: bool
    image_url: str | None
    ozon_sku: str | None
    ozon_product_url: str | None
    purchase_verified: bool | None
    generated_response: str | None
    edited_response: str | None
    final_response: str | None
    regenerate_count: int
    fetched_at: datetime
    processed_at: datetime | None
    posted_at: datetime | None
    error_message: str | None

    model_config = {"from_attributes": True}


class ReviewList(BaseModel):
    items: list[ReviewOut]
    total: int


class ApproveRequest(BaseModel):
    response_text: str


class EditResponseRequest(BaseModel):
    edited_response: str


class StatsOut(BaseModel):
    new: int
    pending_approval: int
    auto_posting: int
    posted: int
    rejected: int
    error: int
    total: int
    pending_unarchived: int
    pending_negative: int = 0
    pending_positive: int = 0
    posted_today: int = 0
    posted_negative: int = 0
    posted_positive: int = 0
    fetched_today: int = 0
    coverage_pct: float = 0.0


class PollResult(BaseModel):
    fetched: int = 0
    auto_posted: int = 0
    pending: int = 0
    errors: int = 0
    error: str | None = None
