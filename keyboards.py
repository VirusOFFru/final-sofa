"""
Все клавиатуры бота. Тексты кнопок-меню вынесены в константы,
чтобы фильтры в bot.py и разметка не разъезжались.

Цвета кнопок (Bot API 9.4+, Telegram Premium НЕ нужен):
  GREEN — главное/позитивное действие, BLUE — важное, RED — удаление.
Старые версии приложений просто покажут обычные кнопки.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from pricing import CartSummary, hints_for

GREEN = "success"
BLUE = "primary"
RED = "danger"


def btn(text: str, callback_data: str | None = None, *, url: str | None = None,
        style: str | None = None) -> InlineKeyboardButton:
    """Inline-кнопка с необязательным цветом."""
    return InlineKeyboardButton(text, callback_data=callback_data, url=url, style=style)


def key(text: str, style: str | None = None) -> KeyboardButton:
    """Кнопка нижнего меню с необязательным цветом."""
    return KeyboardButton(text, style=style)


# ── Тексты reply-кнопок (используются и в фильтрах bot.py) ────────────────────
BTN_CATALOG = "🛍 Каталог"
BTN_CART = "🛒 Корзина"
BTN_MY_ORDERS = "📦 Мои заказы"
BTN_SUPPORT = "📞 Поддержка"
BTN_REVIEWS = "⭐ Отзывы"

BTN_ADMIN_ORDERS = "📋 Все заказы"
BTN_ADMIN_ADD_PRODUCT = "➕ Добавить товар"
BTN_ADMIN_PRODUCTS = "📦 Управление товарами"
BTN_ADMIN_USER_MODE = "👤 Режим пользователя"

# Все тексты reply-кнопок — чтобы диалоги не принимали их за ввод
MENU_BUTTONS = (
    BTN_CATALOG, BTN_CART, BTN_MY_ORDERS, BTN_SUPPORT, BTN_REVIEWS,
    BTN_ADMIN_ORDERS, BTN_ADMIN_ADD_PRODUCT, BTN_ADMIN_PRODUCTS, BTN_ADMIN_USER_MODE,
)

INSIDE_BUTTON_TEXT = "👀 Покажи что внутри"

STATUS_EMOJI = {
    "pending_payment": "⏳",
    "new": "🆕",
    "shipped": "🚚",
    "delivered": "✅",
    "cancelled": "❌",
}

STATUS_TEXT = {
    "pending_payment": "Ожидает оплаты",
    "new": "Новый (оплачен)",
    "shipped": "В пути",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}

CURRENT_STATUSES = {"pending_payment", "new", "shipped"}


def status_label(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '❓')} {STATUS_TEXT.get(status, status)}"


# ── Меню ──────────────────────────────────────────────────────────────────────

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [key(BTN_CATALOG, GREEN)],
            [key(BTN_CART, BLUE)],
            [key(BTN_MY_ORDERS), key(BTN_SUPPORT)],
            [key(BTN_REVIEWS)],
        ],
        resize_keyboard=True,
    )


def reviews_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("⭐ Перейти к отзывам", url=url, style=BLUE)]])


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [key(BTN_ADMIN_ORDERS, BLUE)],
            [key(BTN_ADMIN_ADD_PRODUCT, GREEN)],
            [key(BTN_ADMIN_PRODUCTS), key(BTN_ADMIN_USER_MODE)],
        ],
        resize_keyboard=True,
    )


# ── Каталог ───────────────────────────────────────────────────────────────────

def catalog_keyboard(products) -> InlineKeyboardMarkup:
    rows = [
        [btn(f"{p['name']} — {p['price']} ₽", f"product:{p['id']}")]
        for p in products
    ]
    return InlineKeyboardMarkup(rows)


def product_keyboard(product_id: int, has_inside: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if has_inside:
        rows.append([btn(INSIDE_BUTTON_TEXT, f"inside:{product_id}", style=BLUE)])
    rows.append([btn("🛒 В корзину", f"cart:add:{product_id}", style=GREEN)])
    rows.append([btn("◀️ Назад к каталогу", "catalog:back")])
    return InlineKeyboardMarkup(rows)


def inside_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Под экраном «Что внутри»."""
    return InlineKeyboardMarkup(
        [
            [btn("🛒 В корзину", f"cart:add:{product_id}", style=GREEN)],
            [btn("◀️ Назад к товару", f"product:{product_id}")],
        ]
    )


# ── Корзина ───────────────────────────────────────────────────────────────────

def cart_keyboard(items, summary: CartSummary) -> InlineKeyboardMarkup:
    rows = []

    # Подсказки про скидку / бесплатную доставку (по клику — условия акции)
    for hint in hints_for(summary):
        rows.append([btn(hint, "delivery_info")])

    # Каждая кнопка убирает ОДНУ штуку товара
    for item in items:
        qty = int(item["quantity"])
        if qty > 1:
            text = f"➖ Убрать 1 шт. «{item['name']}» (в корзине {qty})"
        else:
            text = f"❌ Убрать «{item['name']}»"
        rows.append([btn(text, f"cart:remove:{item['id']}")])

    rows.append([btn("✅ Оформить заказ", "checkout", style=GREEN)])
    rows.append([btn("🗑 Очистить корзину", "cart:clear")])
    return InlineKeyboardMarkup(rows)


def cart_clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🗑 Да, очистить", "cart:clear_yes", style=RED)],
            [btn("↩️ Нет, вернуться в корзину", "cart:clear_no")],
        ]
    )


def checkout_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("❌ Отменить оформление", "cancel_order")]])


def confirm_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("✅ Подтвердить", "confirm_order", style=GREEN)],
            [btn("❌ Отмена", "cancel_order")],
        ]
    )


def payment_keyboard(confirmation_url: str | None, order_id: int, with_orders_back: bool = False) -> InlineKeyboardMarkup:
    """
    Кнопки оплаты. Ссылка «Оплатить» остаётся на месте после проверки,
    чтобы покупатель мог вернуться к оплате, если нажал «Проверить» случайно.
    """
    rows = []
    if confirmation_url:
        rows.append([btn("💳 Оплатить", url=confirmation_url, style=GREEN)])
    rows.append([btn("✅ Проверить оплату", f"check_pay:{order_id}", style=BLUE)])
    if with_orders_back:
        rows.append([btn("◀️ К моим заказам", "my_orders_back")])
    return InlineKeyboardMarkup(rows)


# ── Заказы ────────────────────────────────────────────────────────────────────

def order_manage_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    """Клавиатура управления заказом для админа."""
    rows = []

    if status in {"new", "pending_payment"}:
        rows.append([btn("🚚 Отправить (добавить трек)", f"admin:order_track:{order_id}", style=BLUE)])

    if status == "shipped":
        rows.append([btn("✅ Отметить доставленным", f"admin:order_delivered:{order_id}", style=GREEN)])

    if status not in {"cancelled", "delivered"}:
        rows.append([btn("❌ Отменить заказ", f"admin:order_cancel:{order_id}", style=RED)])

    # Telegram не принимает пустую клавиатуру — ставим заглушку-информер
    if not rows:
        rows.append([btn("— заказ закрыт —", "noop")])

    return InlineKeyboardMarkup(rows)


def user_order_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "shipped":
        rows.append([btn("✅ Я получил заказ", f"user:order_received:{order_id}", style=GREEN)])
    rows.append([btn("◀️ К моим заказам", "my_orders_back")])
    return InlineKeyboardMarkup(rows)


def support_reply_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("↩️ Ответить", f"admin:support_reply:{message_id}", style=BLUE)]])


def support_reply_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("❌ Отмена", "admin:support_cancel")]])


# ── Товары (админка) ──────────────────────────────────────────────────────────

def products_manage_keyboard(products) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        mark = "✅" if p["in_stock"] else "❌"
        rows.append([btn(f"{mark} {p['name']} — {p['price']} ₽", f"admin:product_manage:{p['id']}")])
    return InlineKeyboardMarkup(rows)


def single_product_manage_keyboard(product_id: int, in_stock: int) -> InlineKeyboardMarkup:
    toggle_text = "❌ Снять с продажи" if in_stock else "✅ Вернуть в продажу"
    next_value = 0 if in_stock else 1
    return InlineKeyboardMarkup(
        [
            [btn("✏️ Изменить товар", f"admin:edit_menu:{product_id}", style=BLUE)],
            [btn(toggle_text, f"admin:toggle_stock:{product_id}:{next_value}")],
            [btn("🗑 Удалить товар", f"admin:delete_product:{product_id}", style=RED)],
            [btn("◀️ Назад", "admin:back_manage_products")],
        ]
    )


def product_edit_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Что именно поменять в товаре."""
    return InlineKeyboardMarkup(
        [
            [btn("📝 Название", f"admin:edit:{product_id}:name")],
            [btn("📄 Описание", f"admin:edit:{product_id}:desc")],
            [btn("💰 Цена", f"admin:edit:{product_id}:price")],
            [btn("🖼 Фото", f"admin:edit:{product_id}:photo")],
            [btn("🏷 Коллекция", f"admin:edit:{product_id}:collection")],
            [btn(INSIDE_BUTTON_TEXT, f"admin:inside_menu:{product_id}", style=BLUE)],
            [btn("◀️ Назад к товару", f"admin:product_manage:{product_id}")],
        ]
    )


def inside_admin_keyboard(product_id: int, has_inside: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("📝 Текст (комплектация)", f"admin:edit:{product_id}:inside")],
        [btn("🖼 Фото (заменить)", f"admin:inside_photos:{product_id}")],
    ]
    if has_inside:
        rows.append([btn("👁 Посмотреть как покупатель", f"inside:{product_id}")])
        rows.append([btn("🗑 Убрать «Что внутри»", f"admin:inside_clear:{product_id}", style=RED)])
    rows.append([btn("◀️ Назад", f"admin:edit_menu:{product_id}")])
    return InlineKeyboardMarkup(rows)


def inside_clear_confirm_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🗑 Да, убрать", f"admin:inside_clear_yes:{product_id}", style=RED)],
            [btn("↩️ Нет, назад", f"admin:inside_menu:{product_id}")],
        ]
    )


def photos_done_keyboard(done_callback: str, cancel_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("✅ Готово", done_callback, style=GREEN)],
            [btn("❌ Отмена", cancel_callback)],
        ]
    )


def product_edit_cancel_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("❌ Отмена", f"admin:edit_cancel:{product_id}")]])


def product_delete_confirm_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🗑 Да, удалить", f"admin:delete_product_yes:{product_id}", style=RED)],
            [btn("↩️ Нет, назад", f"admin:product_manage:{product_id}")],
        ]
    )


def orders_admin_keyboard(orders, limit: int = 10) -> InlineKeyboardMarkup:
    rows = []
    for o in orders[:limit]:
        tag = "🟦 ВК · " if (o["source"] or "tg") == "vk" else ""
        rows.append([btn(f"📦 {tag}Заказ #{o['id']}", f"admin:view_order:{o['id']}")])
    return InlineKeyboardMarkup(rows)
