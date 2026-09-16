"""
Общая логика заказов — одна и та же для Telegram-бота и для API бота ВКонтакте.

Здесь: оформление заказа (+ платёж ЮKassa), проверка оплаты, тексты для админа
и уведомление покупателя (в Telegram — сообщением, в ВК — через очередь событий).
"""
import asyncio
import logging
from dataclasses import dataclass

import database as db
import payments
import pricing
from config import ADMIN_ID
from keyboards import STATUS_TEXT, order_manage_keyboard

logger = logging.getLogger(__name__)

# Сколько ждать ответ ЮKassa. Библиотека yookassa ходит по сети блокирующе,
# поэтому вызов уходит в отдельный поток с таймаутом — иначе бот зависнет.
PAYMENT_REQUEST_TIMEOUT = 20

SOURCE_VK = "vk"
SOURCE_TG = "tg"


# ── Тексты ────────────────────────────────────────────────────────────────────

def is_vk(order) -> bool:
    return (order["source"] or SOURCE_TG) == SOURCE_VK


def source_tag(order) -> str:
    """Короткая метка площадки для списков заказов."""
    return "🟦 ВК · " if is_vk(order) else ""


def customer_line(order) -> str:
    if is_vk(order):
        text = f"👤 {order['full_name'] or 'Без имени'} (ВК: vk.com/id{order['external_user_id']})"
    else:
        username = order["username"]
        text = f"👤 {order['full_name']} (@{username if username else 'без username'})"
    if order["contact"]:
        text += f"\n📞 {order['contact']}"
    return text


def order_money_lines(order, items) -> str:
    """Сумма / скидка / доставка / итог по уже сохранённому заказу."""
    subtotal = sum(int(i["price"]) * int(i["quantity"]) for i in items)
    delivery = int(order["delivery_cost"] or 0)
    total = int(order["total"])
    discount_amount = subtotal + delivery - total

    text = f"💰 Сумма брелоков: {subtotal} ₽\n"
    if discount_amount > 0:
        percent = round(discount_amount / subtotal * 100) if subtotal else 0
        text += f"🎁 Скидка {percent}%: −{discount_amount} ₽\n"
    text += f"🚚 Доставка: {'бесплатно' if delivery == 0 else f'{delivery} ₽'}\n"
    text += f"💰 Итого: {total} ₽"
    return text


def admin_order_text(order, items, header: str, note: str = "") -> str:
    text = header + "\n"
    if note:
        text += note + "\n"
    text += "\n"
    if is_vk(order):
        text += "🌐 Источник: ВКонтакте\n"
    text += customer_line(order) + "\n"
    text += f"📍 Адрес: {order['address']}\n"
    if order["comment"]:
        text += f"💬 Комментарий: {order['comment']}\n"
    text += "\n" + pricing.format_cart_lines(items) + "\n\n" + order_money_lines(order, items)
    return text


# ── Уведомления ───────────────────────────────────────────────────────────────

async def notify_admin(bot, text: str, reply_markup=None) -> None:
    """Сообщение админу. Ошибка отправки не должна ломать сценарий покупателя."""
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=reply_markup)
    except Exception:
        logger.exception("Не удалось отправить уведомление администратору")


async def notify_customer(bot, order, text: str, tg_reply_markup=None) -> None:
    """
    Сообщает покупателю об изменении заказа.
    Telegram — сразу сообщением; ВК — событием в очередь, бот ВК сам его доставит.
    """
    if is_vk(order):
        fresh = await db.get_order(order["id"]) or order
        await db.add_event(SOURCE_VK, "order_status", fresh["external_user_id"], {
            "order_id": fresh["id"],
            "status": fresh["status"],
            "status_text": STATUS_TEXT.get(fresh["status"], fresh["status"]),
            "track_number": fresh["track_number"] or "",
            "text": text,
        })
        return
    try:
        await bot.send_message(chat_id=order["user_id"], text=text, reply_markup=tg_reply_markup)
    except Exception:
        logger.warning("Не удалось отправить сообщение пользователю %s", order["user_id"])


# ── Оформление заказа ─────────────────────────────────────────────────────────

@dataclass
class PlacedOrder:
    order_id: int
    status: str             # 'pending_payment' или 'new'
    total: int
    payment_url: str | None  # ссылка на оплату (если оплата включена и платёж создан)
    payment_failed: bool     # оплата включена, но платёж создать не удалось


async def place_order(
    bot,
    *,
    items: list[dict],
    address: str,
    comment: str,
    user_id: int,
    username: str = "",
    full_name: str = "",
    source: str = SOURCE_TG,
    external_user_id: str = "",
    contact: str = "",
    external_key: str = "",
    return_url: str | None = None,
) -> PlacedOrder:
    """
    Создаёт заказ, при включённой ЮKassa — платёж, и уведомляет админа.
    items — [{"product_id", "name", "price", "quantity"}] с АКТУАЛЬНЫМИ ценами из БД.
    """
    summary = pricing.calculate(items)
    payment_enabled = payments.is_configured()

    order_id = await db.create_order(
        user_id=user_id,
        username=username,
        full_name=full_name,
        address=address,
        comment=comment,
        total=summary.total,
        delivery_cost=summary.delivery,
        items=items,
        status="pending_payment" if payment_enabled else "new",
        source=source,
        external_user_id=external_user_id,
        contact=contact,
        external_key=external_key,
    )

    payment_url = None
    payment_failed = False
    note = ""

    if payment_enabled:
        result = None
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    payments.create_payment,
                    float(summary.total),
                    order_id,
                    user_id,
                    f"Заказ #{order_id} — магазин Ssarafos",
                    return_url,
                ),
                timeout=PAYMENT_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("ЮKassa не ответила за %s сек. (заказ #%s)", PAYMENT_REQUEST_TIMEOUT, order_id)
            note = "⚠️ ЮKassa не ответила при создании платежа"
        except Exception:
            logger.exception("Не удалось создать платёж в ЮKassa (заказ #%s)", order_id)
            note = "⚠️ Ошибка создания платежа ЮKassa"

        if result is None:
            payment_failed = True
            await db.update_order_status(order_id, "new")
        else:
            payment_url = result["confirmation_url"]
            await db.set_payment_info(order_id, result["payment_id"], payment_url)
            note = "⏳ Ожидает оплаты"

    order = await db.get_order(order_id)
    order_items = await db.get_order_items(order_id)
    await notify_admin(
        bot,
        admin_order_text(order, order_items, f"🔔 Новый заказ #{order_id}", note),
        order_manage_keyboard(order_id, order["status"]),
    )

    return PlacedOrder(
        order_id=order_id,
        status=order["status"],
        total=int(order["total"]),
        payment_url=payment_url,
        payment_failed=payment_failed,
    )


# ── Проверка оплаты ───────────────────────────────────────────────────────────

# Возможные результаты check_order_payment
PAY_NOT_PENDING = "not_pending"            # заказ уже не ждёт оплаты
PAY_NO_PAYMENT = "no_payment"              # у заказа нет платежа
PAY_TIMEOUT = "timeout"                    # ЮKassa не ответила
PAY_ERROR = "error"                        # ошибка запроса к ЮKassa
PAY_PAID = "paid"                          # оплата подтверждена (только что)
PAY_PENDING = "pending"                    # ещё не оплачено
PAY_CANCELLED = "cancelled"                # платёж отменён/истёк → заказ отменён (только что)
PAY_ALREADY_PROCESSED = "already_processed"  # статус успел измениться параллельно


async def check_order_payment(bot, order) -> str:
    order_id = order["id"]

    if order["status"] != "pending_payment":
        return PAY_NOT_PENDING
    if not order["payment_id"]:
        return PAY_NO_PAYMENT

    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(payments.check_payment, order["payment_id"]),
            timeout=PAYMENT_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("ЮKassa не ответила за %s сек. при проверке заказа #%s", PAYMENT_REQUEST_TIMEOUT, order_id)
        return PAY_TIMEOUT
    except Exception:
        logger.exception("Ошибка проверки платежа для заказа #%s", order_id)
        return PAY_ERROR

    if status == "succeeded":
        # Атомарно: при двойном нажатии уведомление уйдёт только один раз
        if not await db.mark_order_paid(order_id):
            return PAY_ALREADY_PROCESSED
        fresh = await db.get_order(order_id)
        items = await db.get_order_items(order_id)
        await notify_admin(
            bot,
            admin_order_text(fresh, items, f"💰 Заказ #{order_id} ОПЛАЧЕН!"),
            order_manage_keyboard(order_id, "new"),
        )
        return PAY_PAID

    if status in ("pending", "waiting_for_capture"):
        return PAY_PENDING

    # canceled — платёж отменён или истёк
    if not await db.cancel_unpaid_order(order_id):
        return PAY_ALREADY_PROCESSED
    await notify_admin(
        bot,
        f"❌ {source_tag(order)}Платёж по заказу #{order_id} отменён/истёк (статус ЮKassa: {status}).\n"
        "Заказ отменён автоматически.",
    )
    return PAY_CANCELLED
