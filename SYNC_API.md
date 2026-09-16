# 🔗 API для бота ВКонтакте

Telegram-бот — **главный**. У него база, каталог, цены, оплата и админка.
Бот ВК ничего не хранит у себя: всё берёт у Telegram-бота и всё отправляет ему.

```
 Покупатель ВК ──► Бот ВК ──HTTP──► Telegram-бот ──► Админ в Telegram
                     ▲                   │
                     └──── /api/events ◄─┘  (статусы заказов, ответы поддержки,
                                              «каталог изменился»)
```

## Включение

В переменных Telegram-бота:

| Переменная | Что это |
|---|---|
| `SYNC_API_TOKEN` | длинная случайная строка (от 32 символов). Пусто — API выключено |
| `SYNC_API_PORT` | порт (если не задан — берётся `PORT`, иначе `8080`) |
| `SYNC_API_HOST` | адрес (по умолчанию `0.0.0.0`) |

Сгенерировать токен: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

Этот же токен и публичный адрес Telegram-бота (на Bothost вида `https://bot-123.bothost.ru`)
указываются в боте ВК.

В логах при запуске должно появиться: `API для бота ВК запущено на 0.0.0.0:8080 ✅`

## Общие правила

- Каждый запрос (кроме `/api/ping`) — с заголовком `Authorization: Bearer <SYNC_API_TOKEN>`.
- Тела запросов и ответы — JSON (UTF-8).
- Успех: `{"ok": true, ...}`. Ошибка: `{"ok": false, "error": "код", "message": "текст", "details": ...}`.
- Коды: `400` — неверные данные, `401` — неверный токен, `404` — не найдено,
  `409` — конфликт (товара нет в продаже, неверный статус), `502` — Telegram не отдал фото, `500` — сбой.
- Цены **всегда** считает Telegram-бот. Бот ВК присылает только `product_id` и `quantity`.

---

## Каталог

### `GET /api/ping`
Проверка связи, без токена. → `{"ok": true, "api_version": 1}`

### `GET /api/catalog`
Товары, которые сейчас в продаже, и условия акции.

```json
{
  "ok": true,
  "version": "3f1c9a0b2d4e5f60",
  "settings": {
    "delivery_cost": 389, "free_delivery_sum": 1000,
    "discount_from_qty": 2, "discount_percent": 10,
    "rules_text": "🎁 От 2 брелоков — скидка 10% ...",
    "reviews_url": "https://t.me/...", "support_username": "ssarafos"
  },
  "products": [
    {
      "id": 7, "name": "Сова", "description": "...", "price": 700, "collection": "Лес",
      "photo": {"key": "a1b2c3d4e5f60718", "url": "/api/products/7/photo"},
      "inside": {
        "text": "Коробка, брелок, наклейка",
        "photos": [{"key": "…", "url": "/api/inside_photos/12"}]
      }
    }
  ]
}
```

- `photo` — `null`, если фото нет.
- `inside` — `null`, если «👀 Покажи что внутри» не заполнено → **кнопку не показывать**.
- `version` меняется при любом изменении каталога — можно не перекачивать каталог, если версия та же.
- `key` у фото меняется, только когда меняется само фото → по нему удобно кэшировать
  загруженные в ВК фотографии (не загружать повторно).

### `GET /api/products/{id}/photo` и `GET /api/inside_photos/{id}`
Байты JPEG (с тем же заголовком `Authorization`). Заголовок ответа `X-Photo-Key` — ключ фото.

---

## Корзина

### `POST /api/cart/quote`
Расчёт корзины по тем же правилам, что в Telegram.

```json
{"items": [{"product_id": 7, "quantity": 2}]}
```
→
```json
{
  "ok": true,
  "unavailable": [],
  "summary": {
    "items": [{"product_id": 7, "name": "Сова", "price": 700, "quantity": 2, "sum": 1400}],
    "quantity": 2, "subtotal": 1400,
    "discount_percent": 10, "discount_amount": 140,
    "delivery": 0, "free_delivery": true, "total": 1260,
    "to_free_delivery": 0, "to_discount": 0,
    "hints": ["🎉 Скидка 10% + бесплатная доставка!"],
    "text": "🛒 Ваша корзина: ... готовый текст ..."
  }
}
```
`unavailable` — id товаров, которых больше нет в продаже (их нужно убрать из корзины ВК).
Одинаковые `product_id` складываются. Максимум 50 позиций, до 99 шт. одного товара.

---

## Заказы

### `POST /api/orders` — оформить заказ
```json
{
  "vk_user_id": 555,
  "name": "Анна Иванова",
  "address": "г. Казань, ул. Баумана 1, кв. 2",
  "comment": "позвонить за час",
  "contact": "+79990000000",
  "client_order_id": "vk-555-1726500000",
  "return_url": "https://vk.com/club123",
  "items": [{"product_id": 7, "quantity": 2}]
}
```
Обязательны: `vk_user_id`, `address` (от 10 символов), `items`.

- `client_order_id` — **ключ от дублей**: если запрос повторить с тем же ключом
  (например, после обрыва связи), вернётся уже созданный заказ и `"duplicate": true`.
- `return_url` — куда вернуть покупателя после оплаты (только `https://`).

→
```json
{"ok": true, "duplicate": false, "payment_failed": false, "order": { ...заказ... }}
```

Если каких-то товаров уже нет: `409 products_unavailable`, в `details` — их id.

Админ сразу получает заказ в Telegram с пометкой «🟦 ВК».

**Заказ** в ответах выглядит так:
```json
{
  "id": 15, "status": "pending_payment", "status_text": "Ожидает оплаты",
  "total": 1260, "delivery_cost": 0, "track_number": "",
  "payment_url": "https://yoomoney.ru/...",
  "address": "...", "comment": "...", "created_at": "2026-09-16 12:00:00",
  "items": [{"product_id": 7, "name": "Сова", "price": 700, "quantity": 2}]
}
```
Статусы: `pending_payment` (ждёт оплаты), `new` (оплачен/принят), `shipped` (в пути),
`delivered` (доставлен), `cancelled` (отменён). `payment_url` есть только у `pending_payment`.
Если ЮKassa не подключена — заказ сразу `new`, если платёж не создался — `new` и `payment_failed: true`.

### `GET /api/orders?vk_user_id=555`
Последние 30 заказов пользователя → `{"ok": true, "orders": [...]}`

### `GET /api/orders/{id}?vk_user_id=555`
Один заказ (чужой — `404`).

### `POST /api/orders/{id}/check_payment` — кнопка «Проверить оплату»
```json
{"vk_user_id": 555}
```
→ `{"ok": true, "result": "...", "order": {...}}`, где `result`:

| result | Что сказать покупателю |
|---|---|
| `paid` | Оплата подтверждена, заказ принят |
| `pending` | Оплата ещё не поступила (ссылка `payment_url` остаётся!) |
| `cancelled` | Платёж отменён/истёк, заказ отменён — оформить заново |
| `timeout` / `error` | ЮKassa не ответила — попробовать через минуту |
| `not_pending` | Заказ уже не ждёт оплаты — показать `order.status_text` |
| `already_processed` | Статус только что изменился — показать `order.status_text` |
| `no_payment` | Платёж не найден — в поддержку |

### `POST /api/orders/{id}/received` — «Я получил заказ»
`{"vk_user_id": 555}` → заказ становится `delivered`. Только для `shipped`, иначе `409 wrong_status`.

---

## Поддержка

### `POST /api/support`
```json
{"vk_user_id": 555, "name": "Анна", "text": "Где мой заказ?", "attachments": ["https://vk.com/photo..."]}
```
Нужен `text` или `attachments` (до 10 https-ссылок). Текст до 3000 символов.
→ `{"ok": true, "message_id": 3}`

Админ получает сообщение в Telegram с кнопкой **«↩️ Ответить»**.
Ответ админа придёт боту ВК событием `support_reply`.

---

## События (что бот ВК должен доставить)

### `GET /api/events?limit=50`
Опрашивать раз в 2–5 секунд.
```json
{"ok": true, "events": [
  {"id": 1, "type": "order_status", "user_id": "555", "created_at": "...",
   "payload": {"order_id": 15, "status": "shipped", "status_text": "В пути",
               "track_number": "RR123", "text": "🚚 Ваш заказ #15 отправлен! ..."}},
  {"id": 2, "type": "support_reply", "user_id": "555",
   "payload": {"text": "Заказ уже в пути!", "reply_to_message_id": 3}},
  {"id": 3, "type": "catalog_changed", "user_id": "", "payload": {}}
]}
```

| type | Что сделать боту ВК |
|---|---|
| `order_status` | Отправить пользователю `user_id` текст `payload.text` |
| `support_reply` | Отправить пользователю `user_id` ответ поддержки `payload.text` |
| `catalog_changed` | Перекачать `/api/catalog` |

### `POST /api/events/ack`
```json
{"ids": [1, 2, 3]}
```
Подтверждение, что события обработаны — они удаляются из очереди.
**Подтверждайте только после успешной отправки.** Неподтверждённые события
будут выданы снова при следующем запросе (доставка «хотя бы один раз»).
