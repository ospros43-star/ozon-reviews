from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ozon_review_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(128))
    product_name: Mapped[str | None] = mapped_column(String(512))
    author_name: Mapped[str | None] = mapped_column(String(256))
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    review_text: Mapped[str | None] = mapped_column(Text)
    review_created_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Расширенные поля товара
    image_url: Mapped[str | None] = mapped_column(String(512))
    ozon_sku: Mapped[str | None] = mapped_column(String(64))
    ozon_product_url: Mapped[str | None] = mapped_column(String(512))
    purchase_verified: Mapped[bool | None] = mapped_column(Boolean)

    # Статусы: new | pending_approval | auto_posting | posted | rejected | error
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")

    # Архив
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Ответы
    generated_response: Mapped[str | None] = mapped_column(Text)
    edited_response: Mapped[str | None] = mapped_column(Text)
    final_response: Mapped[str | None] = mapped_column(Text)

    # Счётчик перегенераций
    regenerate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)
