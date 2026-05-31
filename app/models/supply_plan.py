"""Модель плана поставки — локальная история расчётов поставок."""
import json
from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from app.database import Base


class SupplyPlan(Base):
    __tablename__ = "supply_plans"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(Integer, unique=True, nullable=False)  # порядковый номер плана
    delivery_date = Column(DateTime, nullable=True)        # планируемая дата поставки
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    status = Column(String, default="planning")            # planning | ready | sent | cancelled
    user_name = Column(String, default="")
    notes = Column(Text, default="")

    items = relationship("SupplyPlanItem", back_populates="plan",
                         cascade="all, delete-orphan", lazy="selectin")
    tags = relationship("SupplyPlanTag", back_populates="plan",
                        cascade="all, delete-orphan", lazy="selectin")

    @property
    def sku_count(self) -> int:
        return len(self.items)

    @property
    def total_units(self) -> int:
        return sum(i.units for i in self.items)

    @property
    def total_volume(self) -> float:
        return round(sum(i.volume_liters for i in self.items), 1)

    @property
    def total_weight(self) -> float:
        return round(sum(i.weight_kg for i in self.items), 2)


class SupplyPlanItem(Base):
    __tablename__ = "supply_plan_items"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("supply_plans.id"), nullable=False)
    plan = relationship("SupplyPlan", back_populates="items")

    offer_id = Column(String, default="")    # артикул продавца
    sku = Column(String, default="")         # Ozon SKU
    product_name = Column(String, default="")
    image_url = Column(String, default="")

    units = Column(Integer, default=0)        # кол-во единиц товара
    boxes = Column(Integer, default=1)        # кол-во коробов
    units_per_box = Column(Integer, default=0)  # единиц в коробе

    # Физические параметры короба
    length_cm = Column(Float, default=0)
    width_cm = Column(Float, default=0)
    height_cm = Column(Float, default=0)
    weight_kg = Column(Float, default=0)

    @property
    def volume_liters(self) -> float:
        if self.length_cm and self.width_cm and self.height_cm:
            return round(self.length_cm * self.width_cm * self.height_cm * self.boxes / 1000, 2)
        return 0.0


class SupplyPlanTag(Base):
    __tablename__ = "supply_plan_tags"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("supply_plans.id"), nullable=False)
    plan = relationship("SupplyPlan", back_populates="tags")
    name = Column(String, default="")
