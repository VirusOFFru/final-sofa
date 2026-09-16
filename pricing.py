"""
Единый источник правды для расчёта стоимости заказа.

Любой экран (корзина, подтверждение, оплата) берёт цифры ТОЛЬКО отсюда.

Правила:
  • Скидка 10% на брелоки — если в заказе от 2 брелоков (штук).
  • Доставка бесплатная — если сумма брелоков от 1000 ₽.
    Сумма считается ТОЛЬКО по брелокам: без доставки и до применения скидки.
  • Иначе доставка — 389 ₽.
"""
from dataclasses import dataclass

from config import (
    DELIVERY_COST,
    FREE_DELIVERY_SUM,
    DISCOUNT_FROM_QTY,
    DISCOUNT_PERCENT,
)


def plural_keychain(n: int) -> str:
    """1 брелок, 2 брелока, 5 брелоков, 11 брелоков, 21 брелок."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "брелок"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "брелока"
    return "брелоков"


def plural_keychain_from(n: int) -> str:
    """Для оборота «от N ...»: от 1 брелока, от 2 брелоков, от 21 брелока."""
    n = abs(n)
    return "брелока" if (n % 10 == 1 and n % 100 != 11) else "брелоков"


@dataclass
class CartSummary:
    subtotal: int          # сумма брелоков без скидки и доставки
    quantity: int          # общее число брелоков
    delivery: int          # стоимость доставки (0 = бесплатно)
    discount_percent: int  # применённая скидка в %
    discount_amount: int   # размер скидки в рублях
    total: int             # итог к оплате

    @property
    def free_delivery(self) -> bool:
        return self.delivery == 0

    @property
    def to_free_delivery(self) -> int:
        """Сколько рублей брелоков не хватает до бесплатной доставки (0 если уже)."""
        return max(0, FREE_DELIVERY_SUM - self.subtotal)

    @property
    def to_discount(self) -> int:
        """Сколько брелоков не хватает до скидки (0 если уже)."""
        return max(0, DISCOUNT_FROM_QTY - self.quantity)


def calculate(items) -> CartSummary:
    """Считает итог по списку позиций корзины (как из БД, так и из user_data)."""
    subtotal = 0
    quantity = 0

    for item in items:
        price = int(item["price"])
        qty = int(item["quantity"])
        subtotal += price * qty
        quantity += qty

    # Скидка на брелоки — по количеству штук
    discount_percent = DISCOUNT_PERCENT if quantity >= DISCOUNT_FROM_QTY else 0
    discount_amount = int(subtotal * discount_percent / 100)

    # Доставка — по сумме брелоков (без доставки, до скидки)
    if quantity == 0 or subtotal >= FREE_DELIVERY_SUM:
        delivery = 0
    else:
        delivery = DELIVERY_COST

    total = subtotal - discount_amount + delivery

    return CartSummary(
        subtotal=subtotal,
        quantity=quantity,
        delivery=delivery,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
    )


def format_cart_lines(items) -> str:
    """Список позиций заказа в виде строк."""
    lines = []
    for item in items:
        price = int(item["price"])
        qty = int(item["quantity"])
        name = item["name"] if "name" in item.keys() else item["product_name"]
        lines.append(f"• {name} — {qty} шт. × {price} ₽ = {price * qty} ₽")
    return "\n".join(lines)


def render_summary(summary: CartSummary, items=None, title: str = "🛒 Ваша корзина:") -> str:
    """Готовый текст с составом заказа и всеми надбавками. Один вид на всех экранах."""
    parts = [title]

    if items:
        parts.append("")
        parts.append(format_cart_lines(items))

    parts.append("")
    parts.append(f"💰 Сумма брелоков: {summary.subtotal} ₽")

    if summary.discount_percent > 0:
        parts.append(
            f"🎁 Скидка {summary.discount_percent}%: −{summary.discount_amount} ₽"
        )

    if summary.quantity == 0:
        pass
    elif summary.free_delivery:
        parts.append("🚚 Доставка: бесплатно 🎉")
    else:
        parts.append(f"🚚 Доставка: {summary.delivery} ₽")

    parts.append("")
    parts.append(f"✅ Итого к оплате: {summary.total} ₽")

    return "\n".join(parts)


def hints_for(summary: CartSummary) -> list[str]:
    """Подсказки-мотиваторы для корзины (по одной на кнопку). Пустой список — подсказывать нечего."""
    if summary.quantity == 0:
        return []

    if summary.discount_percent > 0 and summary.free_delivery:
        return [f"🎉 Скидка {summary.discount_percent}% + бесплатная доставка!"]

    hints = []
    if summary.discount_percent == 0 and summary.to_discount > 0:
        left = summary.to_discount
        hints.append(f"🎁 Ещё {left} {plural_keychain(left)} — скидка {DISCOUNT_PERCENT}%")
    if not summary.free_delivery and summary.to_free_delivery > 0:
        hints.append(f"🚚 Ещё {summary.to_free_delivery} ₽ — доставка бесплатно")
    return hints


def rules_text() -> str:
    """Описание условий акции — для всплывающей подсказки (Telegram: максимум 200 символов)."""
    return (
        f"🎁 От {DISCOUNT_FROM_QTY} {plural_keychain_from(DISCOUNT_FROM_QTY)} — скидка {DISCOUNT_PERCENT}%\n\n"
        f"🚚 Брелоков на {FREE_DELIVERY_SUM} ₽ и больше — доставка бесплатно "
        f"(доставка в сумму не входит)\n\n"
        f"Иначе доставка — {DELIVERY_COST} ₽"
    )
