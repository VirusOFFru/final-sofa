"""
Все клавиатуры бота. Тексты кнопок-меню вынесены в константы,
чтобы фильтры в bot.py и разметка не разъезжались.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from pricing import CartSummary, hint_for

# ── Тексты reply-кнопок (используются и в фильтрах bot.py) ────────────────────
BTN_CATALOG = "🛍 Каталог"
BTN_CART = "🛒 Корзина"
BTN_MY_ORDERS = "📦 Мои заказы"
BTN_SUPPORT = "📞 Поддержка"

BTN_ADMIN_ORDERS = "📋 Все заказы"
BTN_ADMIN_ADD_PRODUCT = "➕ Добавить товар"
BTN_ADMIN_PRODUCTS = "📦 Управление товарами"
BTN_ADMIN_USER_MODE = "👤 Режим пользователя"

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
            [KeyboardButton(BTN_CATALOG), KeyboardButton(BTN_CART)],
            [KeyboardButton(BTN_MY_ORDERS), KeyboardButton(BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ADMIN_ORDERS), KeyboardButton(BTN_ADMIN_ADD_PRODUCT)],
            [KeyboardButton(BTN_ADMIN_PRODUCTS), KeyboardButton(BTN_ADMIN_USER_MODE)],
        ],
        resize_keyboard=True,
    )


# ── Каталог ───────────────────────────────────────────────────────────────────

def catalog_keyboard(products) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{p['name']} — {p['price']} ₽", callback_data=f"product:{p['id']}")]
        for p in products
    ]
    return InlineKeyboardMarkup(rows)


def product_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 В корзину", callback_data=f"cart:add:{product_id}")],
            [InlineKeyboardButton("◀️ Назад к каталогу", callback_data="catalog:back")],
        ]
    )


# ── Корзина ───────────────────────────────────────────────────────────────────

def cart_keyboard(items, summary: CartSummary) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"❌ Убрать «{item['name']}»", callback_data=f"cart:remove:{item['id']}")]
        for item in items
    ]

    hint = hint_for(summary)
    if hint:
        rows.insert(0, [InlineKeyboardButton(hint, callback_data="delivery_info")])

    rows.append([InlineKeyboardButton("✅ Оформить заказ", callback_data="checkout")])
    rows.append([InlineKeyboardButton("🗑 Очистить корзину", callback_data="cart:clear")])
    return InlineKeyboardMarkup(rows)


def confirm_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_order")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_order")],
        ]
    )


def payment_keyboard(confirmation_url: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Оплатить", url=confirmation_url)],
            [InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_pay:{order_id}")],
        ]
    )


# ── Заказы ────────────────────────────────────────────────────────────────────

def order_manage_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    """Клавиатура управления заказом для админа."""
    rows = []

    if status in {"new", "pending_payment"}:
        rows.append([InlineKeyboardButton(
            "🚚 Отправить (добавить трек)", callback_data=f"admin:order_track:{order_id}"
        )])

    if status == "shipped":
        rows.append([InlineKeyboardButton(
            "✅ Отметить доставленным", callback_data=f"admin:order_delivered:{order_id}"
        )])

    if status not in {"cancelled", "delivered"}:
        rows.append([InlineKeyboardButton(
            "❌ Отменить заказ", callback_data=f"admin:order_cancel:{order_id}"
        )])

    # Telegram не принимает пустую клавиатуру — ставим заглушку-информер
    if not rows:
        rows.append([InlineKeyboardButton("— заказ закрыт —", callback_data="noop")])

    return InlineKeyboardMarkup(rows)


def user_order_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "shipped":
        rows.append([InlineKeyboardButton(
            "✅ Я получил заказ", callback_data=f"user:order_received:{order_id}"
        )])
    rows.append([InlineKeyboardButton("◀️ К моим заказам", callback_data="my_orders_back")])
    return InlineKeyboardMarkup(rows)


# ── Товары (админка) ──────────────────────────────────────────────────────────

def products_manage_keyboard(products) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        mark = "✅" if p["in_stock"] else "❌"
        rows.append([InlineKeyboardButton(
            f"{mark} {p['name']} — {p['price']} ₽",
            callback_data=f"admin:product_manage:{p['id']}",
        )])
    return InlineKeyboardMarkup(rows)


def single_product_manage_keyboard(product_id: int, in_stock: int) -> InlineKeyboardMarkup:
    toggle_text = "❌ Снять с продажи" if in_stock else "✅ Вернуть в продажу"
    next_value = 0 if in_stock else 1
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_text, callback_data=f"admin:toggle_stock:{product_id}:{next_value}")],
            [InlineKeyboardButton("🗑 Удалить товар", callback_data=f"admin:delete_product:{product_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="admin:back_manage_products")],
        ]
    )


def orders_admin_keyboard(orders, limit: int = 10) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📦 Заказ #{o['id']}", callback_data=f"admin:view_order:{o['id']}")]
        for o in orders[:limit]
    ]
    return InlineKeyboardMarkup(rows)
