"""
NFT Gift Listing Tracker для Telegram Stars Marketplace
=========================================================

Отслеживает новые листинги (выставления на продажу) подарков-коллекционок
на внутреннем маркете Telegram через пользовательскую сессию (Telethon)
и шлёт уведомления в указанный чат/канал.

Использует официальный MTProto-метод payments.getResaleStarGifts.

Запуск:
    python tracker.py            # обычный режим
    python tracker.py --debug    # напечатать сырые объекты (для отладки полей)
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gift_tracker")

DEBUG = "--debug" in sys.argv

# Сколько элементов истории "виденных" листингов храним на каждый тип подарка
SEEN_HISTORY_LIMIT = 500


# ---------------------------------------------------------------------------
# "Кнопка" в стиле style=success/danger/... — у Telegram Bot API НЕТ цветных
# кнопок и НЕТ поддержки кастомных премиум-эмодзи (tg-emoji) в тексте кнопки —
# только обычный unicode. Поэтому style эмулируется префиксом-эмодзи, это
# чисто визуальный костыль, а не настоящий цвет кнопки.
# ---------------------------------------------------------------------------

STYLE_EMOJI = {
    "success": "✅",
    "danger": "❌",
    "primary": "🔵",
    "secondary": "⚪️",
}


def premium_button(text: str, url: str, emoji_id: str = None, style: str = "primary") -> dict:
    """
    Аналог PremiumButton(text=..., emoji_id=..., callback_data/url=..., style=...)
    в терминах обычной Telegram inline-кнопки.

    ВАЖНО:
    - emoji_id (кастомный премиум-эмодзи) в кнопках Telegram не отображается —
      это ограничение самого Bot API, а не наше. Параметр принимается для
      совместимости сигнатуры, но не используется — оставлен явно, чтобы не
      делать вид, что он работает.
    - "цвет" — это только эмодзи-префикс по style, реальной покраски кнопки
      Telegram не даёт.
    - Если у вас есть готовая реализация PremiumButton из другого проекта —
      пришлите её, подключим вместо этой заглушки.
    """
    prefix = STYLE_EMOJI.get(style, "")
    label = f"{prefix} {text}".strip()
    return {"text": label, "url": url}


# ---------------------------------------------------------------------------
# Отправка уведомлений через Bot API (от имени бота, не от личной сессии)
# ---------------------------------------------------------------------------

def send_via_bot(bot_token: str, chat_id, text: str, topic_id: int = None):
    """
    Отправляет сообщение от имени бота через Bot API.
    Возвращает (ok: bool, retry_after: int | None) — чтобы вызывающий код
    мог сам решить, ждать и повторять, или нет (раньше ошибка просто
    логировалась и сообщение терялось навсегда).
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                premium_button(
                    text="Забрать",
                    url=text_gift_url(text),
                    emoji_id="6007983438294949171",
                    style="success",
                )
            ]]
        },
    }
    if topic_id:
        payload["message_thread_id"] = topic_id

    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as e:
        log.error(f"Bot API sendMessage network error: {e}")
        return False, 5  # временная сетевая проблема — попробуем ещё раз чуть позже

    if resp.ok:
        return True, None

    retry_after = None
    if resp.status_code == 429:
        try:
            retry_after = resp.json().get("parameters", {}).get("retry_after")
        except Exception:
            pass
        retry_after = retry_after or 5

    log.error(f"Bot API sendMessage failed: {resp.status_code} {resp.text}")
    return False, retry_after


def text_gift_url(text: str) -> str:
    """Достаёт ссылку на подарок (t.me/nft/...) из уже сформированного HTML-текста."""
    import re
    m = re.search(r'href="(https://t\.me/nft/[^"]+)"', text)
    return m.group(1) if m else "https://t.me"


# ---------------------------------------------------------------------------
# Очередь уведомлений — гарантирует ровно 1 сообщение в notify_interval сек.
# и НЕ теряет сообщения при 429: то же самое сообщение ждёт retry_after и
# отправляется повторно, следующее из очереди не отправляется, пока это не
# ушло. Раньше при 429 сообщение просто пропадало навсегда.
# ---------------------------------------------------------------------------

class NotifyQueue:
    def __init__(self, config: dict):
        self.config = config
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.interval = float(config.get("notify_interval", 1))  # 1 сообщение / 1 сек по умолчанию

    async def put(self, listing_id: str, text: str) -> None:
        await self.queue.put((listing_id, text))

    async def run(self) -> None:
        bot_token = self.config.get("bot_token")
        if not bot_token:
            raise RuntimeError("bot_token не задан в config.json")

        while True:
            listing_id, text = await self.queue.get()
            while True:
                ok, retry_after = await asyncio.to_thread(
                    send_via_bot,
                    bot_token,
                    self.config["target_chat"],
                    text,
                    self.config.get("target_topic_id"),
                )
                if ok:
                    log.info(f"Отправлено: {listing_id}")
                    break
                wait_s = (retry_after or 5) + 1
                log.warning(f"Не отправлено ({listing_id}), повтор через {wait_s}s")
                await asyncio.sleep(wait_s)
            self.queue.task_done()
            await asyncio.sleep(self.interval)


# ---------------------------------------------------------------------------
# Конфиг / состояние
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Не найден {CONFIG_PATH}. Скопируйте config.example.json -> config.json "
            f"и заполните api_id / api_hash / target_chat."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "gift_types_cache": None, "gift_types_ts": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Работа с Telegram API
# ---------------------------------------------------------------------------

async def get_resalable_gift_types(client: TelegramClient) -> list:
    """
    Возвращает список типов подарков (gift_id + название), для которых
    в принципе доступна перепродажа (флаг availability_resale).
    """
    result = await client(functions.payments.GetStarGiftsRequest(hash=0))
    gifts = getattr(result, "gifts", [])
    resalable = []
    for g in gifts:
        has_resale = getattr(g, "availability_resale", None)
        if has_resale:
            resalable.append({
                "gift_id": g.id,
                "title": getattr(g, "title", str(g.id)),
            })
    return resalable


async def fetch_resale_page(client: TelegramClient, gift_id: int, limit: int = 25):
    """
    Одна страница листингов конкретного типа подарка.
    Без sort_by_price / sort_by_num -> сортировка по времени последнего
    изменения цены/выставления (descending) — то есть самые свежие первые.
    """
    return await client(
        functions.payments.GetResaleStarGiftsRequest(
            gift_id=gift_id,
            offset="",
            limit=limit,
        )
    )


def extract_listing_id(item) -> str:
    """
    Уникальный идентификатор конкретного экземпляра-листинга.
    slug обычно вида 'GiftName-1234' — он же используется в ссылке t.me/nft/<slug>.
    """
    slug = getattr(item, "slug", None)
    if slug:
        return slug
    return str(getattr(item, "id", getattr(item, "num", "unknown")))


def format_price(item):
    """
    Возвращает (stars, ton) как числа (float/int) или (None, None).
    Поле с ценой перепродажи — список объектов StarsAmount / StarsTonAmount,
    а не готовая строка, поэтому их нужно разобрать по типу.
    StarsAmount:    amount:long nanos:int      -> звёзды (amount + nanos/1e9)
    StarsTonAmount: amount:long (в нанотонах)  -> TON (amount / 1e9)
    """
    val = None
    for attr in ("resell_stars", "resell_amount", "resale_amount", "resell_price"):
        val = getattr(item, attr, None)
        if val is not None:
            break
    if val is None:
        return None, None

    if not isinstance(val, (list, tuple)):
        val = [val]

    stars = ton = None
    for v in val:
        cls_name = type(v).__name__
        if cls_name == "StarsAmount":
            amount = getattr(v, "amount", 0) or 0
            nanos = getattr(v, "nanos", 0) or 0
            stars = amount + nanos / 1_000_000_000
        elif cls_name == "StarsTonAmount":
            amount = getattr(v, "amount", 0) or 0
            ton = amount / 1_000_000_000
    return stars, ton


def format_number(n):
    if n is None:
        return "?"
    if float(n).is_integer():
        return str(int(n))
    return f"{n:.2f}"


def html_escape(s) -> str:
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def emoji(custom_id: str, fallback: str) -> str:
    """tg-emoji тег: показывает premium-эмодзи по ID, с обычным эмодзи как fallback.
    Работает только внутри ТЕКСТА СООБЩЕНИЯ (parse_mode=HTML), не в кнопках."""
    return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'


def format_message(gift_title: str, item, users_by_id: dict) -> str:
    slug = extract_listing_id(item)
    stars, ton = format_price(item)
    attrs = getattr(item, "attributes", []) or []

    model = pattern = backdrop = "—"
    for a in attrs:
        cls_name = type(a).__name__.lower()
        if "model" in cls_name:
            model = getattr(a, "name", model)
        elif "pattern" in cls_name:
            pattern = getattr(a, "name", pattern)
        elif "backdrop" in cls_name:
            backdrop = getattr(a, "name", backdrop)

    owner_peer = getattr(item, "owner_id", None)
    owner_user_id = getattr(owner_peer, "user_id", None) if owner_peer else None
    owner = users_by_id.get(owner_user_id) if owner_user_id else None

    username = getattr(owner, "username", None) if owner else None
    seller_line = f"@{html_escape(username)}" if username else "неизвестен"
    seller_line += f" (<code>{owner_user_id or '?'}</code>)"

    # ⚠️ В ответе payments.GetResaleStarGiftsRequest нет поля с "уровнем по
    # потраченным звёздам" пользователя — такого публичного API-метода не
    # существует, поэтому тут принципиально прочерк, а не баг.
    level = "—"

    paid_stars = getattr(owner, "send_paid_messages_stars", None) if owner else None
    message_line = f"{format_number(paid_stars)} {emoji('6028338546736107668', '⭐')}" if paid_stars else "Бесплатно"

    premium = getattr(owner, "premium", False) if owner else False
    status = "Premium" if premium else "Обычный"

    gift_display_id = slug.split("-")[-1] if "-" in slug else slug

    return (
        "🎉 <b>НОВЫЙ ЛИСТИНГ</b>\n\n"
        f"{emoji('6032644646587338669', '🎁')} Гифт: {html_escape(gift_title)}\n"
        f"{emoji('6037083366438737901', '💰')} Цена: {format_number(stars)} {emoji('6028338546736107668', '⭐')}"
        f" / {format_number(ton)} {emoji('5264781253718090745', '💎')}\n"
        f"{emoji('5938437708635443119', '📝')} Модель: {html_escape(model)}\n"
        f"{emoji('6030466823290360017', '🖼')} Фон: {html_escape(backdrop)}\n"
        f"{emoji('5767244474040192942', '⭐')} Узор: {html_escape(pattern)}\n"
        f"{emoji('6035084557378654059', '👤')} Продавец: {seller_line}\n"
        f"{emoji('5890925363067886150', '📈')} Level: {level}\n"
        f"{emoji('6034831751308644168', '📢')} Сообщение: {message_line}\n"
        f"{emoji('6028346797368283073', '⭐')} Статус: {status}\n"
        f"{emoji('6028171274939797252', '▶️')} <a href=\"https://t.me/nft/{slug}\">{html_escape(gift_title.replace(' ', ''))}-{gift_display_id}</a>\n"
        f"{emoji('6039486778597970865', '⏰')} {time.strftime('%d.%m.%Y:%H:%M:%S')}\n\n"
        f"{emoji('5210956306952758910', '✅')} Created @ddelitpr"
    )


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

async def main():
    config = load_config()
    state = load_state()

    session = StringSession(config.get("session_string") or "")
    client = TelegramClient(session, config["api_id"], config["api_hash"])

    await client.start(phone=config.get("phone") or None)

    if not config.get("session_string"):
        log.info("Сохраните эту session_string в config.json, чтобы не логиниться заново:")
        print(client.session.save())

    poll_interval = float(config.get("poll_interval", 4))
    request_delay = float(config.get("request_delay", 1.5))
    gift_filter = set(config.get("gift_ids") or [])  # пусто = отслеживать все
    refresh_types_every = int(config.get("refresh_gift_types_seconds", 600))

    notify_queue = NotifyQueue(config)
    sender_task = asyncio.create_task(notify_queue.run())

    while True:
        try:
            now = time.time()
            if not state.get("gift_types_cache") or now - state.get("gift_types_ts", 0) > refresh_types_every:
                log.info("Обновляю список типов подарков с возможностью перепродажи...")
                gift_types = await get_resalable_gift_types(client)
                state["gift_types_cache"] = gift_types
                state["gift_types_ts"] = now
                save_state(state)
            else:
                gift_types = state["gift_types_cache"]

            if gift_filter:
                gift_types = [g for g in gift_types if g["gift_id"] in gift_filter]

            for gt in gift_types:
                gift_id = gt["gift_id"]
                title = gt["title"]

                try:
                    page = await fetch_resale_page(client, gift_id)
                except FloodWaitError as e:
                    log.warning(f"FloodWait {e.seconds}s — пауза")
                    await asyncio.sleep(e.seconds + 1)
                    continue

                items = getattr(page, "gifts", [])
                users_by_id = {u.id: u for u in getattr(page, "users", [])}
                if DEBUG and items:
                    log.info("DEBUG raw item:\n%s", items[0].stringify())

                seen_key = str(gift_id)
                # ВАЖНО: список, а не set — порядок вставки важен для корректной
                # обрезки истории. Раньше был set(), и list(set)[-500:] мог
                # случайно выкинуть недавно виденные id вместо старых — из-за
                # этого уже отправленные листинги иногда снова считались
                # "новыми" и уходили повторно.
                seen_list = list(state["seen"].get(seen_key, []))
                seen_ids = set(seen_list)
                is_first_run_for_type = seen_key not in state["seen"]

                new_ids = []
                for item in items:
                    listing_id = extract_listing_id(item)
                    if listing_id not in seen_ids:
                        new_ids.append(listing_id)
                        seen_ids.add(listing_id)
                        if not is_first_run_for_type:
                            # первый прогон по новому типу подарка — не спамим
                            # историческими листингами, только помечаем как виденные
                            text = format_message(title, item, users_by_id)
                            await notify_queue.put(listing_id, text)
                            log.info(f"В очередь: {title} / {listing_id}")

                if new_ids:
                    seen_list.extend(new_ids)
                    # обрезаем строго с начала (старые первыми), список
                    # сохраняет порядок появления в отличие от set
                    if len(seen_list) > SEEN_HISTORY_LIMIT:
                        seen_list = seen_list[-SEEN_HISTORY_LIMIT:]
                    state["seen"][seen_key] = seen_list
                    save_state(state)

                # пауза между разными типами подарков, чтобы не бомбить API
                # пачкой запросов подряд (настраивается через request_delay)
                await asyncio.sleep(request_delay)

        except Exception as e:
            log.exception(f"Ошибка в основном цикле: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    asyncio.run(main())