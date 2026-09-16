"""
Слой работы с SQLite. Всё асинхронно (aiosqlite).
"""
import json
import os

import aiosqlite

from config import DB_PATH


def ensure_db_dir() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


async def init_db() -> None:
    ensure_db_dir()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price INTEGER NOT NULL,
                photo_id TEXT DEFAULT NULL,
                collection_name TEXT DEFAULT '',
                in_stock INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER DEFAULT 1,
                UNIQUE(user_id, product_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                address TEXT NOT NULL,
                comment TEXT DEFAULT '',
                total INTEGER NOT NULL,
                delivery_cost INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                track_number TEXT DEFAULT '',
                payment_id TEXT DEFAULT NULL,
                payment_url TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL
            )
        """)

        # «👀 Покажи что внутри» — фото комплектации (необязательно)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_inside_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                position INTEGER DEFAULT 0
            )
        """)
        # Очередь событий для бота ВКонтакте (статусы заказов, ответы поддержки и т.д.)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                fetched INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Сообщения в поддержку из других площадок (ВК)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                user_name TEXT DEFAULT '',
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Миграция старых БД: добавляем недостающие колонки ──────────────────
        await _add_missing_columns(db, "orders", (
            ("comment",          "ALTER TABLE orders ADD COLUMN comment TEXT DEFAULT ''"),
            ("delivery_cost",    "ALTER TABLE orders ADD COLUMN delivery_cost INTEGER DEFAULT 0"),
            ("payment_id",       "ALTER TABLE orders ADD COLUMN payment_id TEXT DEFAULT NULL"),
            ("payment_url",      "ALTER TABLE orders ADD COLUMN payment_url TEXT DEFAULT NULL"),
            ("track_number",     "ALTER TABLE orders ADD COLUMN track_number TEXT DEFAULT ''"),
            # откуда заказ: 'tg' или 'vk'
            ("source",           "ALTER TABLE orders ADD COLUMN source TEXT DEFAULT 'tg'"),
            # ID покупателя на другой площадке (для ВК — id пользователя ВК)
            ("external_user_id", "ALTER TABLE orders ADD COLUMN external_user_id TEXT DEFAULT ''"),
            # контакт покупателя (телефон и т.п.), если площадка его передала
            ("contact",          "ALTER TABLE orders ADD COLUMN contact TEXT DEFAULT ''"),
            # ключ идемпотентности от внешнего бота — защита от дублей заказа
            ("external_key",     "ALTER TABLE orders ADD COLUMN external_key TEXT DEFAULT ''"),
        ))
        await _add_missing_columns(db, "products", (
            ("inside_text", "ALTER TABLE products ADD COLUMN inside_text TEXT DEFAULT ''"),
        ))

        await db.commit()


async def _add_missing_columns(db, table: str, columns_ddl) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    for column, ddl in columns_ddl:
        if column not in existing:
            await db.execute(ddl)


# ── Products ──────────────────────────────────────────────────────────────────

_PRODUCT_SELECT = """
    SELECT products.*,
           (SELECT COUNT(*) FROM product_inside_photos p WHERE p.product_id = products.id) AS inside_photos_count
    FROM products
"""


async def get_all_products(include_hidden: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _PRODUCT_SELECT + ("" if include_hidden else " WHERE in_stock = 1") + " ORDER BY id DESC"
        async with db.execute(query) as cursor:
            return await cursor.fetchall()


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(_PRODUCT_SELECT + " WHERE products.id = ?", (product_id,)) as cursor:
            return await cursor.fetchone()


def has_inside(product) -> bool:
    """Есть ли у товара «👀 Покажи что внутри» (текст или хотя бы одно фото)."""
    keys = product.keys()
    text = (product["inside_text"] if "inside_text" in keys else "") or ""
    count = int(product["inside_photos_count"]) if "inside_photos_count" in keys else 0
    return bool(text.strip()) or count > 0


async def add_product(
    name: str,
    description: str,
    price: int,
    photo_id: str | None,
    collection_name: str,
    inside_text: str = "",
    inside_photos: list[str] | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO products (name, description, price, photo_id, collection_name, inside_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, price, photo_id, collection_name, inside_text or ""),
        )
        product_id = cursor.lastrowid
        for position, file_id in enumerate(inside_photos or []):
            await db.execute(
                "INSERT INTO product_inside_photos (product_id, file_id, position) VALUES (?, ?, ?)",
                (product_id, file_id, position),
            )
        await _catalog_changed(db)
        await db.commit()
        return product_id


# Поля товара, которые админ может менять (белый список — защита от SQL-инъекций)
EDITABLE_PRODUCT_FIELDS = {"name", "description", "price", "photo_id", "collection_name", "inside_text"}


async def update_product_field(product_id: int, field: str, value) -> bool:
    """Меняет одно поле товара. Возвращает True, если товар найден и обновлён."""
    if field not in EDITABLE_PRODUCT_FIELDS:
        raise ValueError(f"Недопустимое поле товара: {field}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE products SET {field} = ? WHERE id = ?",
            (value, product_id),
        )
        updated = cursor.rowcount > 0
        if updated:
            await _catalog_changed(db)
        await db.commit()
        return updated


async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.execute("DELETE FROM cart WHERE product_id = ?", (product_id,))
        await db.execute("DELETE FROM product_inside_photos WHERE product_id = ?", (product_id,))
        await _catalog_changed(db)
        await db.commit()


async def toggle_product_stock(product_id: int, in_stock: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET in_stock = ? WHERE id = ?", (in_stock, product_id))
        await _catalog_changed(db)
        await db.commit()


# ── «👀 Покажи что внутри» ─────────────────────────────────────────────────────

async def get_inside_photos(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM product_inside_photos WHERE product_id = ? ORDER BY position, id",
            (product_id,),
        ) as cursor:
            return await cursor.fetchall()


async def get_inside_photo(photo_row_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM product_inside_photos WHERE id = ?", (photo_row_id,)) as cursor:
            return await cursor.fetchone()


async def replace_inside_photos(product_id: int, file_ids: list[str]) -> None:
    """Полностью заменяет фото «что внутри» (пустой список — удалить все)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM product_inside_photos WHERE product_id = ?", (product_id,))
        for position, file_id in enumerate(file_ids):
            await db.execute(
                "INSERT INTO product_inside_photos (product_id, file_id, position) VALUES (?, ?, ?)",
                (product_id, file_id, position),
            )
        await _catalog_changed(db)
        await db.commit()


async def clear_inside(product_id: int) -> None:
    """Убирает «Покажи что внутри» полностью — кнопка у товара пропадёт."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET inside_text = '' WHERE id = ?", (product_id,))
        await db.execute("DELETE FROM product_inside_photos WHERE product_id = ?", (product_id,))
        await _catalog_changed(db)
        await db.commit()


# ── Cart ──────────────────────────────────────────────────────────────────────

async def add_to_cart(user_id: int, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, quantity FROM cart WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            await db.execute("UPDATE cart SET quantity = quantity + 1 WHERE id = ?", (row[0],))
        else:
            await db.execute(
                "INSERT INTO cart (user_id, product_id, quantity) VALUES (?, ?, 1)",
                (user_id, product_id),
            )
        await db.commit()


async def get_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT cart.id, cart.user_id, cart.product_id, cart.quantity,
                   products.name, products.price, products.photo_id
            FROM cart
            JOIN products ON products.id = cart.product_id
            WHERE cart.user_id = ? AND products.in_stock = 1
            ORDER BY cart.id DESC
            """,
            (user_id,),
        ) as cursor:
            return await cursor.fetchall()


async def remove_one_from_cart(cart_item_id: int, user_id: int) -> bool:
    """
    Убирает из корзины ОДНУ штуку товара.
    Если это была последняя штука — позиция удаляется целиком.
    Возвращает True, если что-то было убрано.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT quantity FROM cart WHERE id = ? AND user_id = ?",
            (cart_item_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return False

        if int(row[0]) > 1:
            await db.execute(
                "UPDATE cart SET quantity = quantity - 1 WHERE id = ? AND user_id = ?",
                (cart_item_id, user_id),
            )
        else:
            await db.execute(
                "DELETE FROM cart WHERE id = ? AND user_id = ?",
                (cart_item_id, user_id),
            )
        await db.commit()
        return True


async def clear_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        await db.commit()


# ── Orders ────────────────────────────────────────────────────────────────────

async def create_order(
    user_id: int,
    username: str,
    full_name: str,
    address: str,
    comment: str,
    total: int,
    delivery_cost: int,
    items: list[dict],
    status: str = "new",
    source: str = "tg",
    external_user_id: str = "",
    contact: str = "",
    external_key: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO orders (user_id, username, full_name, address, comment, total, delivery_cost, status, "
            "source, external_user_id, contact, external_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, address, comment, total, delivery_cost, status,
             source, str(external_user_id), contact, external_key),
        )
        order_id = cursor.lastrowid

        for item in items:
            await db.execute(
                "INSERT INTO order_items (order_id, product_id, product_name, price, quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, item["product_id"], item["name"], item["price"], item["quantity"]),
            )

        await db.commit()
        return order_id


async def get_all_orders(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return await cursor.fetchall()


async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cursor:
            return await cursor.fetchone()


async def get_order_items(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)) as cursor:
            return await cursor.fetchall()


async def get_order_by_external_key(source: str, external_key: str):
    if not external_key:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE source = ? AND external_key = ?",
            (source, external_key),
        ) as cursor:
            return await cursor.fetchone()


async def get_external_user_orders(source: str, external_user_id: str, limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE source = ? AND external_user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (source, str(external_user_id), limit),
        ) as cursor:
            return await cursor.fetchall()


async def update_order_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()


async def update_track_number(order_id: int, track_number: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET track_number = ?, status = 'shipped' WHERE id = ?",
            (track_number, order_id),
        )
        await db.commit()


async def get_user_orders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE user_id = ? AND source = 'tg' ORDER BY created_at DESC, id DESC",
            (user_id,),
        ) as cursor:
            return await cursor.fetchall()


# ── ЮKassa ────────────────────────────────────────────────────────────────────

async def set_payment_info(order_id: int, payment_id: str, payment_url: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET payment_id = ?, payment_url = ? WHERE id = ?",
            (payment_id, payment_url, order_id),
        )
        await db.commit()


async def get_order_by_payment(payment_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE payment_id = ?", (payment_id,)) as cursor:
            return await cursor.fetchone()


async def mark_order_paid(order_id: int) -> bool:
    """
    Оплата подтверждена — заказ уходит в работу к админу.
    Срабатывает только для заказа «Ожидает оплаты» — защита от двойного нажатия.
    Возвращает True, если статус действительно изменился.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE orders SET status = 'new' WHERE id = ? AND status = 'pending_payment'",
            (order_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def cancel_unpaid_order(order_id: int) -> bool:
    """Платёж отменён/истёк — отменяем заказ, если он всё ещё ждал оплаты."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'pending_payment'",
            (order_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def restore_cart_from_order(order_id: int, user_id: int) -> int:
    """
    Возвращает товары из заказа обратно в корзину (только те, что ещё в продаже).
    Возвращает количество возвращённых штук.
    """
    restored = 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT order_items.product_id, order_items.quantity
            FROM order_items
            JOIN products ON products.id = order_items.product_id
            WHERE order_items.order_id = ? AND products.in_stock = 1
            """,
            (order_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        for product_id, quantity in rows:
            async with db.execute(
                "SELECT id FROM cart WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                await db.execute(
                    "UPDATE cart SET quantity = quantity + ? WHERE id = ?",
                    (quantity, existing[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO cart (user_id, product_id, quantity) VALUES (?, ?, ?)",
                    (user_id, product_id, quantity),
                )
            restored += int(quantity)

        await db.commit()
    return restored


# ── Синхронизация с другими ботами (ВК) ───────────────────────────────────────

async def _catalog_changed(db) -> None:
    """
    Кладёт в очередь событие «каталог изменился» (если такое ещё не забрано ботом ВК).
    Вызывается внутри уже открытого соединения, commit делает вызывающий.
    """
    async with db.execute(
        "SELECT 1 FROM sync_events WHERE target = 'vk' AND event_type = 'catalog_changed' AND fetched = 0 LIMIT 1"
    ) as cursor:
        if await cursor.fetchone():
            return
    await db.execute(
        "INSERT INTO sync_events (target, event_type, user_id, payload) VALUES ('vk', 'catalog_changed', '', '{}')"
    )


async def add_event(target: str, event_type: str, user_id: str, payload: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO sync_events (target, event_type, user_id, payload) VALUES (?, ?, ?, ?)",
            (target, event_type, str(user_id), json.dumps(payload, ensure_ascii=False)),
        )
        await db.commit()
        return cursor.lastrowid


async def fetch_events(target: str, limit: int = 50) -> list[dict]:
    """Отдаёт неподтверждённые события (старые — первыми) и помечает их как забранные."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sync_events WHERE target = ? ORDER BY id LIMIT ?",
            (target, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            ids = [row["id"] for row in rows]
            await db.execute(
                f"UPDATE sync_events SET fetched = 1 WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            await db.commit()
    return [
        {
            "id": row["id"],
            "type": row["event_type"],
            "user_id": row["user_id"],
            "payload": json.loads(row["payload"] or "{}"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def ack_events(target: str, ids: list[int]) -> int:
    """Бот ВК подтвердил обработку — удаляем события из очереди."""
    ids = [int(i) for i in ids][:500]
    if not ids:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"DELETE FROM sync_events WHERE target = ? AND id IN ({','.join('?' * len(ids))})",
            [target, *ids],
        )
        await db.commit()
        return cursor.rowcount


async def add_support_message(source: str, external_user_id: str, user_name: str, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO support_messages (source, external_user_id, user_name, text) VALUES (?, ?, ?, ?)",
            (source, str(external_user_id), user_name, text),
        )
        await db.commit()
        return cursor.lastrowid


async def get_support_message(message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM support_messages WHERE id = ?", (message_id,)) as cursor:
            return await cursor.fetchone()
