"""
Telegram-бот магазина SSARAFOS.

Запуск: python bot.py
Схема: пользователь собирает корзину → оформляет заказ → платит через ЮKassa
(если ключи заданы) → заказ уходит администратору → админ ведёт заказ
по статусам «Новый → В пути → Доставлен».
"""
import asyncio
import logging
import warnings
from typing import Final

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.warnings import PTBUserWarning

import database as db
import payments
import pricing
from config import BOT_TOKEN, ADMIN_ID, SUPPORT_USERNAME
from keyboards import (
    BTN_ADMIN_ADD_PRODUCT,
    BTN_ADMIN_ORDERS,
    BTN_ADMIN_PRODUCTS,
    BTN_ADMIN_USER_MODE,
    BTN_CATALOG,
    BTN_CART,
    BTN_MY_ORDERS,
    BTN_SUPPORT,
    CURRENT_STATUSES,
    admin_menu,
    cart_keyboard,
    catalog_keyboard,
    confirm_order_keyboard,
    main_menu,
    order_manage_keyboard,
    orders_admin_keyboard,
    payment_keyboard,
    product_keyboard,
    products_manage_keyboard,
    single_product_manage_keyboard,
    status_label,
    user_order_keyboard,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Гасим известное предупреждение PTB про per_message в ConversationHandler:
# у нас состояния хранятся в памяти на время сессии, callback'ы внутри
# диалога отсутствуют — поведение нас устраивает.
warnings.filterwarnings("ignore", category=PTBUserWarning)

# ── Состояния диалогов ────────────────────────────────────────────────────────
CHECKOUT_ADDRESS: Final = 1
CHECKOUT_COMMENT: Final = 2
CHECKOUT_CONFIRM: Final = 3

ADD_PRODUCT_NAME: Final = 10
ADD_PRODUCT_DESC: Final = 11
ADD_PRODUCT_PRICE: Final = 12
ADD_PRODUCT_PHOTO: Final = 13
ADD_PRODUCT_COLLECTION: Final = 14

ADD_TRACK: Final = 20

# Сколько ждать ответ ЮKassa. Библиотека yookassa ходит по сети блокирующе,
# поэтому вызов уходит в отдельный поток с таймаутом — иначе бот зависнет.
PAYMENT_REQUEST_TIMEOUT: Final = 20

NO_WORDS = {"нет", "не", "пропустить", "пропуск", "skip", "no", "-", "—", "без"}


# ── Утилиты ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def is_skip(text: str) -> bool:
    return text.strip().lower() in NO_WORDS


async def safe_edit(query, text: str, reply_markup=None) -> None:
    """Редактирует сообщение, переживая случаи 'message is not modified' и потерю фото."""
    try:
        if query.message and query.message.photo:
            await query.message.delete()
            await query.message.chat.send_message(text, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            await query.message.chat.send_message(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Не удалось отредактировать сообщение")


def build_admin_order_text(order_id, user, summary, address, comment, items, status_note="") -> str:
    username = getattr(user, "username", "") or ""
    full_name = getattr(user, "full_name", "") or ""

    odd = "\n" if not status_note else f"\n{status_note}\n"
    text = (
        f"🔔 Новый заказ #{order_id}{odd}\n"
        f"👤 От: {full_name} (@{username if username else 'без username'})\n"
        f"📍 Адрес: {address}\n"
    )
    if comment:
        text += f"💬 Комментарий: {comment}\n"

    text += "\n" + pricing.format_cart_lines(items) + "\n\n"
    if summary.discount_percent > 0:
        text += f"🎁 Скидка {summary.discount_percent}%: −{summary.discount_amount} ₽\n"
    text += f"🚚 Доставка: {'бесплатно' if summary.free_delivery else f'{summary.delivery} ₽'}\n"
    text += f"💰 Итого: {summary.total} ₽"
    return text


# ── Fallback-и (регистрируются последними) ────────────────────────────────────

async def stale_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(
        "⚠️ Сессия оформления истекла (бот перезапускался).\n\n"
        "Откройте «🛒 Корзина» и оформите заказ заново.",
        show_alert=True,
    )


async def stale_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Сессия оформления уже неактивна 🙂", show_alert=True)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит клики по кнопкам из старых сообщений (после перезапуска бота)."""
    await update.callback_query.answer("Кнопка устарела. Откройте меню заново 🙂", show_alert=True)


async def to_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "Главное меню 👇")
    await query.message.chat.send_message(
        "Выберите раздел:",
        reply_markup=admin_menu() if is_admin(query.from_user.id) else main_menu(),
    )


# ── Старт и меню ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if is_admin(user.id):
        await update.message.reply_text(
            f"Здравствуйте, {user.first_name}! Вы администратор 👑\n\n"
            "Выберите действие в меню ниже.\n"
            "Чтобы посмотреть магазин глазами покупателя — «👤 Режим пользователя».",
            reply_markup=admin_menu(),
        )
    else:
        await update.message.reply_text(
            f"Здравствуйте, {user.first_name}! 👋\n"
            "Добро пожаловать в магазин брелоков Ssarafos\n\n"
            "Выберите нужный раздел:",
            reply_markup=main_menu(),
        )


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return
    await update.message.reply_text("👑 Панель администратора", reply_markup=admin_menu())


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Главное меню администратора 👑", reply_markup=admin_menu())
    else:
        await update.message.reply_text("Главное меню", reply_markup=main_menu())


async def user_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ смотрит магазин как покупатель."""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👤 Вы в режиме пользователя.\n\n"
        "Чтобы вернуться в админ-панель — отправьте /admin",
        reply_markup=main_menu(),
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if SUPPORT_USERNAME:
        text = f"📞 Поддержка\n\nПо всем вопросам пишите: @{SUPPORT_USERNAME}"
    else:
        text = "📞 Поддержка\n\nНапишите нам, и мы ответим в ближайшее время."
    await update.message.reply_text(text, reply_markup=main_menu())


# ── Каталог ───────────────────────────────────────────────────────────────────

async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = await db.get_all_products()
    if not products:
        await update.message.reply_text("Каталог пока пуст 📦", reply_markup=main_menu())
        return
    await update.message.reply_text(
        "🛍 Каталог товаров:\nВыберите товар для подробностей",
        reply_markup=catalog_keyboard(products),
    )


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product:
        await safe_edit(query, "Товар не найден 😢")
        return

    text = f"📦 {product['name']}\n\n{product['description']}\n\n💰 Цена: {product['price']} ₽\n"
    if product["collection_name"]:
        text += f"🏷 Коллекция: {product['collection_name']}\n"

    keyboard = product_keyboard(product_id)

    if product["photo_id"]:
        # Пытаемся заменить текущее сообщение на фото с подписью
        try:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=text, reply_markup=keyboard)
                return
        except Exception:
            pass
        try:
            await query.message.chat.send_photo(photo=product["photo_id"], caption=text, reply_markup=keyboard)
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        except Exception:
            logger.exception("Не удалось отправить фото товара")

    # Без фото — обычный текст (при необходимости убираем старое фото)
    await safe_edit(query, text, keyboard)


async def catalog_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    products = await db.get_all_products()
    if not products:
        await safe_edit(query, "Каталог пока пуст 📦")
        await query.message.chat.send_message("Главное меню:", reply_markup=main_menu())
        return
    await safe_edit(
        query,
        "🛍 Каталог товаров:\nВыберите товар для подробностей",
        catalog_keyboard(products),
    )


# ── Корзина ───────────────────────────────────────────────────────────────────

def _cart_screen(items) -> tuple[str, pricing.CartSummary]:
    summary = pricing.calculate(items)
    return pricing.render_summary(summary, items), summary


async def cart_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Добавлено в корзину ✅")
    product_id = int(query.data.split(":")[2])
    await db.add_to_cart(query.from_user.id, product_id)


async def cart_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = await db.get_cart(update.effective_user.id)
    if not items:
        await update.message.reply_text("Ваша корзина пуста 🛒", reply_markup=main_menu())
        return
    text, summary = _cart_screen(items)
    await update.message.reply_text(text, reply_markup=cart_keyboard(items, summary))


async def cart_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Убрано из корзины ✅")

    cart_item_id = int(query.data.split(":")[2])
    user_id = query.from_user.id
    await db.remove_from_cart(cart_item_id)

    items = await db.get_cart(user_id)
    if not items:
        await safe_edit(query, "Ваша корзина пуста 🛒")
        await query.message.chat.send_message("Меню:", reply_markup=main_menu())
        return

    text, summary = _cart_screen(items)
    await safe_edit(query, text, cart_keyboard(items, summary))


async def cart_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Корзина очищена ✅")
    await db.clear_cart(query.from_user.id)
    await safe_edit(query, "Корзина очищена 🛒")
    await query.message.chat.send_message("Меню:", reply_markup=main_menu())


async def delivery_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(pricing.rules_text(), show_alert=True)


# ── Оформление заказа ─────────────────────────────────────────────────────────

async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    items = await db.get_cart(query.from_user.id)
    if not items:
        await safe_edit(query, "Ваша корзина пуста 🛒")
        return ConversationHandler.END

    # Фиксируем состав корзины на момент оформления
    context.user_data["checkout_items"] = [dict(item) for item in items]
    context.user_data.pop("checkout_address", None)
    context.user_data.pop("checkout_comment", None)

    await safe_edit(
        query,
        "📍 Укажите адрес доставки:\n\nПример: г. Москва, ул. Ленина 10, кв. 5",
    )
    return CHECKOUT_ADDRESS


async def checkout_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.message.text.strip()
    if len(address) < 10:
        await update.message.reply_text(
            "Адрес слишком короткий. Укажите, пожалуйста, полный адрес доставки:"
        )
        return CHECKOUT_ADDRESS

    context.user_data["checkout_address"] = address
    await update.message.reply_text(
        "💬 Хотите добавить комментарий к заказу?\n\n"
        "Например: позвонить за час, оставить у двери и т.д.\n\n"
        "Или напишите «нет» / «пропустить», чтобы продолжить без комментария."
    )
    return CHECKOUT_COMMENT


async def checkout_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment_text = "" if is_skip(update.message.text) else update.message.text.strip()
    context.user_data["checkout_comment"] = comment_text

    items = context.user_data.get("checkout_items", [])
    address = context.user_data.get("checkout_address", "")

    summary = pricing.calculate(items)
    confirmation = pricing.render_summary(summary, items, title="🧾 Проверьте заказ:")
    confirmation += f"\n\n📍 Адрес: {address}\n"
    if comment_text:
        confirmation += f"💬 Комментарий: {comment_text}\n"
    confirmation += "\n⚠️ Подтвердите заказ или отмените его"

    await update.message.reply_text(confirmation, reply_markup=confirm_order_keyboard())
    return CHECKOUT_CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    items = context.user_data.get("checkout_items", [])
    address = context.user_data.get("checkout_address", "")
    comment = context.user_data.get("checkout_comment", "")

    if not items:
        await safe_edit(query, "Ошибка: корзина пуста")
        context.user_data.clear()
        return ConversationHandler.END

    summary = pricing.calculate(items)
    username = user.username or ""
    full_name = user.full_name or ""

    payment_enabled = payments.is_configured()
    order_id = await db.create_order(
        user_id=user.id,
        username=username,
        full_name=full_name,
        address=address,
        comment=comment,
        total=summary.total,
        delivery_cost=summary.delivery,
        items=items,
        status="pending_payment" if payment_enabled else "new",
    )
    await db.clear_cart(user.id)

    # ── Вариант 1: с онлайн-оплатой ──
    if payment_enabled:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    payments.create_payment,
                    float(summary.total),
                    order_id,
                    user.id,
                    f"Заказ #{order_id} — магазин Ssarafos",
                ),
                timeout=PAYMENT_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("ЮKassa не ответила за %s сек. (заказ #%s)", PAYMENT_REQUEST_TIMEOUT, order_id)
            await db.update_order_status(order_id, "new")
            await safe_edit(
                query,
                f"✅ Заказ #{order_id} создан!\n\n"
                "⚠️ Платёжная система не ответила вовремя, ссылку на оплату получить не удалось.\n"
                "Мы свяжемся с Вами для подтверждения оплаты.",
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=build_admin_order_text(
                    order_id, user, summary, address, comment, items,
                    status_note="⚠️ ЮKassa не ответила при создании платежа",
                ),
                reply_markup=order_manage_keyboard(order_id, "new"),
            )
            context.user_data.clear()
            return ConversationHandler.END
        except Exception as e:
            logger.exception("Не удалось создать платёж в ЮKassa: %s", e)
            await db.update_order_status(order_id, "new")
            await safe_edit(
                query,
                f"✅ Заказ #{order_id} создан!\n\n"
                "⚠️ Не удалось создать ссылку на оплату.\n"
                "Мы свяжемся с Вами для подтверждения.",
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=build_admin_order_text(
                    order_id, user, summary, address, comment, items,
                    status_note="⚠️ Ошибка создания платежа ЮKassa",
                ),
                reply_markup=order_manage_keyboard(order_id, "new"),
            )
            context.user_data.clear()
            return ConversationHandler.END

        await db.set_payment_info(order_id, result["payment_id"], result["confirmation_url"])

        await safe_edit(query, "💳 Заказ оформлен! Осталось оплатить.")
        await query.message.chat.send_message(
            f"🧾 Заказ #{order_id} на сумму {summary.total} ₽\n\n"
            "Нажмите «Оплатить», затем вернитесь сюда и нажмите «Я оплатил».",
            reply_markup=payment_keyboard(result["confirmation_url"], order_id),
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=build_admin_order_text(
                order_id, user, summary, address, comment, items,
                status_note="⏳ Ожидает оплаты",
            ),
            reply_markup=order_manage_keyboard(order_id, "pending_payment"),
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ── Вариант 2: без онлайн-оплаты ──
    await safe_edit(
        query,
        f"✅ Заказ #{order_id} создан!\n\n"
        "Спасибо за покупку! Мы свяжемся с Вами в ближайшее время для подтверждения.\n\n"
        "Отслеживайте статус в разделе «📦 Мои заказы»",
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=build_admin_order_text(order_id, user, summary, address, comment, items),
        reply_markup=order_manage_keyboard(order_id, "new"),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await safe_edit(query, "❌ Оформление заказа отменено")
    await query.message.chat.send_message("Меню:", reply_markup=main_menu())
    return ConversationHandler.END


# ── Проверка оплаты ───────────────────────────────────────────────────────────

async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = int(query.data.split(":")[1])
    order = await db.get_order(order_id)

    if not order or not order["payment_id"]:
        await query.answer("Платёж не найден. Обратитесь в поддержку.", show_alert=True)
        return

    if order["status"] != "pending_payment":
        await query.answer("Заказ уже оплачен ✅", show_alert=True)
        return

    await query.answer("Проверяю оплату…")

    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(payments.check_payment, order["payment_id"]),
            timeout=PAYMENT_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("ЮKassa не ответила за %s сек. при проверке заказа #%s", PAYMENT_REQUEST_TIMEOUT, order_id)
        await query.answer("ЮKassa сейчас не отвечает. Попробуйте через минуту.", show_alert=True)
        return
    except Exception:
        logger.exception("Ошибка проверки платежа для заказа #%s", order_id)
        await query.answer("Ошибка проверки платежа. Попробуйте позже.", show_alert=True)
        return

    if status == "succeeded":
        await db.mark_order_paid(order_id)
        items = await db.get_order_items(order_id)

        await safe_edit(
            query,
            f"✅ Оплата подтверждена! Заказ #{order_id} принят в работу.\n\n"
            "Мы свяжемся с Вами для уточнения деталей доставки.",
        )

        summary = pricing.CartSummary(
            subtotal=sum(int(i["price"]) * int(i["quantity"]) for i in items),
            quantity=sum(int(i["quantity"]) for i in items),
            delivery=int(order["delivery_cost"] or 0),
            discount_percent=0,
            discount_amount=0,
            total=int(order["total"]),
        )
        summary.discount_amount = summary.subtotal + summary.delivery - summary.total
        if summary.subtotal > 0 and summary.discount_amount > 0:
            summary.discount_percent = round(summary.discount_amount / summary.subtotal * 100)

        text = (
            f"💰 Заказ #{order_id} ОПЛАЧЕН!\n\n"
            f"👤 {order['full_name']} (@{order['username'] if order['username'] else 'без username'})\n"
            f"📍 {order['address']}\n"
        )
        if order["comment"]:
            text += f"💬 {order['comment']}\n"
        text += "\n" + pricing.format_cart_lines(items) + "\n\n"
        if summary.discount_percent > 0:
            text += f"🎁 Скидка {summary.discount_percent}%: −{summary.discount_amount} ₽\n"
        text += f"🚚 Доставка: {'бесплатно' if summary.delivery == 0 else f'{summary.delivery} ₽'}\n"
        text += f"💰 Итого: {order['total']} ₽"

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            reply_markup=order_manage_keyboard(order_id, "new"),
        )

    elif status in ("pending", "waiting_for_capture"):
        await safe_edit(
            query,
            "⏳ Оплата ещё не поступила. Если Вы только что оплатили — подождите минуту и попробуйте снова.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Проверить снова", callback_data=f"check_pay:{order_id}"),
            ]]),
        )
    else:
        await safe_edit(
            query,
            f"❌ Платёж отменён или истёк (статус: {status}).\n\n"
            "Создайте заказ заново или обратитесь в поддержку.",
        )


# ── Мои заказы ────────────────────────────────────────────────────────────────

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_callback = update.callback_query is not None
    if is_callback:
        await update.callback_query.answer()

    orders = await db.get_user_orders(update.effective_user.id)

    if not orders:
        msg = "У Вас пока нет заказов 📦"
        if is_callback:
            await safe_edit(update.callback_query, msg)
        else:
            await update.message.reply_text(msg, reply_markup=main_menu())
        return

    current, history = [], []
    for order in orders:
        (current if order["status"] in CURRENT_STATUSES else history).append(order)

    text = "📦 Ваши заказы:\n"

    if current:
        text += "\n🔵 Текущие заказы:\n"
        for o in current:
            text += f"\n{status_label(o['status'])}\n   Заказ #{o['id']} — 💰 {o['total']} ₽\n"
            if o["track_number"]:
                text += f"   🔍 Трек: {o['track_number']}\n"

    if history:
        text += "\n\n📋 История заказов:\n"
        for o in history:
            text += f"\n{status_label(o['status'])}\n   Заказ #{o['id']} — 💰 {o['total']} ₽\n"

    rows = []
    for o in current:
        if o["status"] == "shipped":
            rows.append([InlineKeyboardButton(
                f"✅ Я получил заказ #{o['id']}", callback_data=f"user:order_received:{o['id']}"
            )])
        elif o["status"] == "pending_payment" and o["payment_url"]:
            rows.append([InlineKeyboardButton(f"💳 Оплатить заказ #{o['id']}", url=o["payment_url"])])
            rows.append([InlineKeyboardButton(
                f"✅ Я оплатил #{o['id']}", callback_data=f"check_pay:{o['id']}"
            )])

    rows.append([InlineKeyboardButton("🏠 В главное меню", callback_data="to_main_menu")])
    markup = InlineKeyboardMarkup(rows)

    if is_callback:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def user_order_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Спасибо за подтверждение! 🎉")

    order_id = int(query.data.split(":")[2])
    await db.update_order_status(order_id, "delivered")

    await safe_edit(
        query,
        f"✅ Заказ #{order_id} отмечен как доставленный!\n\n"
        "Спасибо за покупку! Будем рады видеть Вас снова 😊",
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Пользователь подтвердил получение заказа #{order_id}",
    )


# ── Админ: заказы ─────────────────────────────────────────────────────────────

async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return

    orders = await db.get_all_orders(limit=20)
    if not orders:
        await update.message.reply_text("Заказов пока нет 📦", reply_markup=admin_menu())
        return

    text = "📋 Последние заказы:\n"
    for o in orders:
        text += (
            f"\n{status_label(o['status'])}\n"
            f"   Заказ #{o['id']} — 💰 {o['total']} ₽\n"
            f"   👤 {o['full_name']} (@{o['username'] if o['username'] else 'нет'})\n"
        )
        if o["track_number"]:
            text += f"   🔍 {o['track_number']}\n"

    await update.message.reply_text(text, reply_markup=orders_admin_keyboard(orders, 10))


async def admin_view_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await safe_edit(query, "Заказ не найден")
        return

    items = await db.get_order_items(order_id)

    text = (
        f"📦 Заказ #{order_id}\n\n"
        f"Статус: {status_label(order['status'])}\n"
        f"👤 {order['full_name']} (@{order['username'] if order['username'] else 'нет'})\n"
        f"📍 {order['address']}\n"
    )
    if order["comment"]:
        text += f"💬 Комментарий: {order['comment']}\n"

    subtotal = sum(int(i["price"]) * int(i["quantity"]) for i in items)
    delivery = int(order["delivery_cost"] or 0)
    discount_amount = subtotal + delivery - int(order["total"])

    text += "\n" + pricing.format_cart_lines(items) + "\n\n"
    text += f"💰 Сумма товаров: {subtotal} ₽\n"
    if discount_amount > 0:
        percent = round(discount_amount / subtotal * 100) if subtotal else 0
        text += f"🎁 Скидка {percent}%: −{discount_amount} ₽\n"
    text += f"🚚 Доставка: {'бесплатно' if delivery == 0 else f'{delivery} ₽'}\n"
    text += f"💰 Итого: {order['total']} ₽\n"
    if order["track_number"]:
        text += f"🔍 Трек: {order['track_number']}\n"
    text += f"\n📅 {order['created_at']}"

    await safe_edit(query, text, order_manage_keyboard(order_id, order["status"]))


async def admin_order_delivered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer("Заказ отмечен доставленным ✅")

    order_id = int(query.data.split(":")[2])
    await db.update_order_status(order_id, "delivered")
    order = await db.get_order(order_id)

    try:
        await query.message.edit_reply_markup(reply_markup=order_manage_keyboard(order_id, "delivered"))
    except Exception:
        pass

    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=f"✅ Ваш заказ #{order_id} доставлен!\n\n"
                 "Спасибо за покупку! Будем рады видеть Вас снова 😊",
        )
    except Exception:
        logger.warning("Не удалось уведомить пользователя о доставке заказа #%s", order_id)


async def admin_order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer("Заказ отменён ❌")

    order_id = int(query.data.split(":")[2])
    await db.update_order_status(order_id, "cancelled")
    order = await db.get_order(order_id)

    try:
        await query.message.edit_reply_markup(reply_markup=order_manage_keyboard(order_id, "cancelled"))
    except Exception:
        pass

    support_line = f"\n\nПо вопросам обращайтесь в поддержку: @{SUPPORT_USERNAME}" if SUPPORT_USERNAME else ""
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=f"❌ Ваш заказ #{order_id} отменён.{support_line}",
        )
    except Exception:
        logger.warning("Не удалось уведомить пользователя об отмене заказа #%s", order_id)


# ── Админ: трек-номер ─────────────────────────────────────────────────────────

async def admin_track_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    order_id = int(query.data.split(":")[2])
    context.user_data["track_order_id"] = order_id

    await safe_edit(
        query,
        f"🚚 Введите трек-номер для заказа #{order_id}:\n\n"
        "После этого статус заказа станет «В пути».",
    )
    return ADD_TRACK


async def admin_track_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    track_number = update.message.text.strip()
    order_id = context.user_data.get("track_order_id")
    if not order_id:
        await update.message.reply_text("Ошибка: заказ не найден", reply_markup=admin_menu())
        return ConversationHandler.END

    await db.update_track_number(order_id, track_number)
    order = await db.get_order(order_id)

    await update.message.reply_text(
        f"✅ Трек-номер добавлен!\nЗаказ #{order_id} переведён в статус «В пути».",
        reply_markup=admin_menu(),
    )

    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=f"🚚 Ваш заказ #{order_id} отправлен!\n\n"
                 f"Трек-номер: {track_number}\n\n"
                 "Отслеживайте статус в разделе «📦 Мои заказы»",
            reply_markup=user_order_keyboard(order_id, "shipped"),
        )
    except Exception:
        logger.warning("Не удалось уведомить пользователя об отправке заказа #%s", order_id)

    context.user_data.pop("track_order_id", None)
    return ConversationHandler.END


async def admin_track_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("track_order_id", None)
    await update.message.reply_text("❌ Добавление трек-номера отменено", reply_markup=admin_menu())
    return ConversationHandler.END


# ── Админ: добавление товара ──────────────────────────────────────────────────

async def admin_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return ConversationHandler.END
    context.user_data.pop("product_photo", None)
    await update.message.reply_text("📦 Добавление товара\n\n1️⃣ Введите название товара:")
    return ADD_PRODUCT_NAME


async def admin_add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["product_name"] = update.message.text.strip()
    await update.message.reply_text("2️⃣ Введите описание товара:")
    return ADD_PRODUCT_DESC


async def admin_add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["product_desc"] = update.message.text.strip()
    await update.message.reply_text("3️⃣ Введите цену (только число, например 1500):")
    return ADD_PRODUCT_PRICE


async def admin_add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace("₽", "").replace(" ", "")
    try:
        price = int(raw)
    except ValueError:
        await update.message.reply_text("❌ Введите корректное число. Попробуйте ещё раз:")
        return ADD_PRODUCT_PRICE

    if price <= 0:
        await update.message.reply_text("❌ Цена должна быть больше нуля. Попробуйте ещё раз:")
        return ADD_PRODUCT_PRICE

    context.user_data["product_price"] = price
    await update.message.reply_text("4️⃣ Отправьте фото товара или напишите «нет», чтобы пропустить:")
    return ADD_PRODUCT_PHOTO


async def admin_add_product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        context.user_data["product_photo"] = update.message.photo[-1].file_id
    elif update.message.text and is_skip(update.message.text):
        context.user_data["product_photo"] = None
    else:
        await update.message.reply_text("Отправьте фото или напишите «нет»:")
        return ADD_PRODUCT_PHOTO

    await update.message.reply_text("5️⃣ Введите название коллекции или напишите «нет»:")
    return ADD_PRODUCT_COLLECTION


async def admin_add_product_collection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    collection = "" if is_skip(update.message.text) else update.message.text.strip()

    name = context.user_data.get("product_name", "")
    desc = context.user_data.get("product_desc", "")
    price = context.user_data.get("product_price", 0)
    photo = context.user_data.get("product_photo")

    await db.add_product(name, desc, price, photo, collection)

    await update.message.reply_text(f"✅ Товар «{name}» добавлен в каталог!", reply_markup=admin_menu())
    context.user_data.clear()
    return ConversationHandler.END


async def admin_add_product_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Добавление товара отменено", reply_markup=admin_menu())
    return ConversationHandler.END


# ── Админ: управление товарами ────────────────────────────────────────────────

async def admin_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return

    products = await db.get_all_products(include_hidden=True)
    if not products:
        await update.message.reply_text("Товаров пока нет 📦", reply_markup=admin_menu())
        return

    await update.message.reply_text(
        "📦 Управление товарами:\n\n✅ — В продаже\n❌ — Снят с продажи",
        reply_markup=products_manage_keyboard(products),
    )


def _product_admin_text(product) -> str:
    status = "✅ В продаже" if product["in_stock"] else "❌ Снят с продажи"
    text = f"📦 {product['name']}\n\nСтатус: {status}\n💰 Цена: {product['price']} ₽\n"
    if product["collection_name"]:
        text += f"🏷 Коллекция: {product['collection_name']}\n"
    return text


async def admin_product_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await safe_edit(query, "Товар не найден")
        return

    await safe_edit(query, _product_admin_text(product),
                    single_product_manage_keyboard(product_id, product["in_stock"]))


async def admin_toggle_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Статус изменён ✅")

    parts = query.data.split(":")
    product_id = int(parts[2])
    new_value = int(parts[3])

    await db.toggle_product_stock(product_id, new_value)
    product = await db.get_product(product_id)

    await safe_edit(query, _product_admin_text(product),
                    single_product_manage_keyboard(product_id, product["in_stock"]))


async def admin_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Товар удалён ✅")

    product_id = int(query.data.split(":")[2])
    await db.delete_product(product_id)

    products = await db.get_all_products(include_hidden=True)
    if not products:
        await safe_edit(query, "Товаров больше нет 📦")
        return

    await safe_edit(
        query,
        "📦 Управление товарами:\n\n✅ — В продаже\n❌ — Снят с продажи",
        products_manage_keyboard(products),
    )


async def admin_back_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    products = await db.get_all_products(include_hidden=True)
    if not products:
        await safe_edit(query, "Товаров пока нет 📦")
        return

    await safe_edit(
        query,
        "📦 Управление товарами:\n\n✅ — В продаже\n❌ — Снят с продажи",
        products_manage_keyboard(products),
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await db.init_db()
    logger.info("База данных инициализирована ✅")
    if payments.is_configured():
        logger.info("ЮKassa: ключи найдены, онлайн-оплата включена ✅")
    else:
        logger.warning("ЮKassa: ключи не заданы, оплата отключена ⚠️")
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан — админ-панель недоступна ⚠️")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Команды ──
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_start))

    # ── Reply-меню пользователя ──
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_CATALOG}$"), catalog))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_CART}$"), cart_view))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_MY_ORDERS}$"), my_orders))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_SUPPORT}$"), support))

    # ── Reply-меню админа ──
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_ADMIN_ORDERS}$"), admin_orders))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_ADMIN_PRODUCTS}$"), admin_manage_products))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_ADMIN_USER_MODE}$"), user_mode))

    # ── Каталог ──
    application.add_handler(CallbackQueryHandler(show_product, pattern=r"^product:\d+$"))
    application.add_handler(CallbackQueryHandler(catalog_back, pattern=r"^catalog:back$"))

    # ── Корзина ──
    application.add_handler(CallbackQueryHandler(cart_add, pattern=r"^cart:add:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_remove, pattern=r"^cart:remove:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_clear, pattern=r"^cart:clear$"))
    application.add_handler(CallbackQueryHandler(delivery_info_callback, pattern=r"^delivery_info$"))

    # ── Оформление заказа ──
    checkout_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(checkout_start, pattern=r"^checkout$")],
        states={
            CHECKOUT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_address)],
            CHECKOUT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_comment)],
            CHECKOUT_CONFIRM: [
                CallbackQueryHandler(confirm_order, pattern=r"^confirm_order$"),
                CallbackQueryHandler(cancel_order, pattern=r"^cancel_order$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("admin", admin_start),
        ],
        name="checkout",
        persistent=False,
    )
    application.add_handler(checkout_conversation)

    # ── ЮKassa: проверка оплаты ──
    application.add_handler(CallbackQueryHandler(check_payment_callback, pattern=r"^check_pay:\d+$"))

    # ── Действия пользователя ──
    application.add_handler(CallbackQueryHandler(user_order_received, pattern=r"^user:order_received:\d+$"))
    application.add_handler(CallbackQueryHandler(my_orders, pattern=r"^my_orders_back$"))
    application.add_handler(CallbackQueryHandler(to_main_menu_callback, pattern=r"^to_main_menu$"))

    # ── Управление заказами (админ) ──
    application.add_handler(CallbackQueryHandler(admin_view_order, pattern=r"^admin:view_order:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_order_delivered, pattern=r"^admin:order_delivered:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_order_cancel, pattern=r"^admin:order_cancel:\d+$"))

    # ── Добавление трек-номера ──
    track_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_track_start, pattern=r"^admin:order_track:\d+$")],
        states={ADD_TRACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_track_save)]},
        fallbacks=[
            CommandHandler("start", admin_track_cancel),
            CommandHandler("admin", admin_track_cancel),
        ],
        name="add_track",
        persistent=False,
    )
    application.add_handler(track_conversation)

    # ── Добавление товара ──
    add_product_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_ADMIN_ADD_PRODUCT}$"), admin_add_product_start)],
        states={
            ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_name)],
            ADD_PRODUCT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_desc)],
            ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_price)],
            ADD_PRODUCT_PHOTO: [
                MessageHandler(filters.PHOTO, admin_add_product_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_photo),
            ],
            ADD_PRODUCT_COLLECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_collection)],
        },
        fallbacks=[
            CommandHandler("start", admin_add_product_cancel),
            CommandHandler("admin", admin_add_product_cancel),
        ],
        name="add_product",
        persistent=False,
    )
    application.add_handler(add_product_conversation)

    # ── Управление товарами ──
    application.add_handler(CallbackQueryHandler(admin_product_manage, pattern=r"^admin:product_manage:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_toggle_stock, pattern=r"^admin:toggle_stock:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_delete_product, pattern=r"^admin:delete_product:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_back_manage_products, pattern=r"^admin:back_manage_products$"))

    # ── Fallback-и (обязательно последними) ──
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(stale_confirm_order, pattern=r"^confirm_order$"))
    application.add_handler(CallbackQueryHandler(stale_cancel_order, pattern=r"^cancel_order$"))
    application.add_handler(CallbackQueryHandler(fallback_callback))

    logger.info("Бот запущен 🚀")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
