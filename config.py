"""
Конфигурация бота.

Все секреты берутся ТОЛЬКО из переменных окружения (.env локально
или ENV-переменные на хостинге вроде Bothost).
Реальные значения в код не прописываем — иначе токен утечёт в git.
"""
import os
import re

try:  # python-dotenv может отсутствовать (например, на хостинге ENV задаётся извне)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# ── Обязательное ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ── Данные магазина ───────────────────────────────────────────────────────────
# ADMIN_ID — ваш numeric Telegram ID. Если 0 — админка недоступна никому.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

# Username поддержки без @
SUPPORT_USERNAME = (os.getenv("SUPPORT_USERNAME", "") or "").lstrip("@")

# Username бота без @ — используется как return_url после оплаты
BOT_USERNAME = (os.getenv("BOT_USERNAME", "") or "").lstrip("@")

# Путь к файлу БД. На хостинге удобно задать DATA_DIR=/app/data
DATA_DIR = os.getenv("DATA_DIR", "").strip()
DB_PATH = os.getenv("DB_PATH", "").strip() or (os.path.join(DATA_DIR, "shop.db") if DATA_DIR else "data/shop.db")

# ── Логика магазина ───────────────────────────────────────────────────────────
# Стоимость доставки (если заказ не набрал сумму для бесплатной доставки)
DELIVERY_COST = int(os.getenv("DELIVERY_COST", "389") or 389)

# Бесплатная доставка — если сумма БРЕЛОКОВ (без учёта доставки и до скидки)
# не меньше этой суммы, ₽.
# ВНИМАНИЕ: старая переменная FREE_DELIVERY_FROM (порог в штуках) больше НЕ используется.
FREE_DELIVERY_SUM = int(os.getenv("FREE_DELIVERY_SUM", "1000") or 1000)

# От скольких брелоков (штук) действует скидка.
# ВНИМАНИЕ: старая переменная DISCOUNT_FROM больше НЕ используется.
DISCOUNT_FROM_QTY = int(os.getenv("DISCOUNT_FROM_QTY", "2") or 2)

# Размер скидки в процентах
DISCOUNT_PERCENT = int(os.getenv("DISCOUNT_PERCENT", "10") or 10)

# ── Ссылки ────────────────────────────────────────────────────────────────────
# Ссылка на группу/канал с отзывами. Можно указать в любом виде:
#   https://t.me/my_reviews   или   t.me/my_reviews   или   @my_reviews
REVIEWS_URL = (os.getenv("REVIEWS_URL", "") or "").strip()


def normalize_url(raw: str) -> str:
    """Приводит ссылку к виду https://... (принимает @group, t.me/group, group)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    if raw.startswith(("https://", "http://", "tg://")):
        return raw
    if raw.startswith(("t.me/", "telegram.me/")):
        return f"https://{raw}"
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        return f"https://t.me/{raw}"
    return f"https://{raw}"


# ── Связь с ботом ВКонтакте (необязательно) ───────────────────────────────────
# Если SYNC_API_TOKEN пуст — API для бота ВК не запускается, всё работает как раньше.
# Токен — длинная случайная строка; ТОТ ЖЕ токен указывается в боте ВК.
SYNC_API_TOKEN = (os.getenv("SYNC_API_TOKEN", "") or "").strip()
SYNC_API_HOST = (os.getenv("SYNC_API_HOST", "") or "0.0.0.0").strip()
# Порт: SYNC_API_PORT, иначе PORT (его часто задаёт хостинг), иначе 8080
SYNC_API_PORT = int(os.getenv("SYNC_API_PORT", "") or os.getenv("PORT", "") or 8080)

# ── ЮKassa (необязательно) ────────────────────────────────────────────────────
# Если пусто — оплата отключается, заказы приходят сразу в статусе «Новый».
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET = os.getenv("YOOKASSA_SECRET", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Заполните .env (см. .env.example) — "
        "токен возьмите у @BotFather."
    )
