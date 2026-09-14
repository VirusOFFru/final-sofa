"""
Единый источник правды для расчёта стоимости заказа.

Раньше логика дублировалась в bot.py / keyboards.py и расходилась
(где-то «бесплатно от 2», где-то «от 3»). Теперь всё считается ЗДЕСЬ,
и любой экран берёт цифры отсюда.

Правила:
  • 1 брелок            → доставка 389 ₽
  • 2 брелока           → доставка бесплатно
  • 3+ брелока          → доставка бесплатно + скидка 10% на товары
"""
from dataclasses import dataclass

from config import (
    DELIVERY_COST,
    FREE_DELIVERY_FROM,
    DISCOUNT_FROM,
    DISCOUNT_PERCENT,
)


@dataclass
class CartSummary:
    subtotal: int          # сумма товаров без скидки и доставки
    quantity: int          # общее число брелков
    delivery: int          # стоимость доставки (0 = бесплатно)
    discount_percent: int  # применённая скидка в %
    discount_amount: int   # размер скидки в рублях
    total: int             # итог к оплате

    @property
    def free_delivery(self) -> bool:
        return self.delivery == 0

    @property
    def to_free_delivery(self) -> int:
        """Сколько брелков не хватает до бесплатной доставки (0 если уже)."""
        return max(0, FREE_DELIVERY_FROM - self.quantity)

    @property
    def to_discount(self) -> int:
        """Сколько брелков не хватает до скидки (0 если уже)."""
        return max(0, DISCOUNT_FROM - self.quantity)


def calculate(items) -> CartSummary:
    """Считает итог по списку позиций корзины (как из БД, так и из user_data)."""
    subtotal = 0
    quantity = 0

    for item in items:
        price = int(item["price"])
        qty = int(item["quantity"])
        subtotal += price * qty
        quantity += qty

    # Доставка: платим только если брелков меньше порога
    delivery = 0 if quantity >= FREE_DELIVERY_FROM else DELIVERY_COST
    if quantity == 0:
        delivery = 0

    # Скидка на товары
    discount_percent = DISCOUNT_PERCENT if quantity >= DISCOUNT_FROM else 0
    discount_amount = int(subtotal * discount_percent / 100)

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
        lines.append(f"• {item['name']} — {qty} шт. × {price} ₽ = {price * qty} ₽")
    return "\n".join(lines)


def render_summary(summary: CartSummary, items=None, title: str = "🛒 Ваша корзина:") -> str:
    """Готовый текст с составом заказа и всеми надбавками. Один вид на всех экранах."""
    parts = [title]

    if items:
        parts.append("")
        parts.append(format_cart_lines(items))

    parts.append("")
    parts.append(f"💰 Сумма товаров: {summary.subtotal} ₽")

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


def hint_for(summary: CartSummary) -> str:
    """Подсказка-мотиватор для корзины. Пустая строка — если подсказывать нечего."""
    if summary.quantity == 0:
        return ""

    if summary.free_delivery and summary.discount_percent > 0:
        return "🎉 Доставка бесплатно + скидка 10% применена"

    if not summary.free_delivery and summary.to_free_delivery > 0:
        left = summary.to_free_delivery
        word = "брелок" if left == 1 else ("брелока" if left in (2, 3, 4) else "брелоков")
        return f"💡 Добавьте ещё {left} {word} — доставка бесплатно"

    if summary.free_delivery and summary.to_discount > 0:
        left = summary.to_discount
        word = "брелок" if left == 1 else ("брелока" if left in (2, 3, 4) else "брелоков")
        return f"🎁 Ещё {left} {word} — и скидка {DISCOUNT_PERCENT}%"

    return ""


def rules_text() -> str:
    """Описание условий акции — для всплывающей подсказки."""
    return (
        f"🚚 Доставка {DELIVERY_COST} ₽ — только за 1 брелок\n\n"
        f"От {FREE_DELIVERY_FROM} брелоков — доставка БЕСПЛАТНО 🎉\n"
        f"От {DISCOUNT_FROM} брелоков — ещё и скидка {DISCOUNT_PERCENT}% на товары 🎁"
    )
