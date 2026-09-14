"""
Конфигурация бота.

Все секреты берутся ТОЛЬКО из переменных окружения (.env локально
или ENV-переменные на хостинге вроде Bothost).
Реальные значения в код не прописываем — иначе токен утечёт в git.
"""
import os

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
# Доставка платная только за 1 брелок
DELIVERY_COST = int(os.getenv("DELIVERY_COST", "389") or 389)

# От скольки брелоков доставка бесплатная
FREE_DELIVERY_FROM = int(os.getenv("FREE_DELIVERY_FROM", "2") or 2)

# От скольки брелоков действует скидка
DISCOUNT_FROM = int(os.getenv("DISCOUNT_FROM", "3") or 3)

# Размер скидки в процентах
DISCOUNT_PERCENT = int(os.getenv("DISCOUNT_PERCENT", "10") or 10)

# ── ЮKassa (необязательно) ────────────────────────────────────────────────────
# Если пусто — оплата отключается, заказы приходят сразу в статусе «Новый».
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET = os.getenv("YOOKASSA_SECRET", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Заполните .env (см. .env.example) — "
        "токен возьмите у @BotFather."
    )
