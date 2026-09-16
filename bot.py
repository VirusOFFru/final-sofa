"""
Telegram-бот магазина SSARAFOS.

Запуск: python bot.py
Схема: пользователь собирает корзину → оформляет заказ → платит через ЮKassa
(если ключи заданы) → заказ уходит администратору → админ ведёт заказ
по статусам «Новый → В пути → Доставлен».
"""
import asyncio
import logging
import re
import warnings
from datetime import datetime, timedelta, timezone
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
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
import services
from config import BOT_TOKEN, ADMIN_ID, SUPPORT_USERNAME, REVIEWS_URL, SYNC_API_TOKEN, normalize_url
from keyboards import (
    BTN_ADMIN_ADD_PRODUCT,
    BTN_ADMIN_ORDERS,
    BTN_ADMIN_PRODUCTS,
    BTN_ADMIN_USER_MODE,
    BTN_CATALOG,
    BTN_CART,
    BTN_MY_ORDERS,
    BTN_REVIEWS,
    BTN_SUPPORT,
    CURRENT_STATUSES,
    INSIDE_BUTTON_TEXT,
    MENU_BUTTONS,
    admin_menu,
    cart_clear_confirm_keyboard,
    cart_keyboard,
    catalog_keyboard,
    checkout_cancel_keyboard,
    confirm_order_keyboard,
    inside_admin_keyboard,
    inside_clear_confirm_keyboard,
    inside_keyboard,
    main_menu,
    order_manage_keyboard,
    orders_admin_keyboard,
    payment_keyboard,
    photos_done_keyboard,
    product_delete_confirm_keyboard,
    product_edit_cancel_keyboard,
    product_edit_keyboard,
    product_keyboard,
    products_manage_keyboard,
    reviews_keyboard,
    single_product_manage_keyboard,
    status_label,
    support_reply_cancel_keyboard,
    support_reply_keyboard,
    user_order_keyboard,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог каждый запрос к Telegram — это засоряет логи
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Гасим известное предупреждение PTB про per_message в ConversationHandler.
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
ADD_PRODUCT_INSIDE_TEXT: Final = 15
ADD_PRODUCT_INSIDE_PHOTOS: Final = 16

ADD_TRACK: Final = 20

EDIT_PRODUCT_VALUE: Final = 30
EDIT_INSIDE_PHOTOS: Final = 31

SUPPORT_REPLY: Final = 40

# «Что внутри»: максимум фото (столько Telegram показывает одним альбомом)
INSIDE_PHOTOS_LIMIT: Final = 10
# Максимальная длина текста «что внутри»
INSIDE_TEXT_LIMIT: Final = 3500
# Сколько ждать остальные фото альбома, прежде чем ответить админу (сек)
ALBUM_WAIT_SECONDS = 1.5

NO_WORDS = {"нет", "не", "пропустить", "пропуск", "skip", "no", "-", "—", "без"}

MSK = timezone(timedelta(hours=3))

CHECKOUT_KEYS = ("checkout_items", "checkout_address", "checkout_comment")
PHOTO_BUFFER_KEYS = ("inside_photos_buffer", "inside_photos_skipped", "inside_album_pending")
ADD_PRODUCT_KEYS = ("product_name", "product_desc", "product_price", "product_photo",
                    "product_collection", "product_inside_text") + PHOTO_BUFFER_KEYS
EDIT_PRODUCT_KEYS = ("edit_product_id", "edit_product_field") + PHOTO_BUFFER_KEYS
TRACK_KEYS = ("track_order_id",)
SUPPORT_KEYS = ("support_reply_to",)


# ── Активный диалог ───────────────────────────────────────────────────────────
# Бот ведёт несколько пошаговых диалогов (оформление заказа, трек-номер,
# добавление и изменение товара). Чтобы брошенный на полпути диалог не
# «перехватывал» текст, предназначенный другому, помним, какой диалог
# у пользователя активен СЕЙЧАС. Текст принимает только он.

FLOW_CHECKOUT: Final = "checkout"
FLOW_TRACK: Final = "track"
FLOW_ADD_PRODUCT: Final = "add_product"
FLOW_EDIT_PRODUCT: Final = "edit_product"
FLOW_SUPPORT_REPLY: Final = "support_reply"

ACTIVE_FLOW: dict[int, str] = {}


def set_flow(user_id: int, flow: str) -> None:
    ACTIVE_FLOW[user_id] = flow


def end_flow(user_id: int, flow: str | None = None) -> None:
    """Завершает активный диалог (любой, либо только указанный)."""
    if flow is None or ACTIVE_FLOW.get(user_id) == flow:
        ACTIVE_FLOW.pop(user_id, None)


def _pop_keys(context: ContextTypes.DEFAULT_TYPE, keys) -> None:
    for key in keys:
        context.user_data.pop(key, None)


def reset_all_flows(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Пользователь ушёл в меню — незаконченный ввод текста сбрасывается.
    Данные оформления заказа не трогаем: уже показанную сводку заказа
    можно подтвердить и позже (корзина при этом перепроверяется).
    """
    end_flow(user_id)
    _pop_keys(context, ADD_PRODUCT_KEYS + EDIT_PRODUCT_KEYS + TRACK_KEYS + SUPPORT_KEYS)


class FlowFilter(filters.MessageFilter):
    """Пропускает сообщение, только если у отправителя активен нужный диалог."""

    def __init__(self, flow: str):
        super().__init__(name=f"FlowFilter({flow})")
        self.flow = flow

    def filter(self, message) -> bool:
        user = message.from_user
        return user is not None and ACTIVE_FLOW.get(user.id) == self.flow


# Только новые сообщения (не отредактированные) — иначе update.message = None
NEW_MSG = filters.UpdateType.MESSAGE
MENU_FILTER = filters.Regex("^(" + "|".join(re.escape(b) for b in MENU_BUTTONS) + ")$")
# Текст, который можно считать вводом пользователя в диалоге
TEXT_INPUT = NEW_MSG & filters.TEXT & ~filters.COMMAND & ~MENU_FILTER


def menu_button(text: str):
    return NEW_MSG & filters.Regex(f"^{re.escape(text)}$")


# ── Утилиты ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def is_skip(text: str) -> bool:
    return text.strip().lower() in NO_WORDS


def menu_for(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Админу — админ-меню (если он не в «режиме пользователя»), остальным — обычное."""
    if is_admin(user_id) and not context.user_data.get("user_mode"):
        return admin_menu()
    return main_menu()


def now_msk() -> str:
    return datetime.now(MSK).strftime("%H:%M:%S")


REVIEWS_LINK = normalize_url(REVIEWS_URL)


async def safe_edit(query, text: str, reply_markup=None) -> None:
    """Редактирует сообщение, переживая 'message is not modified' и сообщения с фото."""
    message = query.message
    try:
        if message is not None and getattr(message, "photo", None):
            # Текст к фото не превратить в обычное сообщение — пересоздаём
            await message.chat.send_message(text, reply_markup=reply_markup)
            try:
                await message.delete()
            except Exception:
                pass
        else:
            await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return  # текст и кнопки не изменились — это не ошибка
        await _send_instead(query, text, reply_markup)
    except Exception:
        await _send_instead(query, text, reply_markup)


def chat_id_of(query) -> int:
    return query.message.chat.id if query.message is not None else query.from_user.id


async def _send_instead(query, text: str, reply_markup=None) -> None:
    try:
        await query.get_bot().send_message(chat_id_of(query), text, reply_markup=reply_markup)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s", query.from_user.id)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    await services.notify_admin(context.bot, text, reply_markup)


async def deny_if_not_admin(query) -> bool:
    """True — если НЕ админ (и ему уже показан отказ)."""
    if is_admin(query.from_user.id):
        return False
    await query.answer("Нет доступа", show_alert=True)
    return True


# ── Fallback-и (регистрируются последними) ────────────────────────────────────

async def stale_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(
        "⚠️ Этот заказ уже оформлен или сессия оформления устарела.\n\n"
        "Статус — в «📦 Мои заказы». Чтобы оформить новый заказ, откройте «🛒 Корзина».",
        show_alert=True,
    )


async def stale_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Сессия оформления уже неактивна 🙂", show_alert=True)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит клики по кнопкам из старых сообщений (после перезапуска бота)."""
    await update.callback_query.answer("Кнопка устарела. Откройте меню заново 🙂", show_alert=True)


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст/фото, которые не относятся ни к одному активному действию."""
    await update.message.reply_text(
        "🤔 Не понял. Воспользуйтесь кнопками меню ниже 👇",
        reply_markup=menu_for(update.effective_user.id, context),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления", exc_info=context.error)


async def to_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    reset_all_flows(query.from_user.id, context)
    await safe_edit(query, "Главное меню 👇")
    await query.get_bot().send_message(
        chat_id_of(query),
        "Выберите раздел:",
        reply_markup=menu_for(query.from_user.id, context),
    )


# ── Старт и меню ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    reset_all_flows(user.id, context)
    context.user_data.pop("user_mode", None)

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
    return ConversationHandler.END


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    reset_all_flows(user_id, context)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет доступа")
        return ConversationHandler.END
    context.user_data.pop("user_mode", None)
    await update.message.reply_text("👑 Панель администратора", reply_markup=admin_menu())
    return ConversationHandler.END


async def user_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ смотрит магазин как покупатель."""
    if not is_admin(update.effective_user.id):
        return
    reset_all_flows(update.effective_user.id, context)
    context.user_data["user_mode"] = True
    await update.message.reply_text(
        "👤 Вы в режиме пользователя.\n\n"
        "Чтобы вернуться в админ-панель — отправьте /admin",
        reply_markup=main_menu(),
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_all_flows(update.effective_user.id, context)
    if SUPPORT_USERNAME:
        text = f"📞 Поддержка\n\nПо всем вопросам пишите: @{SUPPORT_USERNAME}"
    else:
        text = "📞 Поддержка\n\nНапишите нам, и мы ответим в ближайшее время."
    await update.message.reply_text(text, reply_markup=main_menu())


async def reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_all_flows(update.effective_user.id, context)

    if not REVIEWS_LINK:
        await update.message.reply_text("⭐ Отзывы\n\nРаздел скоро появится!", reply_markup=main_menu())
        return

    text = "⭐ Отзывы наших покупателей — в нашей группе.\n\nНажмите кнопку ниже, чтобы перейти 👇"
    try:
        await update.message.reply_text(text, reply_markup=reviews_keyboard(REVIEWS_LINK))
    except BadRequest:
        # Telegram отклонил ссылку в кнопке (например, опечатка в REVIEWS_URL)
        logger.warning("Некорректная ссылка REVIEWS_URL: %s", REVIEWS_LINK)
        await update.message.reply_text(f"⭐ Отзывы наших покупателей:\n{REVIEWS_LINK}")


# ── Каталог ───────────────────────────────────────────────────────────────────

CATALOG_TEXT = "🛍 Каталог товаров:\nВыберите товар для подробностей"


async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_all_flows(update.effective_user.id, context)
    products = await db.get_all_products()
    if not products:
        await update.message.reply_text("Каталог пока пуст 📦", reply_markup=main_menu())
        return
    await update.message.reply_text(CATALOG_TEXT, reply_markup=catalog_keyboard(products))


async def delete_inside_album(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет ранее показанный альбом «что внутри», чтобы не засорять чат."""
    album = context.user_data.pop("inside_album", None)
    if not album:
        return
    chat_id, message_ids = album
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


def product_card_text(product) -> str:
    text = f"📦 {product['name']}\n\n{product['description']}\n\n💰 Цена: {product['price']} ₽\n"
    if product["collection_name"]:
        text += f"🏷 Коллекция: {product['collection_name']}\n"
    return text


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await delete_inside_album(context)

    product_id = int(query.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product or not product["in_stock"]:
        back = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад к каталогу", callback_data="catalog:back")]])
        await safe_edit(query, "Этого товара сейчас нет в продаже 😢", back)
        return

    text = product_card_text(product)
    keyboard = product_keyboard(product_id, db.has_inside(product))

    if product["photo_id"]:
        # Подпись к фото — максимум 1024 символа
        caption = text if len(text) <= 1024 else text[:1020] + "…"
        # Если уже открыто фото — просто меняем его
        if query.message is not None and getattr(query.message, "photo", None):
            try:
                await query.edit_message_media(
                    InputMediaPhoto(media=product["photo_id"], caption=caption),
                    reply_markup=keyboard,
                )
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
            except Exception:
                pass
        try:
            await query.get_bot().send_photo(
                chat_id=chat_id_of(query), photo=product["photo_id"], caption=caption, reply_markup=keyboard
            )
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        except Exception:
            logger.exception("Не удалось отправить фото товара #%s", product_id)

    # Без фото (или фото не отправилось) — обычный текст
    await safe_edit(query, text, keyboard)


async def show_inside(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экран «👀 Покажи что внутри»: альбом фото + описание комплектации."""
    query = update.callback_query
    product_id = int(query.data.split(":")[1])
    product = await db.get_product(product_id)

    admin_view = is_admin(query.from_user.id)
    if not product or (not product["in_stock"] and not admin_view):
        await query.answer("Этого товара сейчас нет в продаже 😢", show_alert=True)
        return
    if not db.has_inside(product):
        await query.answer("Для этого товара пока нет информации 🙂", show_alert=True)
        return
    await query.answer()
    await delete_inside_album(context)

    photos = await db.get_inside_photos(product_id)
    chat_id = chat_id_of(query)
    album_ids: list[int] = []

    if photos:
        try:
            if len(photos) == 1:
                sent = await context.bot.send_photo(chat_id=chat_id, photo=photos[0]["file_id"])
                album_ids = [sent.message_id]
            else:
                sent = await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(media=p["file_id"]) for p in photos[:INSIDE_PHOTOS_LIMIT]],
                )
                album_ids = [m.message_id for m in sent]
        except Exception:
            logger.exception("Не удалось отправить фото «что внутри» товара #%s", product_id)

    inside_text = (product["inside_text"] or "").strip()
    text = f"{INSIDE_BUTTON_TEXT} — «{product['name']}»"
    if inside_text:
        text += f"\n\n{inside_text}"
    elif album_ids:
        text += "\n\nФото — выше 👆"
    else:
        text += "\n\nНе удалось загрузить фото, попробуйте позже."

    keyboard = inside_keyboard(product_id)
    if album_ids:
        # Фото уже выше — старую карточку убираем, текст отправляем под альбомом
        context.user_data["inside_album"] = (chat_id, album_ids)
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        await safe_edit(query, text, keyboard)


async def catalog_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await delete_inside_album(context)

    products = await db.get_all_products()
    if not products:
        await safe_edit(query, "Каталог пока пуст 📦")
        return
    await safe_edit(query, CATALOG_TEXT, catalog_keyboard(products))


# ── Корзина ───────────────────────────────────────────────────────────────────

def _cart_screen(items) -> tuple[str, pricing.CartSummary]:
    summary = pricing.calculate(items)
    return pricing.render_summary(summary, items), summary


async def _show_cart_in_place(query, user_id: int) -> None:
    items = await db.get_cart(user_id)
    if not items:
        await safe_edit(query, "Ваша корзина пуста 🛒")
        return
    text, summary = _cart_screen(items)
    await safe_edit(query, text, cart_keyboard(items, summary))


async def cart_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    product_id = int(query.data.split(":")[2])

    product = await db.get_product(product_id)
    if not product or not product["in_stock"]:
        await query.answer("Этого товара сейчас нет в продаже 😢", show_alert=True)
        return

    await db.add_to_cart(query.from_user.id, product_id)
    await query.answer(f"«{product['name']}» добавлен в корзину ✅")


async def cart_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_all_flows(update.effective_user.id, context)
    items = await db.get_cart(update.effective_user.id)
    if not items:
        await update.message.reply_text("Ваша корзина пуста 🛒", reply_markup=main_menu())
        return
    text, summary = _cart_screen(items)
    await update.message.reply_text(text, reply_markup=cart_keyboard(items, summary))


async def cart_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Убирает из корзины ОДНУ штуку товара."""
    query = update.callback_query
    cart_item_id = int(query.data.split(":")[2])
    user_id = query.from_user.id

    removed = await db.remove_one_from_cart(cart_item_id, user_id)
    await query.answer("Убрано 1 шт. ✅" if removed else "Этого товара уже нет в корзине")
    await _show_cart_in_place(query, user_id)


async def cart_clear_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    items = await db.get_cart(query.from_user.id)
    if not items:
        await safe_edit(query, "Ваша корзина уже пуста 🛒")
        return

    count = sum(int(i["quantity"]) for i in items)
    await safe_edit(
        query,
        f"🗑 Очистить корзину?\n\n"
        f"Будут удалены все товары ({count} {pricing.plural_keychain(count)}).",
        cart_clear_confirm_keyboard(),
    )


async def cart_clear_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Корзина очищена ✅")
    await db.clear_cart(query.from_user.id)
    await safe_edit(query, "🗑 Корзина очищена.\n\nЗагляните в «🛍 Каталог», чтобы выбрать брелоки 🙂")


async def cart_clear_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _show_cart_in_place(query, query.from_user.id)


async def delivery_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(pricing.rules_text(), show_alert=True)


# ── Оформление заказа ─────────────────────────────────────────────────────────

def _snapshot(items) -> list[dict]:
    return [
        {
            "id": int(i["id"]),
            "product_id": int(i["product_id"]),
            "name": i["name"],
            "price": int(i["price"]),
            "quantity": int(i["quantity"]),
        }
        for i in items
    ]


def _signature(items) -> list[tuple]:
    return sorted((int(i["product_id"]), int(i["quantity"]), int(i["price"]), i["name"]) for i in items)


def _confirmation_text(items, address: str, comment: str) -> str:
    summary = pricing.calculate(items)
    text = pricing.render_summary(summary, items, title="🧾 Проверьте заказ:")
    text += f"\n\n📍 Адрес: {address}\n"
    if comment:
        text += f"💬 Комментарий: {comment}\n"
    text += "\n⚠️ Подтвердите заказ или отмените его"
    return text


def _finish_checkout(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_flow(user_id, FLOW_CHECKOUT)
    _pop_keys(context, CHECKOUT_KEYS)
    return ConversationHandler.END


async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    items = await db.get_cart(user_id)
    if not items:
        await safe_edit(query, "Ваша корзина пуста 🛒")
        return _finish_checkout(user_id, context)

    # Сбрасываем прочие незаконченные действия и фиксируем состав корзины
    reset_all_flows(user_id, context)
    _pop_keys(context, CHECKOUT_KEYS)
    context.user_data["checkout_items"] = _snapshot(items)
    set_flow(user_id, FLOW_CHECKOUT)

    await safe_edit(
        query,
        "📍 Укажите адрес доставки:\n\nПример: г. Москва, ул. Ленина 10, кв. 5",
        checkout_cancel_keyboard(),
    )
    return CHECKOUT_ADDRESS


async def checkout_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.message.text.strip()
    if len(address) < 10:
        await update.message.reply_text(
            "Адрес слишком короткий. Укажите, пожалуйста, полный адрес доставки:",
            reply_markup=checkout_cancel_keyboard(),
        )
        return CHECKOUT_ADDRESS

    context.user_data["checkout_address"] = address
    await update.message.reply_text(
        "💬 Хотите добавить комментарий к заказу?\n\n"
        "Например: позвонить за час, оставить у двери и т.д.\n\n"
        "Или напишите «нет» / «пропустить», чтобы продолжить без комментария.",
        reply_markup=checkout_cancel_keyboard(),
    )
    return CHECKOUT_COMMENT


async def checkout_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    comment_text = "" if is_skip(update.message.text) else update.message.text.strip()
    context.user_data["checkout_comment"] = comment_text

    # Берём актуальную корзину — цены могли поменяться
    items = await db.get_cart(user_id)
    if not items:
        await update.message.reply_text("Ваша корзина пуста 🛒", reply_markup=main_menu())
        return _finish_checkout(user_id, context)
    context.user_data["checkout_items"] = _snapshot(items)

    address = context.user_data.get("checkout_address", "")
    # Текстовый ввод закончен — дальше только кнопки
    end_flow(user_id, FLOW_CHECKOUT)
    await update.message.reply_text(
        _confirmation_text(context.user_data["checkout_items"], address, comment_text),
        reply_markup=confirm_order_keyboard(),
    )
    return CHECKOUT_CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = query.from_user

    snapshot = context.user_data.get("checkout_items")
    address = context.user_data.get("checkout_address")
    comment = context.user_data.get("checkout_comment", "")

    if not snapshot or not address:
        await query.answer()
        await safe_edit(query, "⚠️ Сессия оформления устарела.\n\nОткройте «🛒 Корзина» и оформите заказ заново.")
        return _finish_checkout(user.id, context)

    # Перепроверяем корзину: пока покупатель смотрел сводку, он мог изменить
    # корзину, а админ — цену. Оформляем строго то, что сейчас в корзине.
    fresh = await db.get_cart(user.id)
    if not fresh:
        await query.answer()
        await safe_edit(query, "Ваша корзина пуста 🛒\n\nДобавьте товары из «🛍 Каталог».")
        return _finish_checkout(user.id, context)

    if _signature(fresh) != _signature(snapshot):
        context.user_data["checkout_items"] = _snapshot(fresh)
        await query.answer("Корзина или цены изменились — проверьте заказ ещё раз 🙏", show_alert=True)
        await safe_edit(
            query,
            _confirmation_text(context.user_data["checkout_items"], address, comment),
            confirm_order_keyboard(),
        )
        return CHECKOUT_CONFIRM

    await query.answer()
    items = _snapshot(fresh)

    placed = await services.place_order(
        context.bot,
        items=items,
        address=address,
        comment=comment,
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name or "",
    )
    await db.clear_cart(user.id)
    _finish_checkout(user.id, context)

    if placed.payment_url:
        await safe_edit(
            query,
            f"💳 Заказ #{placed.order_id} оформлен! Осталось оплатить.\n\n"
            f"Сумма к оплате: {placed.total} ₽\n\n"
            "1️⃣ Нажмите «💳 Оплатить» и оплатите заказ.\n"
            "2️⃣ Вернитесь сюда и нажмите «✅ Проверить оплату».\n\n"
            "Ссылка на оплату также есть в разделе «📦 Мои заказы».",
            payment_keyboard(placed.payment_url, placed.order_id),
        )
    elif placed.payment_failed:
        await safe_edit(
            query,
            f"✅ Заказ #{placed.order_id} создан!\n\n"
            "⚠️ Не удалось получить ссылку на оплату.\n"
            "Мы свяжемся с Вами для подтверждения оплаты.",
        )
    else:
        await safe_edit(
            query,
            f"✅ Заказ #{placed.order_id} создан!\n\n"
            "Спасибо за покупку! Мы свяжемся с Вами в ближайшее время для подтверждения.\n\n"
            "Отслеживайте статус в разделе «📦 Мои заказы»",
        )
    return ConversationHandler.END


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _finish_checkout(query.from_user.id, context)
    await safe_edit(query, "❌ Оформление заказа отменено.\n\nТовары остались в корзине 🛒")
    return ConversationHandler.END


# ── Проверка оплаты ───────────────────────────────────────────────────────────

ALREADY_PROCESSED = {
    "new": "Заказ уже оплачен ✅",
    "shipped": "Заказ уже оплачен и отправлен 🚚",
    "delivered": "Заказ уже оплачен и доставлен ✅",
    "cancelled": "Этот заказ отменён ❌",
}


def _payment_screen(order, note: str) -> str:
    return (
        f"🧾 Заказ #{order['id']} на сумму {order['total']} ₽\n\n"
        f"{note}\n\n"
        f"🕒 Проверено в {now_msk()} (МСК)"
    )


async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    order_id = int(query.data.split(":")[1])
    order = await db.get_order(order_id)

    if not order or services.is_vk(order) or int(order["user_id"]) != user_id:
        await query.answer("Заказ не найден. Обратитесь в поддержку.", show_alert=True)
        return

    if order["status"] != "pending_payment":
        await query.answer(ALREADY_PROCESSED.get(order["status"], "Заказ уже обработан"), show_alert=True)
        return

    if not order["payment_id"]:
        await query.answer("Платёж не найден. Обратитесь в поддержку.", show_alert=True)
        return

    # Отвечаем сразу: запрос к ЮKassa может идти долго
    await query.answer("Проверяю оплату…")

    # Кнопки оплаты остаются ВСЕГДА, пока заказ не оплачен —
    # случайное нажатие «Проверить оплату» не должно прятать ссылку.
    pay_keyboard = payment_keyboard(order["payment_url"], order_id, with_orders_back=True)

    result = await services.check_order_payment(context.bot, order)

    if result == services.PAY_TIMEOUT:
        await safe_edit(
            query,
            _payment_screen(order, "⚠️ Платёжная система сейчас не отвечает.\nПопробуйте проверить оплату через минуту."),
            pay_keyboard,
        )
    elif result == services.PAY_ERROR:
        await safe_edit(
            query,
            _payment_screen(order, "⚠️ Не удалось проверить оплату.\nПопробуйте ещё раз через минуту."),
            pay_keyboard,
        )
    elif result == services.PAY_PENDING:
        await safe_edit(
            query,
            _payment_screen(
                order,
                "⏳ Оплата ещё не поступила.\n\n"
                "• Ещё не оплатили — нажмите «💳 Оплатить».\n"
                "• Уже оплатили — подождите минуту и нажмите «✅ Проверить оплату» ещё раз.",
            ),
            pay_keyboard,
        )
    elif result == services.PAY_PAID:
        await safe_edit(
            query,
            f"✅ Оплата подтверждена! Заказ #{order_id} принят в работу.\n\n"
            "Мы свяжемся с Вами для уточнения деталей доставки.\n"
            "Статус заказа — в разделе «📦 Мои заказы».",
        )
    elif result == services.PAY_CANCELLED:
        restored = await db.restore_cart_from_order(order_id, user_id)
        text = f"❌ Платёж по заказу #{order_id} отменён или срок оплаты истёк.\n\n"
        if restored:
            text += "Товары возвращены в корзину — откройте «🛒 Корзина», чтобы оформить заказ заново."
        else:
            text += "Оформите заказ заново или обратитесь в поддержку."
        await safe_edit(query, text)
    else:
        # Статус успел измениться (повторное нажатие / действие админа)
        fresh = await db.get_order(order_id)
        fresh_status = fresh["status"] if fresh else ""
        await safe_edit(query, f"🧾 Заказ #{order_id}\n\n{ALREADY_PROCESSED.get(fresh_status, 'Заказ уже обработан')}")


# ── Мои заказы ────────────────────────────────────────────────────────────────

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_callback = update.callback_query is not None
    if is_callback:
        await update.callback_query.answer()
    else:
        reset_all_flows(update.effective_user.id, context)

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
        # Telegram ограничивает длину сообщения — показываем последние 15
        for o in history[:15]:
            text += f"\n{status_label(o['status'])}\n   Заказ #{o['id']} — 💰 {o['total']} ₽\n"
        if len(history) > 15:
            text += f"\n…и ещё {len(history) - 15} заказ(ов)\n"

    rows = []
    for o in current:
        if o["status"] == "shipped":
            rows.append([InlineKeyboardButton(
                f"✅ Я получил заказ #{o['id']}", callback_data=f"user:order_received:{o['id']}"
            )])
        elif o["status"] == "pending_payment" and o["payment_id"]:
            if o["payment_url"]:
                rows.append([InlineKeyboardButton(f"💳 Оплатить заказ #{o['id']}", url=o["payment_url"])])
            rows.append([InlineKeyboardButton(
                f"✅ Проверить оплату #{o['id']}", callback_data=f"check_pay:{o['id']}"
            )])

    rows.append([InlineKeyboardButton("🏠 В главное меню", callback_data="to_main_menu")])
    markup = InlineKeyboardMarkup(rows)

    if is_callback:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def user_order_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)

    if not order or services.is_vk(order) or int(order["user_id"]) != query.from_user.id:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] != "shipped":
        await query.answer(ALREADY_PROCESSED.get(order["status"], "Статус заказа уже изменён"), show_alert=True)
        return

    await query.answer("Спасибо за подтверждение! 🎉")
    await db.update_order_status(order_id, "delivered")

    await safe_edit(
        query,
        f"✅ Заказ #{order_id} отмечен как доставленный!\n\n"
        "Спасибо за покупку! Будем рады видеть Вас снова 😊",
    )
    await notify_admin(context, f"✅ Пользователь подтвердил получение заказа #{order_id}")


# ── Админ: заказы ─────────────────────────────────────────────────────────────

async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return
    reset_all_flows(update.effective_user.id, context)

    orders = await db.get_all_orders(limit=20)
    if not orders:
        await update.message.reply_text("Заказов пока нет 📦", reply_markup=admin_menu())
        return

    text = "📋 Последние заказы:\n"
    for o in orders:
        customer = services.customer_line(o).replace("\n", "\n   ")
        text += (
            f"\n{status_label(o['status'])}\n"
            f"   {services.source_tag(o)}Заказ #{o['id']} — 💰 {o['total']} ₽\n"
            f"   {customer}\n"
        )
        if o["track_number"]:
            text += f"   🔍 {o['track_number']}\n"

    await update.message.reply_text(text, reply_markup=orders_admin_keyboard(orders, 10))


async def admin_view_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    end_flow(query.from_user.id)

    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await safe_edit(query, "Заказ не найден")
        return

    items = await db.get_order_items(order_id)

    text = services.admin_order_text(
        order, items, f"📦 Заказ #{order_id}", f"Статус: {status_label(order['status'])}"
    ) + "\n"
    if order["track_number"]:
        text += f"🔍 Трек: {order['track_number']}\n"
    text += f"\n📅 {order['created_at']}"

    await safe_edit(query, text, order_manage_keyboard(order_id, order["status"]))


async def admin_order_delivered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return

    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] in {"delivered", "cancelled"}:
        await query.answer(f"Заказ уже в статусе «{status_label(order['status'])}»", show_alert=True)
        return

    await db.update_order_status(order_id, "delivered")
    await query.answer("Заказ отмечен доставленным ✅")

    try:
        await query.edit_message_reply_markup(reply_markup=order_manage_keyboard(order_id, "delivered"))
    except Exception:
        pass

    await services.notify_customer(
        context.bot,
        order,
        f"✅ Ваш заказ #{order_id} доставлен!\n\nСпасибо за покупку! Будем рады видеть Вас снова 😊",
    )


async def admin_order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return

    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] in {"delivered", "cancelled"}:
        await query.answer(f"Заказ уже в статусе «{status_label(order['status'])}»", show_alert=True)
        return

    await db.update_order_status(order_id, "cancelled")
    await query.answer("Заказ отменён ❌")

    try:
        await query.edit_message_reply_markup(reply_markup=order_manage_keyboard(order_id, "cancelled"))
    except Exception:
        pass

    support_line = f"\n\nПо вопросам обращайтесь в поддержку: @{SUPPORT_USERNAME}" if SUPPORT_USERNAME else ""
    await services.notify_customer(context.bot, order, f"❌ Ваш заказ #{order_id} отменён.{support_line}")


# ── Админ: трек-номер ─────────────────────────────────────────────────────────

def _finish_track(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_flow(user_id, FLOW_TRACK)
    _pop_keys(context, TRACK_KEYS)
    return ConversationHandler.END


async def admin_track_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    if await deny_if_not_admin(query):
        return ConversationHandler.END

    order_id = int(query.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return _finish_track(user_id, context)
    await query.answer()

    reset_all_flows(user_id, context)
    context.user_data["track_order_id"] = order_id
    set_flow(user_id, FLOW_TRACK)

    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin:track_cancel:{order_id}")]]
    )
    await safe_edit(
        query,
        f"🚚 Введите трек-номер для заказа #{order_id}:\n\n"
        "После этого статус заказа станет «В пути».",
        cancel_kb,
    )
    return ADD_TRACK


async def admin_track_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return _finish_track(user_id, context)

    track_number = update.message.text.strip()
    order_id = context.user_data.get("track_order_id")
    order = await db.get_order(order_id) if order_id else None
    if not order:
        await update.message.reply_text("Ошибка: заказ не найден", reply_markup=admin_menu())
        return _finish_track(user_id, context)

    await db.update_track_number(order_id, track_number)
    _finish_track(user_id, context)

    await update.message.reply_text(
        f"✅ Трек-номер добавлен!\nЗаказ #{order_id} переведён в статус «В пути».",
        reply_markup=admin_menu(),
    )

    await services.notify_customer(
        context.bot,
        order,
        f"🚚 Ваш заказ #{order_id} отправлен!\n\n"
        f"Трек-номер: {track_number}\n\n"
        "Отслеживайте статус в разделе «📦 Мои заказы»",
        user_order_keyboard(order_id, "shipped"),
    )
    return ConversationHandler.END


async def admin_track_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return ConversationHandler.END
    await query.answer("Отменено")
    _finish_track(query.from_user.id, context)
    await safe_edit(query, "❌ Добавление трек-номера отменено")
    return ConversationHandler.END


# ── Админ: добавление товара ──────────────────────────────────────────────────

ADD_CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:add_cancel")]])
ADD_PHOTOS_KB = photos_done_keyboard("admin:add_inside_done", "admin:add_cancel")


def _finish_add_product(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_flow(user_id, FLOW_ADD_PRODUCT)
    _pop_keys(context, ADD_PRODUCT_KEYS)
    return ConversationHandler.END


async def admin_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет доступа")
        return ConversationHandler.END

    reset_all_flows(user_id, context)
    set_flow(user_id, FLOW_ADD_PRODUCT)
    await update.message.reply_text(
        "📦 Добавление товара\n\n1️⃣ Введите название товара:",
        reply_markup=ADD_CANCEL_KB,
    )
    return ADD_PRODUCT_NAME


async def admin_add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["product_name"] = update.message.text.strip()
    await update.message.reply_text("2️⃣ Введите описание товара:", reply_markup=ADD_CANCEL_KB)
    return ADD_PRODUCT_DESC


async def admin_add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["product_desc"] = update.message.text.strip()
    await update.message.reply_text("3️⃣ Введите цену (только число, например 1500):", reply_markup=ADD_CANCEL_KB)
    return ADD_PRODUCT_PRICE


def parse_price(text: str) -> int | None:
    """'1 500 ₽' → 1500. None — если это не положительное целое число."""
    raw = text.strip().replace("₽", "").replace("руб.", "").replace("руб", "").replace(" ", "").replace("\u00a0", "")
    if not raw.isdigit():
        return None
    price = int(raw)
    return price if price > 0 else None


async def admin_add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price = parse_price(update.message.text)
    if price is None:
        await update.message.reply_text(
            "❌ Цена должна быть целым числом больше нуля (например 1500). Попробуйте ещё раз:",
            reply_markup=ADD_CANCEL_KB,
        )
        return ADD_PRODUCT_PRICE

    context.user_data["product_price"] = price
    await update.message.reply_text(
        "4️⃣ Отправьте фото товара или напишите «нет», чтобы пропустить:",
        reply_markup=ADD_CANCEL_KB,
    )
    return ADD_PRODUCT_PHOTO


async def admin_add_product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        context.user_data["product_photo"] = update.message.photo[-1].file_id
    elif update.message.text and is_skip(update.message.text):
        context.user_data["product_photo"] = None
    else:
        await update.message.reply_text("Отправьте фото или напишите «нет»:", reply_markup=ADD_CANCEL_KB)
        return ADD_PRODUCT_PHOTO

    await update.message.reply_text(
        "5️⃣ Введите название коллекции или напишите «нет»:",
        reply_markup=ADD_CANCEL_KB,
    )
    return ADD_PRODUCT_COLLECTION


async def admin_add_product_collection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["product_collection"] = "" if is_skip(update.message.text) else update.message.text.strip()
    await update.message.reply_text(
        f"6️⃣ {INSIDE_BUTTON_TEXT} (необязательно)\n\n"
        "Опишите комплектацию: что лежит в коробке, размеры и т.п.\n\n"
        "Если не нужно — напишите «нет».",
        reply_markup=ADD_CANCEL_KB,
    )
    return ADD_PRODUCT_INSIDE_TEXT


def _inside_text_error(text: str) -> str | None:
    if len(text) > INSIDE_TEXT_LIMIT:
        return f"❌ Слишком длинно: {len(text)} символов (максимум {INSIDE_TEXT_LIMIT}). Сократите, пожалуйста:"
    return None


async def admin_add_product_inside_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if is_skip(text):
        text = ""
    error = _inside_text_error(text)
    if error:
        await update.message.reply_text(error, reply_markup=ADD_CANCEL_KB)
        return ADD_PRODUCT_INSIDE_TEXT

    context.user_data["product_inside_text"] = text
    _pop_keys(context, PHOTO_BUFFER_KEYS)
    await update.message.reply_text(
        f"7️⃣ Фото для «{INSIDE_BUTTON_TEXT}» (необязательно)\n\n"
        f"Отправьте до {INSIDE_PHOTOS_LIMIT} фото — можно сразу альбомом.\n"
        "Когда закончите — нажмите «✅ Готово».\n\n"
        "Без фото — сразу нажмите «✅ Готово».",
        reply_markup=ADD_PHOTOS_KB,
    )
    return ADD_PRODUCT_INSIDE_PHOTOS


def _photos_status_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    count = len(context.user_data.get("inside_photos_buffer", []))
    text = f"📸 Фото получено: {count} из {INSIDE_PHOTOS_LIMIT}."
    skipped = context.user_data.get("inside_photos_skipped", 0)
    if skipped:
        text += f"\n⚠️ Лишние фото ({skipped} шт.) не сохранены — максимум {INSIDE_PHOTOS_LIMIT}."
    if count < INSIDE_PHOTOS_LIMIT:
        text += "\n\nОтправьте ещё или нажмите «✅ Готово»."
    else:
        text += "\n\nНажмите «✅ Готово»."
    return text


async def collect_inside_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str, keyboard) -> None:
    """
    Копит фото «что внутри». Альбом приходит отдельными сообщениями —
    на него отвечаем ОДИН раз, когда придут все фото.
    """
    message = update.message
    user_id = update.effective_user.id
    buffer = context.user_data.setdefault("inside_photos_buffer", [])

    if len(buffer) < INSIDE_PHOTOS_LIMIT:
        buffer.append(message.photo[-1].file_id)
    else:
        context.user_data["inside_photos_skipped"] = context.user_data.get("inside_photos_skipped", 0) + 1

    group_id = message.media_group_id
    if not group_id:
        await message.reply_text(_photos_status_text(context), reply_markup=keyboard)
        return

    pending = context.user_data.setdefault("inside_album_pending", set())
    if group_id in pending:
        return
    pending.add(group_id)
    chat_id = message.chat_id

    async def answer_after_album() -> None:
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        pending.discard(group_id)
        if ACTIVE_FLOW.get(user_id) != flow:
            return  # админ уже нажал «Готово» или отменил
        try:
            await context.bot.send_message(chat_id=chat_id, text=_photos_status_text(context), reply_markup=keyboard)
        except Exception:
            logger.exception("Не удалось ответить на альбом")

    context.application.create_task(answer_after_album())


async def admin_add_product_inside_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await collect_inside_photo(update, context, FLOW_ADD_PRODUCT, ADD_PHOTOS_KB)
    return ADD_PRODUCT_INSIDE_PHOTOS


async def admin_add_product_inside_photo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_skip(update.message.text) or update.message.text.strip().lower() in {"готово", "всё", "все"}:
        return await _save_new_product(update, context)
    await update.message.reply_text(
        "Отправьте фото или нажмите «✅ Готово».",
        reply_markup=ADD_PHOTOS_KB,
    )
    return ADD_PRODUCT_INSIDE_PHOTOS


async def admin_add_product_inside_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return ConversationHandler.END
    await query.answer()
    return await _save_new_product(update, context)


async def _save_new_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    name = context.user_data.get("product_name")
    desc = context.user_data.get("product_desc", "")
    price = context.user_data.get("product_price")
    photo = context.user_data.get("product_photo")
    collection = context.user_data.get("product_collection", "")
    inside_text = context.user_data.get("product_inside_text", "")
    inside_photos = list(context.user_data.get("inside_photos_buffer", []))

    if not name or not price:
        _finish_add_product(user_id, context)
        await context.bot.send_message(
            chat_id, "⚠️ Данные товара потерялись. Начните добавление заново.", reply_markup=admin_menu()
        )
        return ConversationHandler.END

    await db.add_product(name, desc, price, photo, collection, inside_text, inside_photos)
    _finish_add_product(user_id, context)

    text = f"✅ Товар «{name}» добавлен в каталог!"
    if inside_text or inside_photos:
        text += f"\n{INSIDE_BUTTON_TEXT}: есть ({len(inside_photos)} фото)"
    await context.bot.send_message(chat_id, text, reply_markup=admin_menu())
    return ConversationHandler.END


async def admin_add_product_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Отменено")
    _finish_add_product(query.from_user.id, context)
    await safe_edit(query, "❌ Добавление товара отменено")
    return ConversationHandler.END


# ── Админ: управление товарами ────────────────────────────────────────────────

PRODUCTS_LIST_TEXT = "📦 Управление товарами:\n\n✅ — В продаже\n❌ — Снят с продажи\n\nНажмите на товар, чтобы изменить его."


async def admin_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа")
        return
    reset_all_flows(update.effective_user.id, context)

    products = await db.get_all_products(include_hidden=True)
    if not products:
        await update.message.reply_text("Товаров пока нет 📦", reply_markup=admin_menu())
        return

    await update.message.reply_text(PRODUCTS_LIST_TEXT, reply_markup=products_manage_keyboard(products))


def _inside_summary(product) -> str:
    if not db.has_inside(product):
        return "нет (кнопка не показывается)"
    parts = []
    if (product["inside_text"] or "").strip():
        parts.append("текст")
    count = int(product["inside_photos_count"] or 0)
    if count:
        parts.append(f"{count} фото")
    return "есть — " + " + ".join(parts)


def _product_admin_text(product) -> str:
    status = "✅ В продаже" if product["in_stock"] else "❌ Снят с продажи"
    text = (
        f"📦 {product['name']}\n\n"
        f"Статус: {status}\n"
        f"💰 Цена: {product['price']} ₽\n"
        f"🏷 Коллекция: {product['collection_name'] or '—'}\n"
        f"🖼 Фото: {'есть' if product['photo_id'] else 'нет'}\n"
        f"👀 Что внутри: {_inside_summary(product)}\n"
    )
    description = product["description"] or "—"
    if len(description) > 1500:
        description = description[:1500] + "…"
    text += f"\n📄 Описание:\n{description}\n"
    return text


async def _show_product_admin(query, product_id: int, prefix: str = "") -> None:
    product = await db.get_product(product_id)
    if not product:
        await _show_products_list(query, "Товар не найден (возможно, удалён).\n\n")
        return
    await safe_edit(
        query,
        prefix + _product_admin_text(product),
        single_product_manage_keyboard(product_id, product["in_stock"]),
    )


async def _show_products_list(query, prefix: str = "") -> None:
    products = await db.get_all_products(include_hidden=True)
    if not products:
        await safe_edit(query, prefix + "Товаров пока нет 📦")
        return
    await safe_edit(query, prefix + PRODUCTS_LIST_TEXT, products_manage_keyboard(products))


async def admin_product_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    end_flow(query.from_user.id)
    await _show_product_admin(query, int(query.data.split(":")[2]))


async def admin_toggle_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return

    parts = query.data.split(":")
    product_id = int(parts[2])
    new_value = 1 if int(parts[3]) else 0

    if not await db.get_product(product_id):
        await query.answer("Товар не найден", show_alert=True)
        await _show_products_list(query)
        return

    await db.toggle_product_stock(product_id, new_value)
    await query.answer("Товар снова в продаже ✅" if new_value else "Товар снят с продажи ❌")
    await _show_product_admin(query, product_id)


async def admin_delete_product_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    end_flow(query.from_user.id)

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await _show_products_list(query, "Товар не найден (возможно, уже удалён).\n\n")
        return

    await safe_edit(
        query,
        f"⚠️ Удалить товар «{product['name']}»?\n\n"
        "Это действие нельзя отменить. Товар пропадёт из каталога и из корзин покупателей.\n\n"
        "💡 Если нужно убрать его лишь на время — используйте «❌ Снять с продажи».",
        product_delete_confirm_keyboard(product_id),
    )


async def admin_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await query.answer("Товар уже удалён")
        await _show_products_list(query)
        return

    await db.delete_product(product_id)
    await query.answer("Товар удалён ✅")
    await _show_products_list(query, f"🗑 Товар «{product['name']}» удалён.\n\n")


async def admin_back_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    end_flow(query.from_user.id)
    await _show_products_list(query)


# ── Админ: изменение товара ───────────────────────────────────────────────────

# код в кнопке → (колонка в БД, как называется, подсказка)
EDIT_FIELDS: Final = {
    "name": ("name", "Название", "📝 Отправьте новое название товара:"),
    "desc": ("description", "Описание", "📄 Отправьте новое описание товара:"),
    "price": ("price", "Цена", "💰 Отправьте новую цену (только число, например 1500):"),
    "photo": ("photo_id", "Фото", "🖼 Отправьте новое фото товара.\n\nЧтобы убрать фото — напишите «нет»."),
    "collection": ("collection_name", "Коллекция",
                   "🏷 Отправьте название коллекции.\n\nЧтобы убрать коллекцию — напишите «нет»."),
    "inside": ("inside_text", "Текст «Что внутри»",
               "👀 Отправьте описание комплектации.\n\nЧтобы убрать текст — напишите «нет»."),
}


def _finish_edit(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_flow(user_id, FLOW_EDIT_PRODUCT)
    _pop_keys(context, EDIT_PRODUCT_KEYS)
    return ConversationHandler.END


async def admin_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    _finish_edit(query.from_user.id, context)

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await _show_products_list(query, "Товар не найден (возможно, удалён).\n\n")
        return

    await safe_edit(
        query,
        _product_admin_text(product) + "\n✏️ Что изменить?",
        product_edit_keyboard(product_id),
    )


async def admin_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    if await deny_if_not_admin(query):
        return ConversationHandler.END

    _, _, raw_id, field = query.data.split(":")
    product_id = int(raw_id)
    product = await db.get_product(product_id)
    if not product or field not in EDIT_FIELDS:
        await query.answer("Товар не найден", show_alert=True)
        return _finish_edit(user_id, context)
    await query.answer()

    column, title, prompt = EDIT_FIELDS[field]
    if field == "photo":
        current = "есть" if product["photo_id"] else "нет"
    elif field == "price":
        current = f"{product['price']} ₽"
    else:
        current = product[column] or "—"
        if len(current) > 1000:
            current = current[:1000] + "…"

    reset_all_flows(user_id, context)
    context.user_data["edit_product_id"] = product_id
    context.user_data["edit_product_field"] = field
    set_flow(user_id, FLOW_EDIT_PRODUCT)

    await safe_edit(
        query,
        f"✏️ Товар «{product['name']}»\n\n"
        f"{title} сейчас: {current}\n\n"
        f"{prompt}",
        product_edit_cancel_keyboard(product_id),
    )
    return EDIT_PRODUCT_VALUE


async def admin_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    message = update.message
    if not is_admin(user_id):
        return _finish_edit(user_id, context)

    product_id = context.user_data.get("edit_product_id")
    field = context.user_data.get("edit_product_field")
    if not product_id or field not in EDIT_FIELDS:
        await message.reply_text("⚠️ Не удалось определить, что менять. Начните заново.", reply_markup=admin_menu())
        return _finish_edit(user_id, context)

    column, title, _prompt = EDIT_FIELDS[field]
    cancel_kb = product_edit_cancel_keyboard(product_id)

    if field == "photo":
        if message.photo:
            value = message.photo[-1].file_id
        elif message.text and is_skip(message.text):
            value = None
        else:
            await message.reply_text("Отправьте фото или напишите «нет», чтобы убрать фото:", reply_markup=cancel_kb)
            return EDIT_PRODUCT_VALUE
    else:
        if not message.text or not message.text.strip():
            await message.reply_text("Отправьте значение текстом:", reply_markup=cancel_kb)
            return EDIT_PRODUCT_VALUE
        text = message.text.strip()

        if field == "price":
            value = parse_price(text)
            if value is None:
                await message.reply_text(
                    "❌ Цена должна быть целым числом больше нуля (например 1500). Попробуйте ещё раз:",
                    reply_markup=cancel_kb,
                )
                return EDIT_PRODUCT_VALUE
        elif field == "collection":
            value = "" if is_skip(text) else text
        elif field == "inside":
            value = "" if is_skip(text) else text
            error = _inside_text_error(value)
            if error:
                await message.reply_text(error, reply_markup=cancel_kb)
                return EDIT_PRODUCT_VALUE
        else:
            value = text

    updated = await db.update_product_field(product_id, column, value)
    _finish_edit(user_id, context)

    if not updated:
        await message.reply_text("⚠️ Товар не найден — возможно, его удалили.", reply_markup=admin_menu())
        return ConversationHandler.END

    product = await db.get_product(product_id)
    if field == "inside":
        await message.reply_text(
            f"✅ {title} — сохранено!\n\n" + _inside_admin_text(product),
            reply_markup=inside_admin_keyboard(product_id, db.has_inside(product)),
        )
    else:
        await message.reply_text(
            f"✅ {title} — сохранено!\n\n" + _product_admin_text(product),
            reply_markup=single_product_manage_keyboard(product_id, product["in_stock"]),
        )
    return ConversationHandler.END


def _inside_admin_text(product) -> str:
    text = f"{INSIDE_BUTTON_TEXT} — «{product['name']}»\n\n"
    if not db.has_inside(product):
        text += "Сейчас не заполнено — покупатели кнопку не видят.\n"
    else:
        inside_text = (product["inside_text"] or "").strip() or "—"
        if len(inside_text) > 1500:
            inside_text = inside_text[:1500] + "…"
        text += f"📝 Текст:\n{inside_text}\n\n🖼 Фото: {int(product['inside_photos_count'] or 0)} шт.\n"
    text += "\nЧто изменить?"
    return text


async def _show_inside_admin(query, product_id: int, prefix: str = "") -> None:
    product = await db.get_product(product_id)
    if not product:
        await _show_products_list(query, "Товар не найден (возможно, удалён).\n\n")
        return
    await safe_edit(query, prefix + _inside_admin_text(product),
                    inside_admin_keyboard(product_id, db.has_inside(product)))


async def admin_inside_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    _finish_edit(query.from_user.id, context)
    await _show_inside_admin(query, int(query.data.split(":")[2]))


async def admin_inside_photos_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    if await deny_if_not_admin(query):
        return ConversationHandler.END

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await query.answer("Товар не найден", show_alert=True)
        return _finish_edit(user_id, context)
    await query.answer()

    reset_all_flows(user_id, context)
    context.user_data["edit_product_id"] = product_id
    context.user_data["edit_product_field"] = "inside_photos"
    set_flow(user_id, FLOW_EDIT_PRODUCT)

    await safe_edit(
        query,
        f"🖼 Фото «Что внутри» — «{product['name']}»\n\n"
        f"Сейчас: {int(product['inside_photos_count'] or 0)} шт.\n\n"
        f"Отправьте новые фото (до {INSIDE_PHOTOS_LIMIT}, можно альбомом) и нажмите «✅ Готово».\n"
        "⚠️ Новые фото ЗАМЕНЯТ старые.",
        _edit_photos_keyboard(product_id),
    )
    return EDIT_INSIDE_PHOTOS


def _edit_photos_keyboard(product_id: int):
    return photos_done_keyboard(f"admin:inside_photos_done:{product_id}", f"admin:edit_cancel:{product_id}")


async def admin_inside_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    product_id = context.user_data.get("edit_product_id")
    if not is_admin(update.effective_user.id) or not product_id:
        return _finish_edit(update.effective_user.id, context)
    await collect_inside_photo(update, context, FLOW_EDIT_PRODUCT, _edit_photos_keyboard(product_id))
    return EDIT_INSIDE_PHOTOS


async def admin_inside_photo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    product_id = context.user_data.get("edit_product_id")
    if not product_id:
        return _finish_edit(update.effective_user.id, context)
    await update.message.reply_text(
        "Отправьте фото и нажмите «✅ Готово».\n"
        "Чтобы удалить все фото — «Отмена», затем «🗑 Убрать «Что внутри»».",
        reply_markup=_edit_photos_keyboard(product_id),
    )
    return EDIT_INSIDE_PHOTOS


async def admin_inside_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    if await deny_if_not_admin(query):
        return ConversationHandler.END

    product_id = int(query.data.split(":")[2])
    photos = list(context.user_data.get("inside_photos_buffer", []))
    if not photos:
        await query.answer("Вы ещё не отправили ни одного фото 🙂", show_alert=True)
        return EDIT_INSIDE_PHOTOS

    await query.answer("Фото сохранены ✅")
    _finish_edit(user_id, context)
    if not await db.get_product(product_id):
        await _show_products_list(query, "Товар не найден (возможно, удалён).\n\n")
        return ConversationHandler.END

    await db.replace_inside_photos(product_id, photos)
    # Отвечаем новым сообщением: старое уже далеко вверху, над присланными фото
    product = await db.get_product(product_id)
    await context.bot.send_message(
        chat_id_of(query),
        f"✅ Фото сохранены ({len(photos)} шт.)\n\n" + _inside_admin_text(product),
        reply_markup=inside_admin_keyboard(product_id, db.has_inside(product)),
    )
    return ConversationHandler.END


async def admin_inside_clear_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    await query.answer()
    end_flow(query.from_user.id)

    product_id = int(query.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await _show_products_list(query, "Товар не найден (возможно, удалён).\n\n")
        return
    await safe_edit(
        query,
        f"⚠️ Убрать «Что внутри» у товара «{product['name']}»?\n\n"
        "Удалятся текст и все фото, кнопка у товара пропадёт.",
        inside_clear_confirm_keyboard(product_id),
    )


async def admin_inside_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return
    product_id = int(query.data.split(":")[2])
    if not await db.get_product(product_id):
        await query.answer("Товар не найден", show_alert=True)
        await _show_products_list(query)
        return
    await db.clear_inside(product_id)
    await query.answer("«Что внутри» убрано ✅")
    await _show_inside_admin(query, product_id, "🗑 Убрано.\n\n")


async def admin_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return ConversationHandler.END
    await query.answer("Изменение отменено")
    field = context.user_data.get("edit_product_field")
    _finish_edit(query.from_user.id, context)
    product_id = int(query.data.split(":")[2])
    if field in ("inside", "inside_photos"):
        await _show_inside_admin(query, product_id)
    else:
        await _show_product_admin(query, product_id)
    return ConversationHandler.END


# ── Админ: ответ в поддержку ВК ───────────────────────────────────────────────

def _finish_support_reply(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_flow(user_id, FLOW_SUPPORT_REPLY)
    _pop_keys(context, SUPPORT_KEYS)
    return ConversationHandler.END


async def admin_support_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    if await deny_if_not_admin(query):
        return ConversationHandler.END

    message_id = int(query.data.split(":")[2])
    support_message = await db.get_support_message(message_id)
    if not support_message:
        await query.answer("Сообщение не найдено", show_alert=True)
        return _finish_support_reply(user_id, context)
    await query.answer()

    reset_all_flows(user_id, context)
    context.user_data["support_reply_to"] = message_id
    set_flow(user_id, FLOW_SUPPORT_REPLY)

    # Не редактируем исходное сообщение — оно должно остаться в истории
    await context.bot.send_message(
        chat_id_of(query),
        f"↩️ Ответ для {support_message['user_name'] or 'покупателя'} (ВК)\n\n"
        f"Его сообщение:\n«{support_message['text'][:500]}»\n\n"
        "Напишите ответ одним сообщением:",
        reply_markup=support_reply_cancel_keyboard(),
    )
    return SUPPORT_REPLY


async def admin_support_reply_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return _finish_support_reply(user_id, context)

    message_id = context.user_data.get("support_reply_to")
    support_message = await db.get_support_message(message_id) if message_id else None
    if not support_message:
        await update.message.reply_text("⚠️ Сообщение не найдено.", reply_markup=admin_menu())
        return _finish_support_reply(user_id, context)

    text = update.message.text.strip()
    await db.add_event(support_message["source"], "support_reply", support_message["external_user_id"], {
        "text": text,
        "reply_to_message_id": message_id,
    })
    _finish_support_reply(user_id, context)
    await update.message.reply_text(
        f"✅ Ответ отправлен {support_message['user_name'] or 'покупателю'} во ВКонтакте.\n"
        "Бот ВК доставит его в течение нескольких секунд.",
        reply_markup=support_reply_keyboard(message_id),
    )
    return ConversationHandler.END


async def admin_support_reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await deny_if_not_admin(query):
        return ConversationHandler.END
    await query.answer("Отменено")
    _finish_support_reply(query.from_user.id, context)
    await safe_edit(query, "❌ Ответ не отправлен")
    return ConversationHandler.END


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
    if not REVIEWS_LINK:
        logger.warning("REVIEWS_URL не задан — кнопка «Отзывы» покажет «скоро появится» ⚠️")

    if SYNC_API_TOKEN:
        try:
            import sync_api
            application.bot_data["sync_api_runner"] = await sync_api.start_server(application.bot)
        except Exception:
            # Бот в Telegram должен работать, даже если API для ВК не поднялось
            logger.exception("Не удалось запустить API для бота ВК ⚠️")
    else:
        logger.info("SYNC_API_TOKEN не задан — API для бота ВК выключено")


async def post_shutdown(application: Application) -> None:
    runner = application.bot_data.pop("sync_api_runner", None)
    if runner is not None:
        await runner.cleanup()


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    start_cmd = CommandHandler("start", start, filters=NEW_MSG)
    admin_cmd = CommandHandler("admin", admin_start, filters=NEW_MSG)

    # ── Команды ──
    application.add_handler(start_cmd)
    application.add_handler(admin_cmd)

    # ── Reply-меню пользователя ──
    application.add_handler(MessageHandler(menu_button(BTN_CATALOG), catalog))
    application.add_handler(MessageHandler(menu_button(BTN_CART), cart_view))
    application.add_handler(MessageHandler(menu_button(BTN_MY_ORDERS), my_orders))
    application.add_handler(MessageHandler(menu_button(BTN_SUPPORT), support))
    application.add_handler(MessageHandler(menu_button(BTN_REVIEWS), reviews))

    # ── Reply-меню админа ──
    application.add_handler(MessageHandler(menu_button(BTN_ADMIN_ORDERS), admin_orders))
    application.add_handler(MessageHandler(menu_button(BTN_ADMIN_PRODUCTS), admin_manage_products))
    application.add_handler(MessageHandler(menu_button(BTN_ADMIN_USER_MODE), user_mode))

    # ── Каталог ──
    application.add_handler(CallbackQueryHandler(show_product, pattern=r"^product:\d+$"))
    application.add_handler(CallbackQueryHandler(show_inside, pattern=r"^inside:\d+$"))
    application.add_handler(CallbackQueryHandler(catalog_back, pattern=r"^catalog:back$"))

    # ── Корзина ──
    application.add_handler(CallbackQueryHandler(cart_add, pattern=r"^cart:add:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_remove, pattern=r"^cart:remove:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_clear_ask, pattern=r"^cart:clear$"))
    application.add_handler(CallbackQueryHandler(cart_clear_yes, pattern=r"^cart:clear_yes$"))
    application.add_handler(CallbackQueryHandler(cart_clear_no, pattern=r"^cart:clear_no$"))
    application.add_handler(CallbackQueryHandler(delivery_info_callback, pattern=r"^delivery_info$"))

    # ── Оформление заказа ──
    checkout_text = TEXT_INPUT & FlowFilter(FLOW_CHECKOUT)
    checkout_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(checkout_start, pattern=r"^checkout$")],
        states={
            CHECKOUT_ADDRESS: [MessageHandler(checkout_text, checkout_address)],
            CHECKOUT_COMMENT: [MessageHandler(checkout_text, checkout_comment)],
            CHECKOUT_CONFIRM: [CallbackQueryHandler(confirm_order, pattern=r"^confirm_order$")],
        },
        fallbacks=[CallbackQueryHandler(cancel_order, pattern=r"^cancel_order$")],
        allow_reentry=True,
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
    track_cancel = CallbackQueryHandler(admin_track_cancel, pattern=r"^admin:track_cancel:\d+$")
    track_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_track_start, pattern=r"^admin:order_track:\d+$")],
        states={ADD_TRACK: [MessageHandler(TEXT_INPUT & FlowFilter(FLOW_TRACK), admin_track_save)]},
        fallbacks=[track_cancel],
        allow_reentry=True,
        name="add_track",
        persistent=False,
    )
    application.add_handler(track_conversation)

    # ── Добавление товара ──
    add_text = TEXT_INPUT & FlowFilter(FLOW_ADD_PRODUCT)
    add_cancel = CallbackQueryHandler(admin_add_product_cancel, pattern=r"^admin:add_cancel$")
    add_product_conversation = ConversationHandler(
        entry_points=[MessageHandler(menu_button(BTN_ADMIN_ADD_PRODUCT), admin_add_product_start)],
        states={
            ADD_PRODUCT_NAME: [MessageHandler(add_text, admin_add_product_name)],
            ADD_PRODUCT_DESC: [MessageHandler(add_text, admin_add_product_desc)],
            ADD_PRODUCT_PRICE: [MessageHandler(add_text, admin_add_product_price)],
            ADD_PRODUCT_PHOTO: [
                MessageHandler(NEW_MSG & filters.PHOTO & FlowFilter(FLOW_ADD_PRODUCT), admin_add_product_photo),
                MessageHandler(add_text, admin_add_product_photo),
            ],
            ADD_PRODUCT_COLLECTION: [MessageHandler(add_text, admin_add_product_collection)],
            ADD_PRODUCT_INSIDE_TEXT: [MessageHandler(add_text, admin_add_product_inside_text)],
            ADD_PRODUCT_INSIDE_PHOTOS: [
                MessageHandler(NEW_MSG & filters.PHOTO & FlowFilter(FLOW_ADD_PRODUCT), admin_add_product_inside_photo),
                MessageHandler(add_text, admin_add_product_inside_photo_text),
                CallbackQueryHandler(admin_add_product_inside_done, pattern=r"^admin:add_inside_done$"),
            ],
        },
        fallbacks=[add_cancel],
        allow_reentry=True,
        name="add_product",
        persistent=False,
    )
    application.add_handler(add_product_conversation)

    # ── Изменение товара ──
    edit_cancel = CallbackQueryHandler(admin_edit_cancel, pattern=r"^admin:edit_cancel:\d+$")
    edit_product_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_edit_start, pattern=r"^admin:edit:\d+:(name|desc|price|photo|collection|inside)$"
            ),
            CallbackQueryHandler(admin_inside_photos_start, pattern=r"^admin:inside_photos:\d+$"),
        ],
        states={
            EDIT_PRODUCT_VALUE: [
                MessageHandler(NEW_MSG & filters.PHOTO & FlowFilter(FLOW_EDIT_PRODUCT), admin_edit_save),
                MessageHandler(TEXT_INPUT & FlowFilter(FLOW_EDIT_PRODUCT), admin_edit_save),
            ],
            EDIT_INSIDE_PHOTOS: [
                MessageHandler(NEW_MSG & filters.PHOTO & FlowFilter(FLOW_EDIT_PRODUCT), admin_inside_photo_received),
                MessageHandler(TEXT_INPUT & FlowFilter(FLOW_EDIT_PRODUCT), admin_inside_photo_text),
                CallbackQueryHandler(admin_inside_photos_done, pattern=r"^admin:inside_photos_done:\d+$"),
            ],
        },
        fallbacks=[edit_cancel],
        allow_reentry=True,
        name="edit_product",
        persistent=False,
    )
    application.add_handler(edit_product_conversation)

    # ── Управление товарами ──
    application.add_handler(CallbackQueryHandler(admin_product_manage, pattern=r"^admin:product_manage:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_edit_menu, pattern=r"^admin:edit_menu:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_inside_menu, pattern=r"^admin:inside_menu:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_inside_clear_ask, pattern=r"^admin:inside_clear:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_inside_clear, pattern=r"^admin:inside_clear_yes:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_toggle_stock, pattern=r"^admin:toggle_stock:\d+:[01]$"))
    application.add_handler(CallbackQueryHandler(admin_delete_product_ask, pattern=r"^admin:delete_product:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_delete_product, pattern=r"^admin:delete_product_yes:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_back_manage_products, pattern=r"^admin:back_manage_products$"))

    # ── Ответ в поддержку (сообщения из ВК) ──
    support_cancel = CallbackQueryHandler(admin_support_reply_cancel, pattern=r"^admin:support_cancel$")
    support_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_support_reply_start, pattern=r"^admin:support_reply:\d+$")],
        states={
            SUPPORT_REPLY: [MessageHandler(TEXT_INPUT & FlowFilter(FLOW_SUPPORT_REPLY), admin_support_reply_save)],
        },
        fallbacks=[support_cancel],
        allow_reentry=True,
        name="support_reply",
        persistent=False,
    )
    application.add_handler(support_conversation)

    # Кнопки «Отмена» из диалогов, которые уже не активны (например, после перезапуска)
    application.add_handler(edit_cancel)
    application.add_handler(track_cancel)
    application.add_handler(add_cancel)
    application.add_handler(support_cancel)

    # ── Fallback-и (обязательно последними) ──
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(stale_confirm_order, pattern=r"^confirm_order$"))
    application.add_handler(CallbackQueryHandler(stale_cancel_order, pattern=r"^cancel_order$"))
    application.add_handler(CallbackQueryHandler(fallback_callback))
    application.add_handler(MessageHandler(
        NEW_MSG & filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
        unknown_message,
    ))

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    logger.info("Бот запущен 🚀")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
