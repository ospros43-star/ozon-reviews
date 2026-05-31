"""Роуты планирования поставок."""
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.review import Review
from app.models.supply_plan import SupplyPlan, SupplyPlanItem, SupplyPlanTag
from app.services.supply_calculator import CLUSTERS

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _pending_count(db: Session) -> int:
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    return sum(v for k, v in rows if k == "pending_approval")


def _next_plan_number(db: Session) -> int:
    max_num = db.scalar(select(func.max(SupplyPlan.number))) or 800
    return max_num + 1


# ── Список поставок ──────────────────────────────────────────
@router.get("/supplies", response_class=HTMLResponse)
async def supplies_list(request: Request, db: Session = Depends(get_db)):
    plans = db.scalars(
        select(SupplyPlan).order_by(SupplyPlan.number.desc())
    ).all()
    return templates.TemplateResponse("supplies.html", {
        "request": request,
        "active_nav": "supplies",
        "pending_count": _pending_count(db),
        "plans": plans,
    })


# ── Форма создания ───────────────────────────────────────────
@router.get("/supplies/new", response_class=HTMLResponse)
async def supply_new_form(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    default_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    # Прогреваем кеш в фоне пока пользователь заполняет форму
    background_tasks.add_task(_warmup_supply_cache, 28)
    return templates.TemplateResponse("supply_new.html", {
        "request": request,
        "active_nav": "supplies",
        "pending_count": _pending_count(db),
        "clusters": CLUSTERS,
        "tomorrow": tomorrow,
        "default_date": default_date,
    })


async def _warmup_supply_cache(analysis_days: int) -> None:
    try:
        from app.services.supply_calculator import fetch_product_analytics
        await fetch_product_analytics(analysis_days)
        logger.info("Supply analytics cache warmed up (analysis_days=%d)", analysis_days)
    except Exception as exc:
        logger.warning("Supply cache warmup failed: %s", exc)


# ── Выбор товаров ────────────────────────────────────────────
@router.post("/supplies/products", response_class=HTMLResponse)
async def supply_products(request: Request, db: Session = Depends(get_db)):
    from app.services.supply_calculator import fetch_product_analytics, CLUSTER_BY_ID

    form = await request.form()
    delivery_date_str = form.get("delivery_date", "")
    source_cluster = form.get("source_cluster", "msk")
    target_clusters = form.getlist("clusters")
    analysis_days = int(form.get("analysis_days", 28))
    target_days = int(form.get("target_days", 30))

    if not delivery_date_str or not target_clusters:
        return RedirectResponse("/supplies/new", status_code=302)

    products = await fetch_product_analytics(analysis_days)
    source_name = CLUSTER_BY_ID.get(source_cluster, {}).get("name", source_cluster)

    return templates.TemplateResponse("supply_products.html", {
        "request": request,
        "active_nav": "supplies",
        "pending_count": _pending_count(db),
        "products": products,
        "delivery_date": delivery_date_str,
        "source_cluster": source_cluster,
        "source_name": source_name,
        "target_clusters": target_clusters,
        "analysis_days": analysis_days,
        "target_days": target_days,
    })


# ── Расчёт поставки ──────────────────────────────────────────
@router.post("/supplies/calculate", response_class=HTMLResponse)
async def supply_calculate(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.services.supply_calculator import calculate_supply, CLUSTER_BY_ID

    form = await request.form()
    delivery_date_str = form.get("delivery_date", "")
    source_cluster = form.get("source_cluster", "msk")
    target_clusters = form.getlist("clusters")
    analysis_days = int(form.get("analysis_days", 28))
    target_days = int(form.get("target_days", 30))
    selected_skus = form.getlist("selected_skus") or None

    if not delivery_date_str or not target_clusters:
        return RedirectResponse("/supplies/new", status_code=302)

    delivery_date = datetime.strptime(delivery_date_str, "%Y-%m-%d")

    results = await calculate_supply(
        delivery_date=delivery_date,
        source_cluster_id=source_cluster,
        target_cluster_ids=target_clusters,
        analysis_days=analysis_days,
        target_days=target_days,
        selected_skus=selected_skus,
    )

    source_name = CLUSTER_BY_ID.get(source_cluster, {}).get("name", source_cluster)
    target_names = [CLUSTER_BY_ID.get(c, {}).get("name", c) for c in target_clusters]

    return templates.TemplateResponse("supply_result.html", {
        "request": request,
        "active_nav": "supplies",
        "pending_count": _pending_count(db),
        "results": results,
        "delivery_date": delivery_date_str,
        "source_cluster": source_cluster,
        "source_name": source_name,
        "target_clusters": target_clusters,
        "target_names": target_names,
        "analysis_days": analysis_days,
        "target_days": target_days,
        "total_skus": len(results),
        "total_units": sum(r["total_needed"] for r in results),
    })


# ── Сохранение плана ─────────────────────────────────────────
@router.post("/supplies/save")
async def supply_save(request: Request, db: Session = Depends(get_db)):
    import json
    body = await request.json()

    plan = SupplyPlan(
        number=_next_plan_number(db),
        delivery_date=datetime.strptime(body["delivery_date"], "%Y-%m-%d"),
        status="planning",
        user_name=body.get("user_name", ""),
        notes=body.get("notes", ""),
    )
    db.add(plan)
    db.flush()

    for item_data in body.get("items", []):
        item = SupplyPlanItem(
            plan_id=plan.id,
            offer_id=item_data.get("offer_id", ""),
            sku=item_data.get("sku", ""),
            product_name=item_data.get("name", ""),
            image_url=item_data.get("image", ""),
            units=int(item_data.get("units", 0)),
        )
        db.add(item)

    for tag in body.get("tags", []):
        db.add(SupplyPlanTag(plan_id=plan.id, name=tag))

    db.commit()
    return JSONResponse({"ok": True, "id": plan.id, "number": plan.number})


# ── Удаление плана ───────────────────────────────────────────
@router.delete("/api/supplies/{plan_id}")
async def supply_delete(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(SupplyPlan, plan_id)
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete(plan)
    db.commit()
    return JSONResponse({"ok": True})


# ── Детали плана ─────────────────────────────────────────────
@router.get("/supplies/{plan_id}", response_class=HTMLResponse)
async def supply_detail(plan_id: int, request: Request, db: Session = Depends(get_db)):
    plan = db.get(SupplyPlan, plan_id)
    if not plan:
        return RedirectResponse("/supplies", status_code=302)
    return templates.TemplateResponse("supply_detail.html", {
        "request": request,
        "active_nav": "supplies",
        "pending_count": _pending_count(db),
        "plan": plan,
    })
