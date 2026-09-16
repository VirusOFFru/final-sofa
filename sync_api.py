"""
HTTP API для бота ВКонтакте.

Telegram-бот — «главный»: у него база, каталог, цены, оплата и админка.
Бот ВК ничего не хранит сам — всё берёт отсюда и присылает сюда:

  • каталог (товары, фото, «Что внутри», условия акции)   → GET  /api/catalog
  • расчёт корзины по тем же правилам, что в Telegram      → POST /api/cart/quote
  • заказы (создание, оплата, статус, «я получил»)         → /api/orders...
  • сообщения в поддержку → приходят админу в Telegram     → POST /api/support
  • ответы админа, смена статусов, изменение каталога      → GET  /api/events (+ ack)

Подробное описание запросов и ответов — в SYNC_API.md.
Запускается, только если задан SYNC_API_TOKEN.
"""
import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict

from aiohttp import web

import database as db
import pricing
import services
from config import (
    DELIVERY_COST,
    DISCOUNT_FROM_QTY,
    DISCOUNT_PERCENT,
    FREE_DELIVERY_SUM,
    REVIEWS_URL,
    SUPPORT_USERNAME,
    SYNC_API_HOST,
    SYNC_API_PORT,
    SYNC_API_TOKEN,
    normalize_url,
)
from keyboards import STATUS_TEXT, support_reply_keyboard

logger = logging.getLogger(__name__)

API_VERSION = 1
SOURCE = services.SOURCE_VK

MAX_ITEMS = 50          # разных товаров в одном заказе
MAX_QTY = 99            # штук одного товара
PHOTO_CACHE_SIZE = 64   # сколько фото держать в памяти
MIN_ADDRESS_LEN = 10

BOT_KEY = web.AppKey("tg_bot", object)
TOKEN_KEY = web.AppKey("api_token", str)
PHOTO_CACHE_KEY = web.AppKey("photo_cache", OrderedDict)
ORDER_LOCK_KEY = web.AppKey("order_lock", asyncio.Lock)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", details=None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.details = details


def ok(**data) -> web.Response:
    return web.json_response({"ok": True, **data}, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def fail(status: int, code: str, message: str, details=None) -> web.Response:
    body = {"ok": False, "error": code, "message": message}
    if details is not None:
        body["details"] = details
    return web.json_response(body, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


# ── Защита и обработка ошибок ─────────────────────────────────────────────────

@web.middleware
async def api_middleware(request: web.Request, handler):
    if request.path != "/api/ping":
        expected = f"Bearer {request.app[TOKEN_KEY]}".encode()
        given = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return fail(401, "unauthorized", "Неверный или отсутствующий токен")
    try:
        return await handler(request)
    except ApiError as e:
        return fail(e.status, e.code, e.message, e.details)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Ошибка API %s %s", request.method, request.path)
        return fail(500, "internal_error", "Внутренняя ошибка сервера")


# ── Разбор входных данных ─────────────────────────────────────────────────────

async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ApiError(400, "bad_json", "Тело запроса должно быть JSON-объектом")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "Тело запроса должно быть JSON-объектом")
    return data


def get_str(data: dict, field: str, *, required: bool = False, max_len: int = 1000, min_len: int = 0) -> str:
    value = data.get(field, "")
    if value is None:
        value = ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ApiError(400, "bad_field", f"Поле «{field}» должно быть строкой")
    value = str(value).strip()
    if required and not value:
        raise ApiError(400, "missing_field", f"Не заполнено поле «{field}»")
    if value and len(value) < min_len:
        raise ApiError(400, "too_short", f"Поле «{field}» слишком короткое (минимум {min_len})")
    if len(value) > max_len:
        raise ApiError(400, "too_long", f"Поле «{field}» слишком длинное (максимум {max_len})")
    return value


def get_vk_user_id(data) -> str:
    value = get_str(data, "vk_user_id", required=True, max_len=20)
    if not value.isdigit() or int(value) <= 0:
        raise ApiError(400, "bad_field", "vk_user_id должен быть положительным числом")
    return value


def path_int(request: web.Request, name: str) -> int:
    raw = request.match_info.get(name, "")
    if not raw.isdigit():
        raise ApiError(404, "not_found", "Не найдено")
    return int(raw)


def parse_items(raw) -> dict[int, int]:
    """[{product_id, quantity}] → {product_id: quantity} (одинаковые товары складываются)."""
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "bad_items", "items — непустой список [{product_id, quantity}]")
    if len(raw) > MAX_ITEMS:
        raise ApiError(400, "bad_items", f"Слишком много позиций (максимум {MAX_ITEMS})")
    merged: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "bad_items", "Каждая позиция — объект {product_id, quantity}")
        product_id = item.get("product_id")
        quantity = item.get("quantity", 1)
        if (not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0
                or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0):
            raise ApiError(400, "bad_items", "product_id и quantity должны быть положительными целыми числами")
        merged[product_id] = merged.get(product_id, 0) + quantity
        if merged[product_id] > MAX_QTY:
            raise ApiError(400, "bad_items", f"Не больше {MAX_QTY} шт. одного товара")
    return merged


async def resolve_items(merged: dict[int, int]) -> tuple[list[dict], list[int]]:
    """Берёт АКТУАЛЬНЫЕ цены из базы. Возвращает (позиции, id недоступных товаров)."""
    items, unavailable = [], []
    for product_id, quantity in merged.items():
        product = await db.get_product(product_id)
        if not product or not product["in_stock"]:
            unavailable.append(product_id)
            continue
        items.append({
            "product_id": product_id,
            "name": product["name"],
            "price": int(product["price"]),
            "quantity": quantity,
        })
    return items, unavailable


# ── Представление данных ──────────────────────────────────────────────────────

def photo_key(file_id: str) -> str:
    """Ключ фото: меняется, когда меняется само фото — удобно для кэша в боте ВК."""
    return hashlib.sha1(file_id.encode()).hexdigest()[:16]


def summary_dict(items: list[dict]) -> dict:
    summary = pricing.calculate(items)
    return {
        "items": [
            {**i, "sum": i["price"] * i["quantity"]} for i in items
        ],
        "quantity": summary.quantity,
        "subtotal": summary.subtotal,
        "discount_percent": summary.discount_percent,
        "discount_amount": summary.discount_amount,
        "delivery": summary.delivery,
        "free_delivery": summary.free_delivery,
        "total": summary.total,
        "to_free_delivery": summary.to_free_delivery,
        "to_discount": summary.to_discount,
        "hints": pricing.hints_for(summary),
        "text": pricing.render_summary(summary, items, title="🛒 Ваша корзина:"),
    }


async def order_dict(order) -> dict:
    items = await db.get_order_items(order["id"])
    return {
        "id": order["id"],
        "status": order["status"],
        "status_text": STATUS_TEXT.get(order["status"], order["status"]),
        "total": int(order["total"]),
        "delivery_cost": int(order["delivery_cost"] or 0),
        "track_number": order["track_number"] or "",
        "payment_url": order["payment_url"] if order["status"] == "pending_payment" else None,
        "address": order["address"],
        "comment": order["comment"] or "",
        "created_at": order["created_at"],
        "items": [
            {"product_id": i["product_id"], "name": i["product_name"],
             "price": int(i["price"]), "quantity": int(i["quantity"])}
            for i in items
        ],
    }


async def get_own_order(request: web.Request, vk_user_id: str):
    order = await db.get_order(path_int(request, "order_id"))
    if not order or not services.is_vk(order) or str(order["external_user_id"]) != vk_user_id:
        raise ApiError(404, "order_not_found", "Заказ не найден")
    return order


# ── Каталог ───────────────────────────────────────────────────────────────────

async def handle_ping(request: web.Request) -> web.Response:
    return ok(api_version=API_VERSION)


async def handle_catalog(request: web.Request) -> web.Response:
    products = []
    for p in await db.get_all_products():
        inside = None
        if db.has_inside(p):
            photos = await db.get_inside_photos(p["id"])
            inside = {
                "text": (p["inside_text"] or "").strip(),
                "photos": [
                    {"key": photo_key(ph["file_id"]), "url": f"/api/inside_photos/{ph['id']}"}
                    for ph in photos
                ],
            }
        products.append({
            "id": p["id"],
            "name": p["name"],
            "description": p["description"] or "",
            "price": int(p["price"]),
            "collection": p["collection_name"] or "",
            "photo": (
                {"key": photo_key(p["photo_id"]), "url": f"/api/products/{p['id']}/photo"}
                if p["photo_id"] else None
            ),
            "inside": inside,
        })

    settings = {
        "delivery_cost": DELIVERY_COST,
        "free_delivery_sum": FREE_DELIVERY_SUM,
        "discount_from_qty": DISCOUNT_FROM_QTY,
        "discount_percent": DISCOUNT_PERCENT,
        "rules_text": pricing.rules_text(),
        "reviews_url": normalize_url(REVIEWS_URL),
        "support_username": SUPPORT_USERNAME,
    }
    version = hashlib.sha1(
        json.dumps([products, settings], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    return ok(version=version, settings=settings, products=products)


async def _photo_response(request: web.Request, file_id: str) -> web.Response:
    cache: OrderedDict = request.app[PHOTO_CACHE_KEY]
    data = cache.get(file_id)
    if data is None:
        bot = request.app[BOT_KEY]
        try:
            tg_file = await bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
        except Exception:
            logger.exception("Не удалось скачать фото из Telegram")
            raise ApiError(502, "photo_unavailable", "Не удалось получить фото из Telegram")
        cache[file_id] = data
        while len(cache) > PHOTO_CACHE_SIZE:
            cache.popitem(last=False)
    else:
        cache.move_to_end(file_id)
    return web.Response(
        body=data,
        content_type="image/jpeg",
        headers={"X-Photo-Key": photo_key(file_id), "Cache-Control": "private, max-age=86400"},
    )


async def handle_product_photo(request: web.Request) -> web.Response:
    product = await db.get_product(path_int(request, "product_id"))
    if not product or not product["photo_id"]:
        raise ApiError(404, "photo_not_found", "Фото не найдено")
    return await _photo_response(request, product["photo_id"])


async def handle_inside_photo(request: web.Request) -> web.Response:
    photo = await db.get_inside_photo(path_int(request, "photo_id"))
    if not photo:
        raise ApiError(404, "photo_not_found", "Фото не найдено")
    return await _photo_response(request, photo["file_id"])


# ── Корзина и заказы ──────────────────────────────────────────────────────────

async def handle_quote(request: web.Request) -> web.Response:
    data = await read_json(request)
    items, unavailable = await resolve_items(parse_items(data.get("items")))
    return ok(summary=summary_dict(items), unavailable=unavailable)


async def handle_create_order(request: web.Request) -> web.Response:
    data = await read_json(request)
    vk_user_id = get_vk_user_id(data)
    name = get_str(data, "name", max_len=128)
    address = get_str(data, "address", required=True, min_len=MIN_ADDRESS_LEN, max_len=500)
    comment = get_str(data, "comment", max_len=1000)
    contact = get_str(data, "contact", max_len=100)
    client_order_id = get_str(data, "client_order_id", max_len=64)
    return_url = get_str(data, "return_url", max_len=500)
    if return_url and not return_url.startswith("https://"):
        raise ApiError(400, "bad_field", "return_url должен начинаться с https://")
    merged = parse_items(data.get("items"))

    # Один заказ за раз — чтобы повторная отправка с тем же client_order_id не создала дубль
    async with request.app[ORDER_LOCK_KEY]:
        if client_order_id:
            existing = await db.get_order_by_external_key(SOURCE, client_order_id)
            if existing:
                if str(existing["external_user_id"]) != vk_user_id:
                    raise ApiError(409, "client_order_id_conflict", "client_order_id уже использован")
                return ok(order=await order_dict(existing), duplicate=True, payment_failed=False)

        items, unavailable = await resolve_items(merged)
        if unavailable:
            raise ApiError(409, "products_unavailable", "Некоторых товаров нет в продаже", unavailable)

        placed = await services.place_order(
            request.app[BOT_KEY],
            items=items,
            address=address,
            comment=comment,
            user_id=0,
            full_name=name,
            source=SOURCE,
            external_user_id=vk_user_id,
            contact=contact,
            external_key=client_order_id,
            return_url=return_url or None,
        )

    order = await db.get_order(placed.order_id)
    return ok(order=await order_dict(order), duplicate=False, payment_failed=placed.payment_failed)


async def handle_list_orders(request: web.Request) -> web.Response:
    vk_user_id = get_vk_user_id(dict(request.query))
    orders = await db.get_external_user_orders(SOURCE, vk_user_id)
    return ok(orders=[await order_dict(o) for o in orders])


async def handle_get_order(request: web.Request) -> web.Response:
    vk_user_id = get_vk_user_id(dict(request.query))
    order = await get_own_order(request, vk_user_id)
    return ok(order=await order_dict(order))


async def handle_check_payment(request: web.Request) -> web.Response:
    data = await read_json(request)
    vk_user_id = get_vk_user_id(data)
    order = await get_own_order(request, vk_user_id)
    result = await services.check_order_payment(request.app[BOT_KEY], order)
    fresh = await db.get_order(order["id"])
    return ok(result=result, order=await order_dict(fresh))


async def handle_order_received(request: web.Request) -> web.Response:
    data = await read_json(request)
    vk_user_id = get_vk_user_id(data)
    order = await get_own_order(request, vk_user_id)
    if order["status"] != "shipped":
        raise ApiError(409, "wrong_status", "Подтвердить получение можно только для заказа «В пути»",
                       {"status": order["status"]})
    await db.update_order_status(order["id"], "delivered")
    await services.notify_admin(
        request.app[BOT_KEY],
        f"✅ 🟦 ВК · Покупатель подтвердил получение заказа #{order['id']}",
    )
    fresh = await db.get_order(order["id"])
    return ok(order=await order_dict(fresh))


# ── Поддержка ─────────────────────────────────────────────────────────────────

async def handle_support(request: web.Request) -> web.Response:
    data = await read_json(request)
    vk_user_id = get_vk_user_id(data)
    name = get_str(data, "name", max_len=128)
    text = get_str(data, "text", max_len=3000)
    attachments = data.get("attachments") or []
    if (not isinstance(attachments, list) or len(attachments) > 10
            or not all(isinstance(a, str) and a.startswith("https://") and len(a) <= 500 for a in attachments)):
        raise ApiError(400, "bad_field", "attachments — список (до 10) https-ссылок")
    if not text and not attachments:
        raise ApiError(400, "missing_field", "Пустое сообщение")

    stored_text = text + ("\n" if text and attachments else "") + "\n".join(f"📎 {a}" for a in attachments)
    message_id = await db.add_support_message(SOURCE, vk_user_id, name, stored_text)

    await services.notify_admin(
        request.app[BOT_KEY],
        f"💬 🟦 ВК · Сообщение в поддержку\n"
        f"👤 {name or 'Без имени'} (vk.com/id{vk_user_id})\n\n"
        f"{stored_text}",
        support_reply_keyboard(message_id),
    )
    return ok(message_id=message_id)


# ── События для бота ВК ───────────────────────────────────────────────────────

async def handle_events(request: web.Request) -> web.Response:
    raw_limit = request.query.get("limit", "50")
    limit = int(raw_limit) if raw_limit.isdigit() else 50
    events = await db.fetch_events(SOURCE, max(1, min(limit, 200)))
    return ok(events=events)


async def handle_events_ack(request: web.Request) -> web.Response:
    data = await read_json(request)
    ids = data.get("ids")
    if (not isinstance(ids, list) or len(ids) > 500
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise ApiError(400, "bad_field", "ids — список (до 500) целых чисел")
    deleted = await db.ack_events(SOURCE, ids)
    return ok(deleted=deleted)


# ── Сборка и запуск ───────────────────────────────────────────────────────────

def create_app(bot, token: str) -> web.Application:
    if not token:
        raise ValueError("Пустой токен API")
    app = web.Application(middlewares=[api_middleware], client_max_size=256 * 1024)
    app[BOT_KEY] = bot
    app[TOKEN_KEY] = token
    app[PHOTO_CACHE_KEY] = OrderedDict()
    app[ORDER_LOCK_KEY] = asyncio.Lock()

    app.router.add_get("/api/ping", handle_ping)
    app.router.add_get("/api/catalog", handle_catalog)
    app.router.add_get("/api/products/{product_id}/photo", handle_product_photo)
    app.router.add_get("/api/inside_photos/{photo_id}", handle_inside_photo)
    app.router.add_post("/api/cart/quote", handle_quote)
    app.router.add_post("/api/orders", handle_create_order)
    app.router.add_get("/api/orders", handle_list_orders)
    app.router.add_get("/api/orders/{order_id}", handle_get_order)
    app.router.add_post("/api/orders/{order_id}/check_payment", handle_check_payment)
    app.router.add_post("/api/orders/{order_id}/received", handle_order_received)
    app.router.add_post("/api/support", handle_support)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/events/ack", handle_events_ack)
    return app


async def start_server(bot) -> web.AppRunner:
    if len(SYNC_API_TOKEN) < 24:
        logger.warning("SYNC_API_TOKEN короче 24 символов — лучше сделать длиннее ⚠️")
    runner = web.AppRunner(create_app(bot, SYNC_API_TOKEN), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, SYNC_API_HOST, SYNC_API_PORT)
    await site.start()
    logger.info("API для бота ВК запущено на %s:%s ✅", SYNC_API_HOST, SYNC_API_PORT)
    return runner
