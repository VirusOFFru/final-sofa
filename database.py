"""
Слой работы с SQLite. Всё асинхронно (aiosqlite).
"""
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

        # ── Миграция старых БД: добавляем недостающие колонки ──────────────────
        async with db.execute("PRAGMA table_info(orders)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}

        for column, ddl in (
            ("comment",       "ALTER TABLE orders ADD COLUMN comment TEXT DEFAULT ''"),
            ("delivery_cost", "ALTER TABLE orders ADD COLUMN delivery_cost INTEGER DEFAULT 0"),
            ("payment_id",    "ALTER TABLE orders ADD COLUMN payment_id TEXT DEFAULT NULL"),
            ("payment_url",   "ALTER TABLE orders ADD COLUMN payment_url TEXT DEFAULT NULL"),
            ("track_number",  "ALTER TABLE orders ADD COLUMN track_number TEXT DEFAULT ''"),
        ):
            if column not in columns:
                await db.execute(ddl)

        await db.commit()


# ── Products ──────────────────────────────────────────────────────────────────

async def get_all_products(include_hidden: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = ("SELECT * FROM products ORDER BY id DESC" if include_hidden
                 else "SELECT * FROM products WHERE in_stock = 1 ORDER BY id DESC")
        async with db.execute(query) as cursor:
            return await cursor.fetchall()


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cursor:
            return await cursor.fetchone()


async def add_product(name: str, description: str, price: int, photo_id: str | None, collection_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO products (name, description, price, photo_id, collection_name) VALUES (?, ?, ?, ?, ?)",
            (name, description, price, photo_id, collection_name),
        )
        await db.commit()


async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.execute("DELETE FROM cart WHERE product_id = ?", (product_id,))
        await db.commit()


async def toggle_product_stock(product_id: int, in_stock: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET in_stock = ? WHERE id = ?", (in_stock, product_id))
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
            WHERE cart.user_id = ?
            ORDER BY cart.id DESC
            """,
            (user_id,),
        ) as cursor:
            return await cursor.fetchall()


async def remove_from_cart(cart_item_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart WHERE id = ?", (cart_item_id,))
        await db.commit()


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
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO orders (user_id, username, full_name, address, comment, total, delivery_cost, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, address, comment, total, delivery_cost, status),
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
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC, id DESC",
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


async def mark_order_paid(order_id: int) -> None:
    """Оплата подтверждена — заказ уходит в работу к админу."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = 'new' WHERE id = ?", (order_id,))
        await db.commit()
