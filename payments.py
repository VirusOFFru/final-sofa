"""
Интеграция с ЮKassa.

Если YOOKASSA_SHOP_ID / YOOKASSA_SECRET не заданы — is_configured() вернёт False,
и бот работает без онлайн-оплаты (заказ сразу уходит админу).

ВАЖНО: функции здесь СИНХРОННЫЕ (библиотека yookassa ходит по сети блокирующе).
Вызывайте их из бота только через asyncio.to_thread(...), иначе бот зависнет.
"""
import logging
import uuid

from yookassa import Configuration, Payment

from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET, BOT_USERNAME

logger = logging.getLogger(__name__)


def _configure() -> None:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET


def is_configured() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET)


def create_payment(amount: float, order_id: int, user_id: int, description: str) -> dict:
    """
    Создаёт платёж в ЮKassa.

    Возвращает: {"payment_id": ..., "confirmation_url": ..., "status": ...}
    """
    _configure()
    return_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me"

    payment = Payment.create(
        {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": {"order_id": order_id, "user_id": user_id},
        },
        str(uuid.uuid4()),  # идемпотентный ключ — защита от дублей
    )

    return {
        "payment_id": payment.id,
        "confirmation_url": payment.confirmation.confirmation_url,
        "status": payment.status,
    }


def check_payment(payment_id: str) -> str:
    """Статус платежа: pending / waiting_for_capture / succeeded / canceled."""
    _configure()
    return Payment.find_one(payment_id).status
