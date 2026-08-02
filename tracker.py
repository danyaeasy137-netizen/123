"""
NFT Gift Listing Tracker + Telegram-бот (всё в одном процессе)
================================================================

Компонент 1 (Telethon, пользовательская сессия): читает внутренний маркет
подарков Telegram (payments.getResaleStarGifts) и находит новые листинги.

Компонент 2 (aiogram, бот по токену): постит найденные листинги в чат по
темам в зависимости от цены, обрабатывает /start (гейт на подписку на
канал) и inline-кнопку "Взять лог" (удаляет пост и шлёт детали в ЛС
забравшему).

Оба компонента работают в одном asyncio-цикле через asyncio.gather.

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

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
CLAIMS_PATH = BASE_DIR / "claims.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gift_tracker")

DEBUG = "--debug" in sys.argv
SEEN_HISTORY_LIMIT = 500


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Не найден {CONFIG_PATH}.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


config = load_config()

bot = Bot(token=config["bot_token"], default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

SUB_CHANNEL = config.get("subscribe_channel", "rocketgiftss")  # без @
SUB_URL = config.get("subscribe_url", f"https://t.me/{SUB_CHANNEL}")

# ID продавцов, чьи листинги игнорируем (не постим вообще, но помечаем как "виденные")
SELLER_BLACKLIST = {int(x) for x in (config.get("seller_blacklist") or [])}

# Слать только листинги продавцов с бесплатными сообщениями (send_paid_messages_stars пусто/0)
ONLY_FREE_MESSAGES = bool(config.get("notify_only_free_messages", True))

# Не слать листинги продавцов с рейтинговым уровнем профиля выше этого значения
MAX_SELLER_LEVEL = int(config.get("notify_max_seller_level", 5))


# ---------------------------------------------------------------------------
# ⚠️ Честная заметка про "стили" и premium-эмодзи в кнопках:
# У Telegram Bot API НЕТ цветных кнопок (success/primary/...) и НЕТ поддержки
# кастомных premium-эмодзи (tg-emoji) внутри текста кнопки — это ограничение
# самой платформы, обойти нельзя. STYLE_EMOJI ниже — это просто префикс из
# обычного unicode-эмодзи, визуальная имитация стиля, не настоящий цвет.
# Premium-эмодзи по ID (tg-emoji) работают только в ТЕКСТЕ сообщений
# (parse_mode="HTML") — там они используются по максимуму, см. emoji().
# ---------------------------------------------------------------------------

STYLE_EMOJI = {"success": "✅", "danger": "❌", "primary": "🔵", "secondary": "⚪️"}


def styled_button(text: str, style: str = "primary", callback_data: str = None, url: str = None) -> InlineKeyboardButton:
    prefix = STYLE_EMOJI.get(style, "")
    label = f"{prefix} {text}".strip()
    return InlineKeyboardButton(text=label, callback_data=callback_data, url=url)


def emoji(custom_id: str, fallback: str) -> str:
    """tg-emoji: premium-эмодзи по ID с обычным эмодзи как fallback. Только для ТЕКСТА сообщений."""
    return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'


def html_escape(s) -> str:
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Гейт на подписку (/start, проверка подписки)
# ---------------------------------------------------------------------------

GATE_TEXT = (
    f"{emoji('6028346797368283073', '🔔')} Подпишись на канал для использования бота.\n\n"
    f"{emoji('6028205772117118673', 'ℹ️')} Этот парсер был разработан исключительно бесплатно для нашей тимы.\n\n"
    f"{emoji('6039486778597970865', '⏰')} В будущем он станет платным"
)

ACCESS_TEXT = f"{emoji('5467512909909214089', '✅')} Актуальный парсер: https://t.me/+QYrEzNh9ejsyMGRh"


def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_button("Подписаться", style="success", url=SUB_URL)],
        [styled_button("Проверить подписку", style="primary", callback_data="check_sub")],
    ])


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(f"@{SUB_CHANNEL}", user_id)
        return member.status not in ("left", "kicked")
    except TelegramBadRequest as e:
        log.error(f"Не удалось проверить подписку (бот админ в @{SUB_CHANNEL}?): {e}")
        return False


@dp.message(CommandStart())
async def on_start(message: Message):
    if await is_subscribed(message.from_user.id):
        await message.answer(ACCESS_TEXT, disable_web_page_preview=True)
    else:
        await message.answer(GATE_TEXT, reply_markup=gate_keyboard())


@dp.callback_query(F.data == "check_sub")
async def on_check_sub(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(ACCESS_TEXT, disable_web_page_preview=True)
        await callback.answer()
    else:
        await callback.answer("Ты ещё не подписан(а) на канал.", show_alert=True)


# ---------------------------------------------------------------------------
# Claim-кнопка ("Взять лог"): удаляет пост в чате, шлёт детали забравшему в ЛС
# ---------------------------------------------------------------------------

def load_claims() -> dict:
    if CLAIMS_PATH.exists():
        return json.loads(CLAIMS_PATH.read_text(encoding="utf-8"))
    return {}


def save_claims(claims: dict) -> None:
    CLAIMS_PATH.write_text(json.dumps(claims, ensure_ascii=False, indent=2), encoding="utf-8")


def claim_keyboard(claim_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        styled_button("Взять лог", style="success", callback_data=f"claim:{claim_id}")
    ]])


@dp.callback_query(F.data.startswith("claim:"))
async def on_claim(callback: CallbackQuery):
    claim_id = callback.data.split(":", 1)[1]
    claims = load_claims()
    entry = claims.get(claim_id)

    if not entry:
        await callback.answer("Этот лог уже забрали или он устарел.", show_alert=True)
        return

    try:
        await bot.delete_message(entry["chat_id"], entry["message_id"])
    except TelegramBadRequest as e:
        log.warning(f"Не смог удалить сообщение: {e}")

    try:
        await bot.send_message(callback.from_user.id, entry["text"], disable_web_page_preview=True)
        await callback.answer("Забрал(а) ✅, детали — в ЛС")
    except TelegramBadRequest:
        await callback.answer(
            "Не могу написать тебе в ЛС — сначала нажми /start в личке с ботом.",
            show_alert=True,
        )
        return

    del claims[claim_id]
    save_claims(claims)


# ---------------------------------------------------------------------------
# Очередь отправки листингов.
#
# У Telegram Bot API жёсткий лимит — не больше ~20 сообщений в минуту В ОДНУ
# ГРУППУ (топики внутри неё это не обходят, лимит общий на чат). Это и есть
# реальный потолок скорости — не 30/сек (тот лимит на разные чаты) и не то,
# что можно "просто" разогнать notify_interval. Токен-бакет: держим запас
# токенов (burst) на случай, если очередь скопилась, но в среднем не выше
# notify_group_limit_per_min — иначе будем упираться в 429 и по факту слать
# медленнее, а не быстрее.
#
# При 429 ждём ровно retry_after и повторяем то же сообщение (это не потеря).
# Если листинг простоял в очереди дольше notify_max_age_seconds — он уже не
# "новый", и мы его выкидываем вместо того чтобы кидать в чат протухший лог.
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate_per_min: float, burst: float | None = None):
        self.rate = rate_per_min / 60.0  # токенов в секунду
        self.capacity = burst if burst is not None else rate_per_min
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class NotifyQueue:
    def __init__(self, config: dict):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.max_age = float(config.get("notify_max_age_seconds", 20))
        group_limit = float(config.get("notify_group_limit_per_min", 18))  # запас от жёстких 20/мин
        self.bucket = TokenBucket(group_limit)

    async def put(self, claim_id: str, text: str, topic_id: int) -> None:
        await self.queue.put((claim_id, text, topic_id, time.time()))

    async def run(self) -> None:
        max_retries = int(self.config.get("notify_max_retries", 5))
        while True:
            claim_id, text, topic_id, queued_at = await self.queue.get()

            age = time.time() - queued_at
            if self.max_age > 0 and age > self.max_age:
                log.info(f"Пропускаю устаревший листинг ({claim_id}), просидел в очереди {age:.0f}с")
                self.queue.task_done()
                continue

            await self.bucket.acquire()

            attempts = 0
            while True:
                try:
                    msg = await bot.send_message(
                        chat_id=self.config["target_chat"],
                        text=text,
                        message_thread_id=topic_id,
                        disable_web_page_preview=True,
                        reply_markup=claim_keyboard(claim_id),
                    )
                except TelegramRetryAfter as e:
                    # Реальный флуд-лимит — ждём ровно столько, сколько сказал Telegram,
                    # это не считается за "плохую" попытку и не блокирует остальные листинги надолго.
                    log.warning(f"429, жду {e.retry_after}s ({claim_id})")
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except Exception as e:
                    attempts += 1
                    if attempts >= max_retries:
                        # Раньше здесь был бесконечный retry: если ОДИН листинг не мог
                        # уйти (битый topic_id, временный бан и т.п.), очередь зависала
                        # на нём навсегда — тот же номер долбился в лог каждые 5с, а все
                        # более новые листинги копились за ним и не отправлялись вообще.
                        # Теперь после max_retries попыток листинг помечается неотправленным
                        # и очередь идёт дальше, к более новым листингам.
                        log.error(f"Не смог отправить {claim_id} после {attempts} попыток, пропускаю: {e}")
                        break
                    log.warning(f"Ошибка отправки ({claim_id}), попытка {attempts}/{max_retries}, повтор через 5с: {e}")
                    await asyncio.sleep(5)
                    continue
                else:
                    claims = load_claims()
                    claims[claim_id] = {"chat_id": msg.chat.id, "message_id": msg.message_id, "text": text}
                    save_claims(claims)
                    log.info(f"Отправлено: {claim_id}")

                break

            self.queue.task_done()


# ---------------------------------------------------------------------------
# Маршрутизация по темам в зависимости от цены
# ---------------------------------------------------------------------------

def pick_topics(config: dict, stars, ton) -> list:
    """
    topics.all  — всё без фильтра (если задано)
    topics.high — >= 10000 звёзд ИЛИ >= 100 TON
    topics.low  — <= 1500 звёзд ИЛИ <= 15 TON
    topics.mid  — всё остальное
    """
    topics = config.get("topics", {})
    dest = []
    if topics.get("all"):
        dest.append(topics["all"])

    if (stars is not None and stars >= 10000) or (ton is not None and ton >= 100):
        tier = topics.get("high")
    elif (stars is not None and stars <= 1500) or (ton is not None and ton <= 15):
        tier = topics.get("low")
    else:
        tier = topics.get("mid")

    if tier:
        dest.append(tier)
    return dest


# ---------------------------------------------------------------------------
# Конфиг / состояние трекера
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "gift_types_cache": None, "gift_types_ts": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Работа с Telegram API (маркет через Telethon)
# ---------------------------------------------------------------------------

async def get_resalable_gift_types(client: TelegramClient) -> list:
    result = await client(functions.payments.GetStarGiftsRequest(hash=0))
    gifts = getattr(result, "gifts", [])
    resalable = []
    for g in gifts:
        if getattr(g, "availability_resale", None):
            resalable.append({"gift_id": g.id, "title": getattr(g, "title", str(g.id))})
    return resalable


async def fetch_resale_page(client: TelegramClient, gift_id: int, limit: int = 25):
    return await client(
        functions.payments.GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=limit)
    )


def extract_listing_id(item) -> str:
    slug = getattr(item, "slug", None)
    if slug:
        return slug
    return str(getattr(item, "id", getattr(item, "num", "unknown")))


def get_owner_id(item):
    owner_peer = getattr(item, "owner_id", None)
    return getattr(owner_peer, "user_id", None) if owner_peer else None


def get_owner_username(owner) -> str | None:
    """
    Возвращает username продавца.

    У пользователя с несколькими username (в т.ч. коллекционным/NFT-юзернеймом,
    купленным на аукционе Fragment) поле `.username` в Telethon часто пустое —
    актуальный список лежит в `.usernames` (список объектов с `.username`
    и `.active`). Раньше бралось только старое поле `.username`, поэтому у
    таких продавцов бот писал "неизвестен"/"продавец не найден".
    """
    if not owner:
        return None

    usernames = getattr(owner, "usernames", None) or []
    if usernames:
        active = next((u.username for u in usernames if getattr(u, "active", True) and getattr(u, "username", None)), None)
        if active:
            return active
        first = next((u.username for u in usernames if getattr(u, "username", None)), None)
        if first:
            return first

    return getattr(owner, "username", None)


def format_price(item):
    """Возвращает (stars, ton) как числа или (None, None)."""
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


_stars_rating_cache: dict[int, tuple[int | None, float]] = {}
STARS_RATING_CACHE_TTL = 1800  # 30 минут — рейтинг не меняется поминутно, кэш бережёт лимиты аккаунта


async def get_seller_level(client: TelegramClient, user_id: int | None) -> int | None:
    """
    Реальный рейтинг профиля Telegram (level из userFull.stars_rating — тот же,
    что виден по тапу на корону в профиле). Может быть отрицательным. Это НЕ
    часть обычного User из листинга, нужен отдельный запрос users.GetFullUser.
    Кэшируем на STARS_RATING_CACHE_TTL, чтобы не долбить лимиты аккаунта одним
    и тем же продавцом на каждый его листинг. Возвращает None, если узнать не
    получилось (нет данных / ошибка) — в таком случае фильтр по уровню его не
    трогает (лучше пропустить листинг, чем молча ронять всё при сбое API).
    """
    if not user_id:
        return None

    cached = _stars_rating_cache.get(user_id)
    if cached and time.time() - cached[1] < STARS_RATING_CACHE_TTL:
        return cached[0]

    level = None
    try:
        full = await client(functions.users.GetFullUserRequest(user_id))
        rating = getattr(getattr(full, "full_user", None), "stars_rating", None)
        if rating is not None:
            level = getattr(rating, "level", None)
    except FloodWaitError as e:
        log.warning(f"FloodWait при получении рейтинга {user_id}: {e.seconds}с")
    except Exception as e:
        log.warning(f"Не смог получить рейтинг продавца {user_id}: {e}")

    _stars_rating_cache[user_id] = (level, time.time())
    return level


def format_level_line(level: int | None) -> str:
    if level is None:
        return "—"
    if level < 0:
        return "Отрицательный"
    return str(level)


async def format_message(gift_title: str, item, users_by_id: dict, stars, ton, seller_level: int | None) -> str:
    slug = extract_listing_id(item)
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

    owner_user_id = get_owner_id(item)
    owner = users_by_id.get(owner_user_id) if owner_user_id else None

    username = get_owner_username(owner)
    seller_line = f"@{html_escape(username)}" if username else "неизвестен"
    seller_line += f" (<code>{owner_user_id or '?'}</code>)"

    level = format_level_line(seller_level)

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
# Основной цикл трекера (Telethon)
# ---------------------------------------------------------------------------

async def tracker_loop(client: TelegramClient, config: dict, state: dict, notify_queue: NotifyQueue):
    poll_interval = float(config.get("poll_interval", 4))
    request_delay = float(config.get("request_delay", 1.5))
    gift_filter = set(config.get("gift_ids") or [])
    refresh_types_every = int(config.get("refresh_gift_types_seconds", 600))

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
                seen_list = list(state["seen"].get(seen_key, []))
                seen_ids = set(seen_list)
                is_first_run_for_type = seen_key not in state["seen"]

                new_ids = []
                for item in items:
                    listing_id = extract_listing_id(item)
                    if listing_id not in seen_ids:
                        new_ids.append(listing_id)
                        seen_ids.add(listing_id)
                        owner_id = get_owner_id(item)
                        if owner_id in SELLER_BLACKLIST:
                            log.info(f"Продавец {owner_id} в чёрном списке, пропускаю {listing_id}")
                            continue
                        owner = users_by_id.get(owner_id) if owner_id else None
                        paid_stars = getattr(owner, "send_paid_messages_stars", None) if owner else None
                        if ONLY_FREE_MESSAGES and paid_stars:
                            log.info(f"У продавца {owner_id} платные сообщения ({paid_stars}⭐), пропускаю {listing_id}")
                            continue
                        seller_level = await get_seller_level(client, owner_id)
                        if seller_level is not None and seller_level > MAX_SELLER_LEVEL:
                            log.info(f"У продавца {owner_id} уровень {seller_level} > {MAX_SELLER_LEVEL}, пропускаю {listing_id}")
                            continue
                        if not is_first_run_for_type:
                            stars, ton = format_price(item)
                            text = await format_message(title, item, users_by_id, stars, ton, seller_level)
                            for topic_id in pick_topics(config, stars, ton):
                                claim_id = f"{listing_id}:{topic_id}"
                                await notify_queue.put(claim_id, text, topic_id)
                            log.info(f"В очередь: {title} / {listing_id}")

                if new_ids:
                    seen_list.extend(new_ids)
                    if len(seen_list) > SEEN_HISTORY_LIMIT:
                        seen_list = seen_list[-SEEN_HISTORY_LIMIT:]
                    state["seen"][seen_key] = seen_list
                    save_state(state)

                await asyncio.sleep(request_delay)

        except Exception as e:
            log.exception(f"Ошибка в основном цикле: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Точка входа: Telethon-сессия + aiogram-бот в одном asyncio-цикле
# ---------------------------------------------------------------------------

async def main():
    state = load_state()

    session = StringSession(config.get("session_string") or "")
    client = TelegramClient(session, config["api_id"], config["api_hash"])
    await client.start(phone=config.get("phone") or None)

    if not config.get("session_string"):
        log.info("Сохраните эту session_string в config.json, чтобы не логиниться заново:")
        print(client.session.save())

    notify_queue = NotifyQueue(config)

    log.info("Запуск: трекер маркета + бот (полинг) в одном процессе")
    await asyncio.gather(
        notify_queue.run(),
        dp.start_polling(bot),
        tracker_loop(client, config, state, notify_queue),
    )


if __name__ == "__main__":
    asyncio.run(main())
