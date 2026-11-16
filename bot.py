import asyncio
import csv
import io
import logging
import os
import json
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import fair
import sys
from decimal import ROUND_UP, Decimal

import aiohttp
import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ───────────────────────── НАСТРОЙКИ (через переменные окружения) ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GIFT_DEPOSIT_TO = os.getenv("GIFT_DEPOSIT_TO", "@oskeg").strip()  # @username аккаунта, куда игроки шлют NFT-подарки
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
PORT = int(os.getenv("PORT", "10000"))
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Oskeg Case")
MIN_DEPOSIT = int(os.getenv("MIN_DEPOSIT", "10"))
MAX_DEPOSIT = int(os.getenv("MAX_DEPOSIT", "10000"))
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "50"))
MIN_DEPOSIT_TO_WITHDRAW = int(os.getenv("MIN_DEPOSIT_TO_WITHDRAW", "0"))
REF_PERCENT = int(os.getenv("REF_PERCENT", "3"))
MINES_RTP = float(os.getenv("MINES_RTP", "0.93"))
UP_RTP = float(os.getenv("UP_RTP", "0.92"))
MAX_PAYOUT = int(os.getenv("MAX_PAYOUT", "0"))           # лимит выплаты в Ракете/Курочке, 0 = без лимита
MINES_MIN_BET = int(os.getenv("MINES_MIN_BET", "1"))       # сапёр: минимальная ставка (максимума нет — ограничивает только баланс)
MINES_MAX_PAYOUT = int(os.getenv("MINES_MAX_PAYOUT", "0")) # сапёр: лимит выплаты, 0 = без лимита
LIVE_DELAY = int(os.getenv("LIVE_DELAY", "30"))               # запасная задержка: дроп попадёт в Live-ленту сам, если игрок не нажал «Забрать» (обычно лента обновляется сразу после «Забрать»)
RARE_VALUE = int(os.getenv("RARE_VALUE", "900"))           # с какой стоимости дроп считается «редким»

# Крипто-пополнение через Crypto Pay (@CryptoBot): https://help.crypt.bot/crypto-pay-api
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
CRYPTO_API = os.getenv("CRYPTO_PAY_API", "https://pay.crypt.bot/api").rstrip("/")   # тест: https://testnet-pay.crypt.bot/api
CRYPTO_ASSET = os.getenv("CRYPTO_ASSET", "USDT")
CRYPTO_STARS_PER_USD = float(os.getenv("CRYPTO_STARS_PER_USD", "50"))   # сколько ⭐ начисляется за 1 USDT (50 => 100 ⭐ = 2 USDT)
CRYPTO_MIN_DEPOSIT = int(os.getenv("CRYPTO_MIN_DEPOSIT", "10"))
CRYPTO_MAX_DEPOSIT = int(os.getenv("CRYPTO_MAX_DEPOSIT", "1000000"))

BOT_USERNAME = "Oskegcase"

# ───────────────────────── КАТАЛОГ ПОДАРКОВ И КЕЙСОВ ─────────────────────────
# value — стоимость в звёздах (⭐), примерная рыночная цена. Предмет можно продать за эту сумму.
# Цены с «+» на скриншотах маркета; у позиций, помеченных (≈), цена на скрине не влезла — оценка.
ITEMS = {
    "bear": {"emoji": "🧸", "name": "Мишка", "value": 15},
    "heart": {"emoji": "💝", "name": "Сердце", "value": 15},
    "rose": {"emoji": "🌹", "name": "Роза", "value": 25},
    "gift": {"emoji": "🎁", "name": "Подарок", "value": 25},
    "champ": {"emoji": "🍾", "name": "Шампанское", "value": 50},
    "cake": {"emoji": "🍰", "name": "Торт", "value": 50},
    "rocket": {"emoji": "🚀", "name": "Ракета", "value": 50},
    "trophy": {"emoji": "🏆", "name": "Кубок", "value": 100},
    "ring": {"emoji": "💍", "name": "Кольцо", "value": 100},
    "diamond": {"emoji": "💎", "name": "Бриллиант", "value": 100},
    "icecream": {"emoji": "🍦", "name": "Мороженое", "value": 300},
    "snake25": {"emoji": "🐍", "name": "Змея 2025", "value": 390},
    "santasnake": {"emoji": "🎅", "name": "Змея в шапке", "value": 400},
    "cupcake8": {"emoji": "🧁", "name": "Кекс 8 Марта", "value": 400},
    "stocking": {"emoji": "🎄", "name": "Новогодний носок", "value": 400},
    "torch": {"emoji": "🔥", "name": "Факел Свободы", "value": 450},
    "gift25": {"emoji": "📦", "name": "Подарок 2025", "value": 450},
    "cane": {"emoji": "🍬", "name": "Леденцовая трость", "value": 480},
    "flamingo": {"emoji": "🦩", "name": "Фламинго-круг", "value": 489},
    "lantern": {"emoji": "🏮", "name": "Фонарь", "value": 490},
    "lollipop": {"emoji": "🍭", "name": "Леденец", "value": 490},
    "crescent": {"emoji": "🕌", "name": "Полумесяц", "value": 490},
    "noodles": {"emoji": "🍜", "name": "Лапша", "value": 498},
    "popsicle": {"emoji": "🍫", "name": "Эскимо", "value": 499},
    "cherrycake": {"emoji": "🍒", "name": "Вишнёвый торт", "value": 500},
    "poop": {"emoji": "💩", "name": "Какашка", "value": 503},
    "backpack": {"emoji": "🎒", "name": "Рюкзак", "value": 506},
    "baggie": {"emoji": "🛍️", "name": "Злой пакетик", "value": 506},
    "clover": {"emoji": "🍀", "name": "Клевер", "value": 506},
    "socks": {"emoji": "🧦", "name": "Носки", "value": 506},
    "bdaysign": {"emoji": "🎉", "name": "Happy B-Day", "value": 506},
    "bdaycake": {"emoji": "🎂", "name": "Праздничный торт", "value": 506},
    "bdaycal": {"emoji": "📅", "name": "B-Day календарь", "value": 506},
    "liberty": {"emoji": "🗽", "name": "Статуя Свободы", "value": 506},
    "dog": {"emoji": "🐶", "name": "Пёс-рэпер", "value": 506},
    "saber": {"emoji": "🗡️", "name": "Световой меч", "value": 528},
    "mask": {"emoji": "🎭", "name": "Хоккейная маска", "value": 903},
    "candle": {"emoji": "🕯️", "name": "Свеча-сердце", "value": 976},
    "oscar": {"emoji": "🏆", "name": "Статуэтка Оскар", "value": 5786},
    "nailband": {"emoji": "📿", "name": "Браслет-гвоздь", "value": 10639},
    "orangebag": {"emoji": "👜", "name": "Оранжевая сумка", "value": 11355},
    "brick": {"emoji": "🧱", "name": "Кирпич", "value": 7230},
    "genie": {"emoji": "🪔", "name": "Лампа Джинна", "value": 3225},
    "poison": {"emoji": "🧪", "name": "Зелье яда", "value": 5349},
    "shades": {"emoji": "🕶️", "name": "Тёмные очки", "value": 12448},
    "bicep": {"emoji": "💪", "name": "Золотой бицепс", "value": 9409},
    "spartan": {"emoji": "⛑️", "name": "Шлем спартанца", "value": 15850},
    "plum": {"emoji": "🍑", "name": "Драгоценная слива", "value": 21712},
    "durovcap": {"emoji": "🧢", "name": "Кепка Дурова", "value": 36179},
    "plushpepe": {"emoji": "🐸", "name": "Плюшевый Пепе", "value": 600624},
    "skull": {"emoji": "💀", "name": "Череп", "value": 2436},
    "pacifier": {"emoji": "🍼", "name": "Золотая пустышка", "value": 2331},
    "kissfrog": {"emoji": "🐸", "name": "Лягушка с поцелуем", "value": 3987},
    "vamplips": {"emoji": "👄", "name": "Губы вампира", "value": 3843},
    "band": {"emoji": "💍", "name": "Обручальное кольцо", "value": 3617},
    "sapphire": {"emoji": "💍", "name": "Кольцо с сапфиром", "value": 5427},
    "icecrystal": {"emoji": "🧊", "name": "Ледяные кристаллы", "value": 6047},
    "perfume": {"emoji": "🧴", "name": "Духи N°5", "value": 7230},
    "goldhand": {"emoji": "🖖", "name": "Золотая рука", "value": 9984},
    "rainbowcrystal": {"emoji": "💎", "name": "Радужный кристалл", "value": 10704},
    "blackcat": {"emoji": "🐈‍⬛", "name": "Чёрный кот", "value": 23799},
    "hoodbear": {"emoji": "🧸", "name": "Мишка в капюшоне", "value": 43415},
    "pumpkin": {"emoji": "🎃", "name": "Злая тыква", "value": 1113},
    "cigar": {"emoji": "🚬", "name": "Сигара", "value": 3185},
    "vinyl": {"emoji": "📻", "name": "Проигрыватель", "value": 1085},
    "broom": {"emoji": "🧹", "name": "Метла", "value": 1057},
    "skullflower": {"emoji": "🌸", "name": "Цветок-череп", "value": 1043},
    "voodoo": {"emoji": "🪆", "name": "Кукла вуду", "value": 3185},
    "jinglebell": {"emoji": "🔔", "name": "Бубенчик", "value": 832},
    "eagle": {"emoji": "🦅", "name": "Хрустальный орёл", "value": 2403},
    "goldwatch": {"emoji": "⌚", "name": "Золотые часы", "value": 4849},
    "signet": {"emoji": "💍", "name": "Перстень", "value": 2895},
    "lowrider": {"emoji": "🚗", "name": "Лоурайдер", "value": 5783},
    "nekohelm": {"emoji": "🏍️", "name": "Шлем с ушками", "value": 3329},
    "tophat": {"emoji": "🎩", "name": "Цилиндр", "value": 863},
    "mittens": {"emoji": "🧤", "name": "Варежки", "value": 724},
    "starplush": {"emoji": "⭐", "name": "Звёздочка", "value": 845},
    "lockheart": {"emoji": "💛", "name": "Сердце с ключом", "value": 1930},
    "skullorb": {"emoji": "🔮", "name": "Шар с черепом", "value": 1093},
    "xmasbells": {"emoji": "🔔", "name": "Рождественские колокольчики", "value": 851},
    "ringbox": {"emoji": "💍", "name": "Кольцо в коробочке", "value": 2895},
    "lovepotion": {"emoji": "🧪", "name": "Любовное зелье", "value": 1338},
    "rosedome": {"emoji": "🌹", "name": "Роза под стеклом", "value": 2387},
    "pinkbear": {"emoji": "🧸", "name": "Розовый мишка", "value": 3045},
    "hairdryer": {"emoji": "💇", "name": "Фен", "value": 1409},
    "webheart": {"emoji": "💜", "name": "Сердце в паутине", "value": 1447},
    "cocoa": {"emoji": "☕", "name": "Какао", "value": 450},
    "potgold": {"emoji": "🍀", "name": "Горшок золота", "value": 507},
    "sakura": {"emoji": "🌸", "name": "Сакура", "value": 1003},
    "jackbox": {"emoji": "🤡", "name": "Джек-в-коробке", "value": 499},
    "bowtie": {"emoji": "🎀", "name": "Бабочка", "value": 507},
    "wreath": {"emoji": "🎄", "name": "Венок", "value": 470},
    "bunnycake": {"emoji": "🧁", "name": "Кекс-кролик", "value": 739},
    "cauldron": {"emoji": "🧙", "name": "Котёл", "value": 507},
    "chocberry": {"emoji": "🍓", "name": "Клубника в шоколаде", "value": 794},
    "witchhat": {"emoji": "🧙", "name": "Шляпа ведьмы", "value": 507},
    "snowglobe": {"emoji": "🔮", "name": "Снежный шар", "value": 615},
    "candle2": {"emoji": "🕯️", "name": "Свеча", "value": 556},
    "lovecats": {"emoji": "😻", "name": "Влюблённые котики", "value": 1194},
    "ufcbox": {"emoji": "🥊", "name": "UFC бокс", "value": 1375},
    "eyeball": {"emoji": "👁️", "name": "Глаз", "value": 687},
    "moonneck": {"emoji": "🌙", "name": "Подвеска-месяц", "value": 542},
    "ginger": {"emoji": "🍪", "name": "Пряничный человечек", "value": 507},
    "loveshiba": {"emoji": "🐕", "name": "Сиба Love You", "value": 667},
    "mochi": {"emoji": "🍡", "name": "Моти", "value": 687},
    "fountpen": {"emoji": "🖋️", "name": "Перьевая ручка", "value": 717},
    "mulled": {"emoji": "🍷", "name": "Глинтвейн", "value": 507},
    "easteregg": {"emoji": "🥚", "name": "Пасхальное яйцо", "value": 489},
    "lolli2": {"emoji": "🍭", "name": "Леденец-спираль", "value": 507},
    "notebook": {"emoji": "📓", "name": "Блокнот", "value": 507},
    "jester": {"emoji": "🃏", "name": "Шутовской колпак", "value": 507},
    "bunnybasket": {"emoji": "🐰", "name": "Корзина с кроликом", "value": 507},
    "gingerheart": {"emoji": "❤️", "name": "Пряник-сердце", "value": 507},
    "rollup": {"emoji": "🚬", "name": "Самокрутка", "value": 1363},
    "medal": {"emoji": "🥇", "name": "Медаль", "value": 506},
    "moneybouquet": {"emoji": "💐", "name": "Денежный букет", "value": 507},
    "heartkey": {"emoji": "⌨️", "name": "Клавиша-сердце", "value": 521},
    "redbook": {"emoji": "📕", "name": "Книга", "value": 507},
    "monkey": {"emoji": "🐵", "name": "Обезьянка", "value": 643},
    "sparkler": {"emoji": "🎇", "name": "Бенгальский огонь", "value": 507},
    "rocket2": {"emoji": "🚀", "name": "Космическая ракета", "value": 507},
    "heartjar": {"emoji": "🫙", "name": "Банка сердец", "value": 507},
    "brooch": {"emoji": "💖", "name": "Магическая брошь", "value": 119415},
    "bluecup": {"emoji": "🏆", "name": "Ледяной кубок", "value": 120646},
    "mosaiccup": {"emoji": "🏆", "name": "Мозаичный кубок", "value": 112106},
}


# Старые NFT-предметы удалены из игры. Если они остались у игроков — при старте бота
# автоматически превращаются в звёзды по старой цене (см. migrate_legacy).
LEGACY_NFT = {"cap": 1500, "pepe": 5000, "cat": 300, "dragon": 800, "ufo": 1200,
              "crown": 2500, "unicorn": 3500, "ape": 6000, "lambo": 10000}

CASE_RTP = float(os.getenv("CASE_RTP", "0.90"))   # средний возврат кейса (0.90 = 10% маржа)


def _weights(values, target):
    """Подбирает веса w ~ v^-a так, чтобы мат. ожидание дропа = target (цена * RTP)."""
    def ev(a):
        w = [v ** -a for v in values]
        return sum(x * v for x, v in zip(w, values)) / sum(w)
    lo, hi = -3.0, 8.0
    for _ in range(90):
        mid = (lo + hi) / 2
        if ev(mid) > target:
            lo = mid
        else:
            hi = mid
    a = (lo + hi) / 2
    w = [v ** -a for v in values]
    s = sum(w)
    return [max(1, round(x / s * 1_000_000)) for x in w]


def _mk(name, price, icon, keys, rtp=None):
    """rtp — отдача именно этого кейса (0.80 = 80%). Если не задана — берётся CASE_RTP."""
    vals = [ITEMS[k]["value"] for k in keys]
    target = price * (rtp if rtp is not None else CASE_RTP)
    assert min(vals) < target < max(vals), (name, min(vals), target, max(vals))
    return {"name": name, "price": price, "icon": icon,
            "drops": [(f"i:{k}", w) for k, w in zip(keys, _weights(vals, target))]}


# В кейсах ТОЛЬКО подарки (обычные и NFT-маркет). Звёзды в дропе не выпадают.
CASES = {
    "k25": _mk("🪙 Мини", 25, "bear", ["bear", "heart", "rose", "gift", "champ", "cake", "rocket", "trophy", "ring", "diamond"]),
    "k50": _mk("🧸 Малыш", 50, "heart", ["heart", "rose", "gift", "champ", "cake", "rocket", "trophy", "ring", "diamond", "icecream"]),
    "k100": _mk("📦 Стартовый", 100, "gift25", ["gift", "champ", "cake", "rocket", "trophy", "ring", "diamond", "icecream", "snake25", "cupcake8", "stocking", "torch"]),
    "k250": _mk("🎁 Золотой", 250, "icecream", ["champ", "rocket", "trophy", "ring", "diamond", "icecream", "snake25", "cupcake8", "stocking", "torch", "gift25", "cane", "flamingo", "lantern", "noodles"]),
    "k500": _mk("🎉 Праздничный", 500, "bdaycake", ["icecream", "snake25", "cocoa", "wreath", "jackbox", "easteregg", "lantern", "lollipop", "popsicle", "poop", "backpack", "baggie",
                                                "clover", "socks", "bdaycake", "bdaycal", "liberty", "dog", "saber", "moonneck", "candle2", "snowglobe", "bunnycake"]),
    "k700": _mk("❄️ Новогодний", 700, "snowglobe", ["stocking", "gift25", "cane", "cocoa", "wreath", "snowglobe", "mittens", "xmasbells", "jinglebell", "tophat", "starplush", "santasnake"]),
    "k1000": _mk("🎃 Хэллоуин", 1000, "pumpkin", ["cauldron", "witchhat", "eyeball", "broom", "skullorb", "skullflower", "pumpkin", "skull", "voodoo", "mask", "candle"]),
    "k1500": _mk("💘 Любовь", 1500, "lockheart", ["heartkey", "ginger", "loveshiba", "mochi", "bunnycake", "chocberry", "lovecats", "lovepotion", "hairdryer", "webheart", "lockheart", "rosedome", "ringbox", "pinkbear"]),
    "k2500": _mk("💎 Алмазный", 2500, "genie", ["mask", "candle", "pumpkin", "lovecats", "ufcbox", "rollup", "hairdryer", "webheart", "lockheart", "pacifier", "rosedome", "eagle", "skull", "signet",
                                             "pinkbear", "cigar", "voodoo", "genie", "band", "vamplips", "kissfrog"]),
    "k5000": _mk("👑 Премиум", 5000, "goldwatch", ["signet", "pinkbear", "cigar", "genie", "nekohelm", "band", "vamplips", "kissfrog", "goldwatch", "poison", "sapphire", "lowrider",
                                                 "oscar", "icecrystal", "brick", "perfume", "bicep", "goldhand"]),
    "k10000": _mk("🔥 Огненный", 10000, "spartan", ["brick", "perfume", "bicep", "goldhand", "rainbowcrystal", "nailband", "orangebag", "shades", "spartan", "plum", "blackcat"]),
    "k25000": _mk("🌌 Легенда", 25000, "durovcap", ["bicep", "goldhand", "nailband", "rainbowcrystal", "orangebag", "shades", "spartan", "plum", "blackcat", "durovcap", "hoodbear"]),
    "k50000": _mk("🏆 Миллионер", 50000, "mosaiccup", ["orangebag", "shades", "spartan", "plum", "blackcat", "durovcap", "hoodbear", "mosaiccup", "brooch", "bluecup"]),
    "k100000": _mk("🚀 Космос", 100000, "plushpepe", ["shades", "spartan", "plum", "blackcat", "durovcap", "hoodbear", "mosaiccup", "brooch", "bluecup", "plushpepe"]),
    # ── Новые подарочные кейсы: отдача 80% и 85% ──
    "k150": _mk("🎀 Сюрприз", 150, "gift", ["heart", "rose", "gift", "champ", "cake", "rocket", "trophy", "ring", "diamond", "icecream"], rtp=0.80),
    "k400": _mk("💝 Подарок мечты", 400, "bdaycake", ["champ", "trophy", "diamond", "icecream", "snake25", "cupcake8", "stocking", "gift25", "cane", "lantern", "flamingo"], rtp=0.85),
    "k1200": _mk("🌸 Сакура", 1200, "sakura", ["candle2", "snowglobe", "mochi", "mask", "candle", "sakura", "skullflower", "broom", "vinyl", "pumpkin", "lovecats", "lovepotion", "rollup", "hairdryer"], rtp=0.80),
}

BET_PRESETS = [10, 50, 100, 500, 1000]
SIDE = 4
CELLS = SIDE * SIDE
MINES_OPTIONS = [2, 3, 5, 8]
UP_MULTS = {"1.5": 1.5, "2": 2.0, "3": 3.0, "5": 5.0, "10": 10.0, "20": 20.0}
DEPOSIT_PRESETS = [10, 50, 100, 250, 500, 1000]


def drop_value(key: str) -> int:
    if key.startswith("s:"):
        return int(key[2:])
    return ITEMS[key[2:]]["value"]


def drop_label(key: str) -> str:
    if key.startswith("s:"):
        return f"⭐ {int(key[2:])} звёзд"
    it = ITEMS[key[2:]]
    return f"{it['emoji']} {it['name']} ({it['value']} ⭐)"


def case_rtp(case: dict) -> float:
    total = sum(w for _, w in case["drops"])
    ev = sum(drop_value(k) * w for k, w in case["drops"]) / total
    return ev / case["price"] * 100


def roll(drops, rng=secrets):
    total = sum(w for _, w in drops)
    r = rng.randbelow(total)
    acc = 0
    for key, w in drops:
        acc += w
        if r < acc:
            return key
    return drops[-1][0]


def mines_mult(m: int, k: int) -> float:
    if k <= 0:
        return 1.0
    p = 1.0
    for i in range(k):
        p *= (CELLS - i) / (CELLS - m - i)
    return p * MINES_RTP


def mines_payout(bet: int, m: int, k: int) -> int:
    pay = int(bet * mines_mult(m, k))
    return min(pay, MINES_MAX_PAYOUT) if MINES_MAX_PAYOUT > 0 else pay


def mines_capped(pay: int) -> bool:
    return MINES_MAX_PAYOUT > 0 and pay >= MINES_MAX_PAYOUT


def parse_ints(s: str):
    return [int(x) for x in s.split(",") if x != ""]


def join_ints(lst) -> str:
    return ",".join(str(x) for x in lst)


# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
pool: asyncpg.Pool = None  # type: ignore

SCHEMA = fair.DDL + ";\n" + """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY,
    username TEXT,
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    total_dep BIGINT NOT NULL DEFAULT 0,
    ref_by BIGINT,
    last_bonus TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inventory(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inventory_user_idx ON inventory(user_id);
CREATE TABLE IF NOT EXISTS withdrawals(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    item TEXT,
    amount BIGINT NOT NULL DEFAULT 0,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS gift_deposits(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    link TEXT,
    claimed BIGINT NOT NULL DEFAULT 0,
    amount BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS payments(
    charge_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mines_games(
    user_id BIGINT PRIMARY KEY,
    bet BIGINT NOT NULL,
    mines INT NOT NULL,
    mine_cells TEXT NOT NULL,
    opened TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS cases_opened INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_free_case TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gate_passed BOOLEAN NOT NULL DEFAULT true;   -- у уже существующих игроков = true
ALTER TABLE users ALTER COLUMN gate_passed SET DEFAULT false;                            -- у новых = false, пока не подпишутся
CREATE TABLE IF NOT EXISTS live_drops(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    case_id TEXT NOT NULL,
    item TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    visible_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS live_drops_vis_idx ON live_drops(visible_at DESC);
ALTER TABLE users ADD COLUMN IF NOT EXISTS rare_drops INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS prof_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS prof_emoji TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS prof_color INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS prof_title INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS task_cents INT NOT NULL DEFAULT 0;   -- накопленные за задания сотые доли звезды (50 = 0.5 ⭐)
CREATE TABLE IF NOT EXISTS hub_tasks(
    user_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'shown',   -- shown = показано, done = выполнено и оплачено
    shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, url)
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS mines_wins INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS best_mines DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS up_wins INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS best_up DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS crypto_invoices(
    invoice_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    stars BIGINT NOT NULL,
    amount TEXT NOT NULL,
    asset TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS achievements(
    user_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
"""


def _db_dsn(url: str) -> str:
    """Neon: убираем параметры, которых не понимает asyncpg, и включаем SSL, если его забыли указать."""
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("channel_binding", "options")]
    if "neon.tech" in (parts.hostname or "") and not any(k == "sslmode" for k, _ in q):
        q.append(("sslmode", "require"))
    return urlunsplit(parts._replace(query=urlencode(q)))


DB_KEEPALIVE = int(os.getenv("DB_KEEPALIVE", "240"))          # раз в сколько секунд будить базу, пока игроки активны (0 = выкл)
DB_ACTIVE_MIN = int(os.getenv("DB_ACTIVE_MIN", "30"))         # «активны» = был запрос от игрока за последние N минут
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
last_activity = {"t": 0.0}


async def init_db():
    global pool
    for attempt in range(1, 13):
        try:
            # max_inactive_connection_lifetime=0 — не закрываем простаивающие соединения (пересоздание = лишние сотни мс на TLS);
            # их держит живыми db_keepalive(). statement_cache_size=0 нужен только если у вас pgbouncer без prepared statements.
            pool = await asyncpg.create_pool(
                _db_dsn(DATABASE_URL), min_size=DB_POOL_MIN, max_size=DB_POOL_MAX, command_timeout=60, timeout=30,
                max_inactive_connection_lifetime=0,
                statement_cache_size=0 if os.getenv("DB_NO_PS") == "1" else 100,
            )
            break
        except Exception as e:
            log.warning("БД недоступна (попытка %s/12): %s", attempt, e)
            await asyncio.sleep(5)
    else:
        raise SystemExit("Не удалось подключиться к базе данных")
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
    await migrate_legacy()
    log.info("База данных готова")


async def db_keepalive():
    """Neon усыпляет базу после ~5 минут простоя, и первый запрос после этого идёт секунды.
    Пока есть активные игроки, раз в DB_KEEPALIVE секунд прогреваем все соединения пула."""
    if DB_KEEPALIVE <= 0:
        return
    while True:
        await asyncio.sleep(DB_KEEPALIVE)
        if time.time() - last_activity["t"] > DB_ACTIVE_MIN * 60:
            continue
        try:
            await asyncio.gather(*[pool.execute("SELECT 1") for _ in range(DB_POOL_MIN)])
        except Exception as e:
            log.warning("db_keepalive: %s", e)


async def migrate_legacy():
    """Старые NFT-предметы -> звёзды по прежней цене (чтобы ничего не пропало у игроков)."""
    for key, value in LEGACY_NFT.items():
        n = await pool.fetchval(
            "WITH d AS (DELETE FROM inventory WHERE item=$1 RETURNING user_id), "
            "c AS (SELECT user_id, count(*) AS n FROM d GROUP BY user_id), "
            "u AS (UPDATE users x SET balance = x.balance + c.n * $2 FROM c WHERE x.id = c.user_id RETURNING c.n) "
            "SELECT COALESCE(sum(n), 0) FROM u",
            key, value,
        )
        if n:
            log.info("Миграция: %s × %s -> %s ⭐", key, n, int(n) * value)


_touched: dict = {}


async def touch_user(u):
    """Запись пользователя. Если за последние 10 минут уже писали то же имя — базу не трогаем."""
    now = time.time()
    last_activity["t"] = now
    c = _touched.get(u.id)
    if c and c[0] == u.username and now - c[1] < 600:
        return
    await pool.execute(
        "INSERT INTO users(id, username) VALUES($1, $2) "
        "ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username "
        "WHERE users.username IS DISTINCT FROM EXCLUDED.username",
        u.id, u.username,
    )
    if len(_touched) > 20000:
        _touched.clear()
    _touched[u.id] = (u.username, now)


async def get_balance(uid: int) -> int:
    v = await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid)
    return int(v or 0)


async def spend(uid: int, amount: int) -> bool:
    ok = await pool.fetchval(
        "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
        uid, amount,
    )
    return ok is not None


async def credit_deposit(uid: int, amount: int, charge_id: str) -> bool:
    async with pool.acquire() as con:
        async with con.transaction():
            ins = await con.fetchval(
                "INSERT INTO payments(charge_id, user_id, amount) VALUES($1,$2,$3) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                charge_id, uid, amount,
            )
            if not ins:
                return False
            ref = await con.fetchval(
                "UPDATE users SET balance = balance + $2, total_dep = total_dep + $2 "
                "WHERE id=$1 RETURNING ref_by",
                uid, amount,
            )
            if ref:
                bonus = amount * REF_PERCENT // 100
                if bonus > 0:
                    await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", ref, bonus)
    return True


# ───────────────────────── КРИПТО-ПОПОЛНЕНИЕ (Crypto Pay / @CryptoBot) ─────────────────────────
class CryptoError(Exception):
    pass


async def crypto_call(method: str, params: dict | None = None, post: bool = False):
    if not CRYPTO_TOKEN:
        raise CryptoError("Крипто-оплата не настроена")
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        if post:
            cm = s.post(f"{CRYPTO_API}/{method}", json=params or {}, headers=headers)
        else:
            cm = s.get(f"{CRYPTO_API}/{method}", params=params or {}, headers=headers)
        async with cm as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raise CryptoError(f"Crypto Pay: неверный ответ (HTTP {resp.status})")
    if not data.get("ok"):
        err = data.get("error")
        raise CryptoError(f"Crypto Pay: {err.get('name') if isinstance(err, dict) else err}")
    return data["result"]


def crypto_usd(stars: int) -> Decimal:
    """Сколько USDT нужно заплатить за N звёзд (округляем вверх до цента)."""
    return (Decimal(stars) / Decimal(str(CRYPTO_STARS_PER_USD))).quantize(Decimal("0.01"), rounding=ROUND_UP)


async def crypto_create_invoice(uid: int, stars: int) -> dict:
    if not CRYPTO_MIN_DEPOSIT <= stars <= CRYPTO_MAX_DEPOSIT:
        raise CryptoError(f"Сумма от {CRYPTO_MIN_DEPOSIT} до {CRYPTO_MAX_DEPOSIT} ⭐")
    amount = crypto_usd(stars)
    inv = await crypto_call("createInvoice", {
        "asset": CRYPTO_ASSET,
        "amount": str(amount),
        "description": f"Пополнение {BOT_NAME} на {stars} ⭐",
        "payload": f"dep:{uid}:{stars}",
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    }, post=True)
    await pool.execute(
        "INSERT INTO crypto_invoices(invoice_id, user_id, stars, amount, asset) VALUES($1,$2,$3,$4,$5) "
        "ON CONFLICT DO NOTHING",
        int(inv["invoice_id"]), uid, stars, str(amount), CRYPTO_ASSET,
    )
    link = inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url") or inv.get("pay_url")
    return {"id": int(inv["invoice_id"]), "link": link, "amount": str(amount), "asset": CRYPTO_ASSET}


async def crypto_sync(bot: Bot, uid: int | None = None) -> int:
    """Проверяет неоплаченные счета в Crypto Pay и зачисляет оплаченные. Возвращает число зачислений."""
    if not CRYPTO_TOKEN:
        return 0
    if uid is None:
        rows = await pool.fetch(
            "SELECT invoice_id, user_id, stars FROM crypto_invoices "
            "WHERE status='active' AND created_at > now() - interval '3 days' ORDER BY invoice_id LIMIT 100")
    else:
        rows = await pool.fetch(
            "SELECT invoice_id, user_id, stars FROM crypto_invoices "
            "WHERE status='active' AND user_id=$1 AND created_at > now() - interval '3 days' "
            "ORDER BY invoice_id LIMIT 100", uid)
    if not rows:
        return 0
    mine = {int(x["invoice_id"]): x for x in rows}
    res = await crypto_call("getInvoices", {"invoice_ids": ",".join(map(str, mine)), "count": 100})
    items = res.get("items", []) if isinstance(res, dict) else res
    credited = 0
    for inv in items:
        row = mine.get(int(inv.get("invoice_id", 0)))
        if not row:
            continue
        st = inv.get("status")
        if st == "paid":
            done = await credit_deposit(row["user_id"], int(row["stars"]), f"crypto:{row['invoice_id']}")
            await pool.execute("UPDATE crypto_invoices SET status='paid' WHERE invoice_id=$1", row["invoice_id"])
            if done:
                credited += 1
                try:
                    await bot.send_message(
                        row["user_id"],
                        f"✅ Крипто-платёж получен: <b>+{row['stars']} ⭐</b>\n"
                        f"💰 Баланс: <b>{await get_balance(row['user_id'])} ⭐</b>")
                except Exception:
                    pass
                await notify_ach(bot, row["user_id"])
        elif st == "expired":
            await pool.execute("UPDATE crypto_invoices SET status='expired' WHERE invoice_id=$1", row["invoice_id"])
    return credited


async def crypto_poller(bot: Bot):
    while True:
        await asyncio.sleep(8)
        try:
            await crypto_sync(bot)
        except Exception as e:
            log.warning("crypto_poller: %s", e)


# ───────────────────────── ДОСТИЖЕНИЯ ─────────────────────────
ACHIEVEMENTS = {
    "first_case": ("📦 Первый кейс", "откройте 1 кейс", lambda u: u["cases_opened"] >= 1),
    "cases_50": ("🎁 Коллекционер", "откройте 50 кейсов", lambda u: u["cases_opened"] >= 50),
    "cases_500": ("📚 Кейс-мастер", "откройте 500 кейсов", lambda u: u["cases_opened"] >= 500),
    "rare": ("🎭 Редкая находка", "выбейте редкий подарок (от 900 ⭐)", lambda u: u["rare_drops"] >= 1),
    "mines_win": ("💣 Сапёр", "заберите выигрыш в сапёре", lambda u: u["mines_wins"] >= 1),
    "mines_x10": ("🔥 Хладнокровие", "множитель x10 в сапёре", lambda u: u["best_mines"] >= 10),
    "up_win": ("🚀 Первый апгрейд", "выиграйте апгрейд", lambda u: u["up_wins"] >= 1),
    "up_x10": ("⚡ Удача", "выиграйте апгрейд x10 и выше", lambda u: u["best_up"] >= 10),
    "depositor": ("💳 Первое пополнение", "пополните баланс", lambda u: u["total_dep"] > 0),
    "whale": ("🐳 Кит", "пополните на 1000 ⭐ суммарно", lambda u: u["total_dep"] >= 1000),
    "rich": ("💰 Богач", "накопите 1000 ⭐ на балансе", lambda u: u["balance"] >= 1000),
    "friend": ("👥 Друг", "пригласите 1 друга", lambda u: u["refs"] >= 1),
}


def ach_missing(u, have) -> list:
    """Ключи достижений, условия которых выполнены, но которых ещё нет в базе."""
    return [k for k, (_t, _d, cond) in ACHIEVEMENTS.items() if k not in have and cond(u)]


async def ach_grant(uid: int, keys: list) -> list:
    """Одним запросом записывает достижения, возвращает названия реально новых."""
    if not keys:
        return []
    rows = await pool.fetch(
        "INSERT INTO achievements(user_id, key) SELECT $1::bigint, k FROM unnest($2::text[]) AS k "
        "ON CONFLICT DO NOTHING RETURNING key", uid, keys)
    return [ACHIEVEMENTS[r["key"]][0] for r in rows]


async def check_achievements(uid: int):
    u = await pool.fetchrow(
        "SELECT u.*, (SELECT count(*) FROM users x WHERE x.ref_by = u.id) AS refs, "
        "ARRAY(SELECT key FROM achievements WHERE user_id = u.id) AS have FROM users u WHERE u.id=$1",
        uid,
    )
    if not u:
        return []
    return await ach_grant(uid, ach_missing(u, set(u["have"])))


async def notify_ach(bot, uid: int):
    try:
        new = await check_achievements(uid)
        if new:
            await bot.send_message(uid, "🏆 <b>Новое достижение!</b>\n" + "\n".join(new))
    except Exception:
        log.exception("notify_ach failed")


async def record_mines_win(con, uid: int, bet: int, payout: int):
    if bet > 0 and payout > bet:
        await con.execute(
            "UPDATE users SET mines_wins = mines_wins + 1, best_mines = GREATEST(best_mines, $2) WHERE id=$1",
            uid, float(payout / bet),
        )


# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ UI ─────────────────────────
class St(StatesGroup):
    dep_amount = State()
    dep_crypto = State()
    mines_bet = State()
    wd_amount = State()
    wd_details = State()


r = Router()
r.message.filter(F.chat.type == "private")


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK_ROW = [btn("🏠 Меню", "menu")]


def main_kb() -> InlineKeyboardMarkup:
    if EXTERNAL_URL:
        return kb([[InlineKeyboardButton(text="🎮 Открыть Oskeg Case", web_app=WebAppInfo(url=EXTERNAL_URL))]])
    return kb([
        [btn("🎁 Кейсы", "cases"), btn("💣 Сапёр", "mines")],
        [btn("🚀 Апгрейдер", "up"), btn("🎒 Инвентарь", "inv")],
        [btn("💳 Пополнить", "dep"), btn("💸 Вывод", "wd")],
        [btn("🎅 Бонус", "bonus"), btn("👤 Профиль", "profile")],
        [btn("🏆 Достижения", "ach")] + ([btn(f"📋 Задания +{fmt_stars(TASK_CENTS)} ⭐", "tasks")] if TASKS_ON else []),
    ])


async def menu_text(uid: int) -> str:
    bal = await get_balance(uid)
    return f"<b>{BOT_NAME}</b>\n\n💰 Баланс: <b>{bal} ⭐</b>\n\nВыберите раздел:"


async def show(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup)
    except Exception:
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup)


class UserMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is not None and not u.is_bot:
            try:
                await touch_user(u)
            except Exception:
                log.exception("touch_user failed")
        return await handler(event, data)


# ───────────────────────── ОБЯЗАТЕЛЬНЫЕ ПОДПИСКИ ─────────────────────────
# SUB_CHANNELS="@channel1,@channel2" — два канала (бот должен быть админом в каждом, иначе Telegram не отдаёт статус подписки).
# Канал без username: "-1001234567890|https://t.me/+инвайт". Пусто = проверки выключены.
def _parse_channels(raw: str) -> list:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        chat, _, link = part.partition("|")
        chat, link = chat.strip(), link.strip()
        if not link and chat.startswith("@"):
            link = "https://t.me/" + chat[1:]
        if chat.lstrip("-").isdigit():
            chat = int(chat)
        out.append({"chat": chat, "url": link, "name": chat if isinstance(chat, str) else "Канал"})
    return out


SUB_CHANNELS = _parse_channels(os.getenv("SUB_CHANNELS", ""))


BOTOHUB_TOKEN = os.getenv("BOTOHUB_TOKEN", "").strip()      # токен из https://botohub.me/integration (заголовок Auth)
BOTOHUB_URL = os.getenv("BOTOHUB_URL", "https://botohub.me/get-tasks").strip()
SUBS_ON = bool(BOTOHUB_TOKEN or SUB_CHANNELS)
_bh_session = None
BH_LAST = ""   # последняя ошибка/ответ BotoHub (для диагностики в окне заданий)


def _sponsor_name(url: str, i: int) -> str:
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return "@" + tail if tail and not tail.startswith("+") and tail.replace("_", "").isalnum() else f"Спонсор {i}"


async def botohub_fetch(uid: int):
    """Запрос к BotoHub. Возвращает (ok, completed, links): ok=False — сервис недоступен/ошибка, completed — игрок выполнил всё."""
    global _bh_session, BH_LAST
    if _bh_session is None or _bh_session.closed:
        _bh_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    try:
        async with _bh_session.post(BOTOHUB_URL, json={"chat_id": uid},
                                    headers={"Content-Type": "application/json", "Auth": BOTOHUB_TOKEN}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                BH_LAST = f"HTTP {resp.status}: {str(data)[:150]}"
                log.warning("BotoHub %s", BH_LAST)
                return False, False, []
    except Exception as e:
        BH_LAST = f"{type(e).__name__}: {str(e)[:150]}"
        log.warning("BotoHub недоступен: %s", BH_LAST)
        return False, False, []
    if not isinstance(data, dict):
        BH_LAST = f"ответ не JSON-объект: {str(data)[:150]}"
        return False, False, []
    BH_LAST = f"ответ: {str(data)[:150]}"
    log.info("BotoHub %s", BH_LAST)
    if data.get("completed") or data.get("skip"):
        return True, True, []
    links = [str(x).replace("https://telegram.me/", "https://t.me/") for x in (data.get("tasks") or [])]
    return True, False, links


async def botohub_tasks(uid: int) -> list:
    """Невыполненные задания BotoHub для игрока: [{'name','url'}]. Пустой список = можно пускать дальше (всё выполнено / спонсоров нет / сервис недоступен)."""
    ok, _done, links = await botohub_fetch(uid)
    if not ok:
        return []
    return [{"url": u, "name": _sponsor_name(u, i + 1)} for i, u in enumerate(links)]


# ───────────────────────── ЗАДАНИЯ ОТ BOTOHUB (+0.5 ⭐ за задание) ─────────────────────────
# Раздел «Задания»: бот берёт задания у BotoHub и показывает их игроку. Выполнил (подписался) — задание пропало из выдачи BotoHub,
# значит оно засчитано: игрок получает TASK_REWARD ⭐. Баланс у нас целый, поэтому дробная часть копится в users.task_cents
# (сотые доли звезды), а каждая накопленная целая звезда переходит на баланс. За одну и ту же ссылку платим один раз (hub_tasks).
TASK_REWARD = float(os.getenv("TASK_REWARD", "0.5"))
TASK_CENTS = max(1, round(TASK_REWARD * 100))
TASKS_ON = True                      # кнопка «Задания» видна всегда
TASKS_READY = bool(BOTOHUB_TOKEN)    # сами задания работают, только если задан BOTOHUB_TOKEN (BotoHub)


def fmt_stars(cents: int) -> str:
    return f"{cents / 100:g}"


async def tasks_sync(uid: int) -> dict:
    """Синхронизация заданий игрока с BotoHub + начисление наград.
    Возвращает: ok, tasks=[{name,url,paid}], new (сколько заданий засчитано сейчас), gained_cents, frac_cents, done_total."""
    if not TASKS_READY:                  # токена BotoHub нет — кнопка видна, но список заданий пуст
        log.warning("Задания: не задан BOTOHUB_TOKEN (Render → Environment)")
        return {"ok": False, "why": "no_token", "tasks": [], "new": 0, "gained_cents": 0, "frac_cents": 0, "done_total": 0}
    ok, _completed, links = await botohub_fetch(uid)
    new_n = gained = 0
    paid_urls: set = set()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute("SELECT 1 FROM users WHERE id=$1 FOR UPDATE", uid)
            if ok:
                shown = {x["url"] for x in await con.fetch(
                    "SELECT url FROM hub_tasks WHERE user_id=$1 AND status='shown'", uid)}
                finished = list(shown - set(links))          # было показано, а в выдаче BotoHub его больше нет = выполнено
                if finished:
                    await con.execute(
                        "UPDATE hub_tasks SET status='done', done_at=now() "
                        "WHERE user_id=$1 AND status='shown' AND url = ANY($2::text[])", uid, finished)
                    new_n = len(finished)
                    gained = new_n * TASK_CENTS
                    cents = await con.fetchval(
                        "UPDATE users SET task_cents = task_cents + $2::int WHERE id=$1 RETURNING task_cents", uid, gained)
                    stars = cents // 100
                    if stars:
                        await con.execute(
                            "UPDATE users SET balance = balance + $2::bigint, task_cents = task_cents - ($2::bigint * 100)::int WHERE id=$1",
                            uid, stars)
                if links:
                    await con.execute(
                        "INSERT INTO hub_tasks(user_id, url) SELECT $1::bigint, x FROM unnest($2::text[]) x "
                        "ON CONFLICT DO NOTHING", uid, links)
                    paid_urls = {x["url"] for x in await con.fetch(
                        "SELECT url FROM hub_tasks WHERE user_id=$1 AND status='done' AND url = ANY($2::text[])",
                        uid, links)}
            frac = await con.fetchval("SELECT task_cents FROM users WHERE id=$1", uid) or 0
            done_total = await con.fetchval(
                "SELECT count(*) FROM hub_tasks WHERE user_id=$1 AND status='done'", uid) or 0
    tasks = [{"name": _sponsor_name(u, i + 1), "url": u, "paid": u in paid_urls} for i, u in enumerate(links)]
    return {"ok": ok, "why": "" if ok else "down", "err": BH_LAST, "tasks": tasks, "new": new_n, "gained_cents": gained, "frac_cents": frac, "done_total": done_total}


async def missing_subs(bot: Bot, uid: int) -> list:
    """Каналы, на которые игрок ещё НЕ подписан (BotoHub, а если токена нет — SUB_CHANNELS). Пусто = всё выполнено."""
    if BOTOHUB_TOKEN:
        return await botohub_tasks(uid)
    if not SUB_CHANNELS:
        return []
    res = await asyncio.gather(*[bot.get_chat_member(c["chat"], uid) for c in SUB_CHANNELS], return_exceptions=True)
    bad = []
    for c, m in zip(SUB_CHANNELS, res):
        if isinstance(m, Exception):
            log.warning("Проверка подписки на %s не удалась (бот должен быть админом канала): %s", c["chat"], m)
        elif m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True)):
            bad.append(c)
    return bad


def subs_kb(chs: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 {c['name']}", url=c["url"])] for c in chs if c["url"]]
    rows.append([btn("✅ Проверить подписку", "gate_check")])
    return kb(rows)


GATE_TEXT = "🎁 <b>Добро пожаловать!</b>\n\nЧтобы начать, подпишитесь на наших спонсоров, затем нажмите «Проверить подписку»."


@r.callback_query(F.data == "gate_check")
async def cb_gate_check(cb: CallbackQuery):
    bad = await missing_subs(cb.bot, cb.from_user.id)
    if bad:
        await cb.answer("Вы подписались не на все каналы", show_alert=True)
        await show(cb, GATE_TEXT, subs_kb(bad))
        return
    await pool.execute("UPDATE users SET gate_passed = true WHERE id=$1", cb.from_user.id)
    await cb.answer("✅ Готово!")
    await show(cb, await menu_text(cb.from_user.id), main_kb())


def tasks_view(st: dict):
    """Текст и клавиатура раздела «Задания» для бота."""
    todo = [t for t in st["tasks"] if not t["paid"]]
    lines = ["📋 <b>Задания</b>", "", f"За каждое выполненное задание — <b>+{fmt_stars(TASK_CENTS)} ⭐</b>.",
             "Подпишитесь на канал/бота из списка и нажмите «Проверить»."]
    if not st["ok"]:
        lines += ["", "⚠️ Сервис заданий временно недоступен, попробуйте позже."]
    elif todo:
        lines += ["", f"Доступно заданий: <b>{len(todo)}</b>"]
    else:
        lines += ["", "✅ Сейчас новых заданий нет. Загляните позже!"]
    if st["frac_cents"]:
        lines += ["", f"💫 Накоплено: {fmt_stars(st['frac_cents'])} ⭐ (до зачисления на баланс нужна 1 ⭐)"]
    lines.append(f"Выполнено всего: {st['done_total']}")
    rows = [[InlineKeyboardButton(text=f"📢 {t['name']}  +{fmt_stars(TASK_CENTS)} ⭐", url=t["url"])] for t in todo]
    rows.append([btn("✅ Проверить задания", "tasks")])
    rows.append(BACK_ROW)
    return "\n".join(lines), kb(rows)


@r.callback_query(F.data == "tasks")
async def cb_tasks(cb: CallbackQuery):
    st = await tasks_sync(cb.from_user.id)
    if st["new"]:
        await cb.answer(f"✅ Засчитано заданий: {st['new']}. +{fmt_stars(st['gained_cents'])} ⭐", show_alert=True)
    else:
        await cb.answer()
    text, markup = tasks_view(st)
    await show(cb, text + f"\n\n💰 Баланс: <b>{await get_balance(cb.from_user.id)} ⭐</b>", markup)


# ───────────────────────── /start, меню, профиль, бонус ─────────────────────────
@r.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    args = command.args or ""
    if args.startswith("ref") and args[3:].isdigit():
        ref = int(args[3:])
        await pool.execute(
            "UPDATE users SET ref_by=$2 WHERE id=$1 AND ref_by IS NULL AND $2 <> $1 "
            "AND created_at > now() - interval '1 minute' "
            "AND EXISTS(SELECT 1 FROM users WHERE id=$2)",
            m.from_user.id, ref,
        )
    if SUBS_ON and not await pool.fetchval("SELECT gate_passed FROM users WHERE id=$1", m.from_user.id):
        bad = await missing_subs(m.bot, m.from_user.id)
        if bad:
            await m.answer(GATE_TEXT, reply_markup=subs_kb(bad))
            return
        await pool.execute("UPDATE users SET gate_passed = true WHERE id=$1", m.from_user.id)   # уже подписан — больше не спрашиваем
    await m.answer(await menu_text(m.from_user.id), reply_markup=main_kb())


@r.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await menu_text(m.from_user.id), reply_markup=main_kb())


@r.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, await menu_text(cb.from_user.id), main_kb())
    await cb.answer()


@r.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@r.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    uid = cb.from_user.id
    row = await pool.fetchrow("SELECT balance, total_dep FROM users WHERE id=$1", uid)
    refs = await pool.fetchval("SELECT count(*) FROM users WHERE ref_by=$1", uid)
    inv = await pool.fetchval("SELECT count(*) FROM inventory WHERE user_id=$1", uid)
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"💰 Баланс: <b>{row['balance']} ⭐</b>\n"
        f"💳 Всего пополнено: {row['total_dep']} ⭐\n"
        f"🎒 Предметов: {inv}\n"
        f"👥 Рефералов: {refs}\n\n"
        f"🔗 Ваша реферальная ссылка (+{REF_PERCENT}% с пополнений друзей):\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=ref{uid}</code>"
    )
    await show(cb, text, kb([BACK_ROW]))
    await cb.answer()


@r.callback_query(F.data == "ach")
async def cb_ach(cb: CallbackQuery):
    uid = cb.from_user.id
    await check_achievements(uid)
    got = {x["key"] for x in await pool.fetch("SELECT key FROM achievements WHERE user_id=$1", uid)}
    u = await pool.fetchrow(
        "SELECT cases_opened, mines_wins, up_wins, best_mines, best_up FROM users WHERE id=$1", uid
    )
    lines = []
    for key, (title, desc, _c) in ACHIEVEMENTS.items():
        mark = "✅" if key in got else "🔒"
        lines.append(f"{mark} {title} — {desc}")
    text = (
        f"🏆 <b>Достижения</b> ({len(got)}/{len(ACHIEVEMENTS)})\n\n" + "\n".join(lines) + "\n\n"
        f"📊 Кейсов открыто: {u['cases_opened']}\n"
        f"💣 Побед в сапёре: {u['mines_wins']} (лучший x{u['best_mines']:.1f})\n"
        f"🚀 Побед в апгрейдере: {u['up_wins']} (лучший x{u['best_up']:g})"
    )
    await show(cb, text, kb([BACK_ROW]))
    await cb.answer()


@r.callback_query(F.data == "bonus")
async def cb_bonus(cb: CallbackQuery):
    uid = cb.from_user.id
    amount = secrets.randbelow(5) + 1
    got = await pool.fetchval(
        "UPDATE users SET balance = balance + $2, last_bonus = now() "
        "WHERE id=$1 AND (last_bonus IS NULL OR last_bonus < now() - interval '24 hours') RETURNING 1",
        uid, amount,
    )
    if got:
        await cb.answer(f"🎅 Бонус получен: +{amount} ⭐", show_alert=True)
        await show(cb, await menu_text(uid), main_kb())
        return
    left = await pool.fetchval(
        "SELECT (last_bonus + interval '24 hours') - now() FROM users WHERE id=$1", uid
    )
    secs = int(left.total_seconds()) if left else 0
    await cb.answer(f"⏳ Следующий бонус через {secs // 3600} ч {secs % 3600 // 60} мин", show_alert=True)


# ───────────────────────── КЕЙСЫ ─────────────────────────
@r.callback_query(F.data == "cases")
async def cb_cases(cb: CallbackQuery):
    rows = [[btn(f"{c['name']} — {c['price']} ⭐", f"case:{cid}")] for cid, c in CASES.items()]
    rows.append(BACK_ROW)
    await show(cb, "🎁 <b>Кейсы</b>\n\nВыберите кейс:", kb(rows))
    await cb.answer()


@r.callback_query(F.data.startswith("case:"))
async def cb_case(cb: CallbackQuery):
    cid = cb.data.split(":")[1]
    c = CASES.get(cid)
    if not c:
        await cb.answer("Кейс не найден", show_alert=True)
        return
    total = sum(w for _, w in c["drops"])
    drops = sorted(c["drops"], key=lambda d: drop_value(d[0]), reverse=True)
    lines = [f"{drop_label(k)} — {w / total * 100:.1f}%" for k, w in drops]
    text = (
        f"{c['name']}\nЦена: <b>{c['price']} ⭐</b>\n"
        f"Возврат игрокам (RTP): ~{case_rtp(c):.0f}%\n\n<b>Возможный дроп:</b>\n" + "\n".join(lines)
    )
    rows = [
        [btn(f"Открыть ×1 ({c['price']} ⭐)", f"open:{cid}:1")],
        [btn(f"Открыть ×5 ({c['price'] * 5} ⭐)", f"open:{cid}:5")],
        [btn("⬅️ К кейсам", "cases")],
    ]
    await show(cb, text, kb(rows))
    await cb.answer()


async def open_case_once(uid: int, cid: str):
    c = CASES[cid]
    async with pool.acquire() as con:
        async with con.transaction():
            ok = await con.fetchval(
                "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                uid, c["price"],
            )
            if not ok:
                return None
            key = roll(c["drops"], await fair.rng(con, uid))
            if key.startswith("s:"):
                await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, int(key[2:]))
            else:
                await con.execute("INSERT INTO inventory(user_id, item) VALUES($1,$2)", uid, key[2:])
                # лента «Live»: дроп появляется у всех только через LIVE_DELAY сек, чтобы не спойлерить рулетку
                await con.execute(
                    "INSERT INTO live_drops(user_id, case_id, item, visible_at) "
                    "VALUES($1,$2,$3, now() + make_interval(secs => $4))",
                    uid, cid, key[2:], float(LIVE_DELAY))
            rare = 1 if key.startswith("i:") and ITEMS[key[2:]]["value"] >= RARE_VALUE else 0
            await con.execute(
                "UPDATE users SET cases_opened = cases_opened + 1, rare_drops = rare_drops + $2 WHERE id=$1",
                uid, rare,
            )
    return key


async def open_cases(uid: int, cid: str, n: int) -> list:
    """Открывает до n кейсов за ОДНУ транзакцию (≈5 обращений к базе вместо ≈7 на каждый кейс).
    Если звёзд хватает не на все — открывает сколько может. Ничего не хватает — пустой список."""
    c = CASES[cid]
    price = c["price"]
    async with pool.acquire() as con:
        async with con.transaction():
            k = await con.fetchval(
                "WITH b AS (SELECT id, LEAST($3::int, (balance / $2)::int) AS k FROM users WHERE id=$1 FOR UPDATE) "
                "UPDATE users u SET balance = u.balance - b.k * $2 FROM b WHERE u.id = b.id RETURNING b.k",
                uid, price, n)
            if not k:
                return []
            rngs = await fair.rng_n(con, uid, k)
            keys = [roll(c["drops"], g) for g in rngs]
            star = sum(int(x[2:]) for x in keys if x.startswith("s:"))
            items = [x[2:] for x in keys if x.startswith("i:")]
            rare = sum(1 for x in items if ITEMS[x]["value"] >= RARE_VALUE)
            await con.execute(
                "WITH i AS (INSERT INTO inventory(user_id, item) SELECT $1::bigint, x FROM unnest($2::text[]) AS x), "
                "l AS (INSERT INTO live_drops(user_id, case_id, item, visible_at) "
                "      SELECT $1::bigint, $3::text, x, now() + make_interval(secs => $4) FROM unnest($2::text[]) AS x) "
                "UPDATE users SET balance = balance + $5, cases_opened = cases_opened + $6, rare_drops = rare_drops + $7 WHERE id=$1",
                uid, items, cid, float(LIVE_DELAY), star, k, rare)
    return keys


# ── Ежедневный бесплатный кейс (шансы нигде не показываются) ──
FREE_CASE_COOLDOWN_H = 24
FREE_CASE_DROPS = [("s:0", 80), ("s:1", 15), ("s:5", 4), ("s:10", 1)]   # веса = проценты, сумма 100


async def open_free_case(uid: int):
    """Бесплатный кейс раз в 24 часа. Возвращает ключ дропа («s:N») или None, если ещё не доступен."""
    async with pool.acquire() as con:
        async with con.transaction():
            ok = await con.fetchval(
                "UPDATE users SET last_free_case = now() WHERE id=$1 AND "
                "(last_free_case IS NULL OR last_free_case < now() - make_interval(hours => $2)) RETURNING 1",
                uid, FREE_CASE_COOLDOWN_H)
            if not ok:
                return None
            key = roll(FREE_CASE_DROPS, await fair.rng(con, uid))
            amount = int(key[2:])
            if amount:
                await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, amount)
    return key


@r.callback_query(F.data.startswith("open:"))
async def cb_open(cb: CallbackQuery):
    _, cid, n_str = cb.data.split(":")
    if cid not in CASES or n_str not in ("1", "5"):
        await cb.answer("Ошибка", show_alert=True)
        return
    n = int(n_str)
    uid = cb.from_user.id
    c = CASES[cid]
    results = []
    for _ in range(n):
        key = await open_case_once(uid, cid)
        if key is None:
            break
        results.append(key)
    if not results:
        await cb.answer("❌ Недостаточно средств. Пополните баланс.", show_alert=True)
        return
    spent = len(results) * c["price"]
    won = sum(drop_value(k) for k in results)
    text = f"{c['name']}\n\n🎉 <b>Выпало:</b>\n" + "\n".join(drop_label(k) for k in results)
    text += f"\n\nПотрачено: {spent} ⭐ · Выигрыш: {won} ⭐"
    text += f"\n💰 Баланс: <b>{await get_balance(uid)} ⭐</b>"
    rows = [
        [btn(f"🔁 Ещё ×{len(results)}", f"open:{cid}:{n_str}")],
        [btn("🎒 Инвентарь", "inv"), btn("⬅️ К кейсам", "cases")],
    ]
    await show(cb, text, kb(rows))
    await cb.answer()
    await notify_ach(cb.bot, uid)


# ───────────────────────── ИНВЕНТАРЬ ─────────────────────────
async def render_inventory(cb: CallbackQuery):
    rows_db = await pool.fetch(
        "SELECT item, count(*) AS n FROM inventory WHERE user_id=$1 GROUP BY item", cb.from_user.id
    )
    rows_db = [x for x in rows_db if x["item"] in ITEMS]
    if not rows_db:
        await show(cb, "🎒 <b>Инвентарь пуст</b>\n\nОткрывайте кейсы, чтобы получить подарки!",
                   kb([[btn("🎁 Кейсы", "cases")], BACK_ROW]))
        return
    rows_db.sort(key=lambda x: ITEMS[x["item"]]["value"], reverse=True)
    rows = []
    for x in rows_db:
        it = ITEMS[x["item"]]
        rows.append([btn(f"{it['emoji']} {it['name']} ×{x['n']} · {it['value']} ⭐", f"invi:{x['item']}")])
    rows.append(BACK_ROW)
    await show(cb, "🎒 <b>Ваш инвентарь</b>\n\nВыберите предмет:", kb(rows))


@r.callback_query(F.data == "inv")
async def cb_inv(cb: CallbackQuery):
    await render_inventory(cb)
    await cb.answer()


@r.callback_query(F.data.startswith("invi:"))
async def cb_inv_item(cb: CallbackQuery):
    item = cb.data.split(":")[1]
    if item not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    n = await pool.fetchval("SELECT count(*) FROM inventory WHERE user_id=$1 AND item=$2", cb.from_user.id, item)
    if not n:
        await render_inventory(cb)
        await cb.answer()
        return
    it = ITEMS[item]
    text = f"{it['emoji']} <b>{it['name']}</b>\nСтоимость: {it['value']} ⭐\nУ вас: ×{n}"
    rows = [
        [btn(f"💰 Продать 1 (+{it['value']} ⭐)", f"sell:{item}")],
        [btn(f"💰 Продать все (+{it['value'] * n} ⭐)", f"sella:{item}")],
        [btn("🚀 В апгрейдер", f"upi:{item}"), btn("📤 Вывести", f"wdi:{item}")],
        [btn("⬅️ Инвентарь", "inv")],
    ]
    await show(cb, text, kb(rows))
    await cb.answer()


async def sell_items(uid: int, item: str, sell_all: bool) -> int:
    async with pool.acquire() as con:
        async with con.transaction():
            if sell_all:
                rows = await con.fetch(
                    "DELETE FROM inventory WHERE user_id=$1 AND item=$2 RETURNING id", uid, item
                )
            else:
                rows = await con.fetch(
                    "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                    "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id",
                    uid, item,
                )
            n = len(rows)
            if n:
                await con.execute(
                    "UPDATE users SET balance = balance + $2 WHERE id=$1", uid, n * ITEMS[item]["value"]
                )
    return n


@r.callback_query(F.data.startswith("sell:") | F.data.startswith("sella:"))
async def cb_sell(cb: CallbackQuery):
    action, item = cb.data.split(":")
    if item not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    n = await sell_items(cb.from_user.id, item, action == "sella")
    await render_inventory(cb)
    if n:
        await cb.answer(f"✅ Продано: {n} шт. (+{n * ITEMS[item]['value']} ⭐)", show_alert=True)
    else:
        await cb.answer("Предмета уже нет", show_alert=True)


# ───────────────────────── САПЁР ─────────────────────────
def mines_text(bet: int, m: int, opened_n: int) -> str:
    cur = mines_mult(m, opened_n)
    text = (
        f"💣 <b>Сапёр</b> · ставка {bet} ⭐ · мин: {m}\n"
        f"Открыто: {opened_n}/{CELLS - m}\n"
        f"Текущий множитель: <b>x{cur:.2f}</b>"
    )
    if opened_n < CELLS - m:
        text += f"\nСледующий: x{mines_mult(m, opened_n + 1):.2f}"
    return text


def mines_kb(opened, mines=None, hit=None, bet=0, m=0, finished=False) -> InlineKeyboardMarkup:
    rows = []
    for rr in range(SIDE):
        row = []
        for cc in range(SIDE):
            i = rr * SIDE + cc
            if hit is not None and i == hit:
                t = "💥"
            elif i in opened:
                t = "💎"
            elif finished and mines and i in mines:
                t = "💣"
            else:
                t = "⬜"
            row.append(btn(t, "noop" if finished else f"mc:{i}"))
        rows.append(row)
    if finished:
        rows.append([btn("🔄 Играть снова", "mines"), btn("🏠 Меню", "menu")])
    elif opened:
        payout = mines_payout(bet, m, len(opened))
        rows.append([btn(f"💰 Забрать {payout} ⭐", "mcash")])
    return kb(rows)


def mines_mine_rows(bet: int):
    return [[btn(f"{m} мин", f"mm:{bet}:{m}") for m in MINES_OPTIONS], [btn("⬅️ Назад", "mines")]]


@r.callback_query(F.data == "mines")
async def cb_mines(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.from_user.id
    g = await pool.fetchrow("SELECT * FROM mines_games WHERE user_id=$1", uid)
    if g:
        opened = parse_ints(g["opened"])
        await show(cb, mines_text(g["bet"], g["mines"], len(opened)),
                   mines_kb(opened, bet=g["bet"], m=g["mines"]))
        await cb.answer()
        return
    rows = []
    row = []
    for b in BET_PRESETS:
        row.append(btn(f"{b} ⭐", f"mb:{b}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✏️ Своя ставка", "mbc"), btn("🎰 Ва-банк", "mball")])
    rows.append(BACK_ROW)
    text = (
        "💣 <b>Сапёр</b>\n\nПоле 4×4. Открывайте безопасные ячейки 💎 — множитель растёт. "
        "Наткнётесь на мину 💣 — ставка сгорает. Забрать выигрыш можно в любой момент.\n\n"
        f"Ставка — любая от {MINES_MIN_BET} ⭐, без верхнего лимита. Выберите или введите свою:"
    )
    await show(cb, text, kb(rows))
    await cb.answer()


@r.callback_query(F.data == "mbc")
async def cb_mines_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(St.mines_bet)
    bal = await get_balance(cb.from_user.id)
    await show(cb, f"✏️ Введите ставку числом (от {MINES_MIN_BET} ⭐, у вас {bal} ⭐):", kb([[btn("❌ Отмена", "mines")]]))
    await cb.answer()


@r.message(St.mines_bet, F.text)
async def msg_mines_bet(m: Message, state: FSMContext):
    txt = m.text.strip().replace(" ", "")
    if not txt.isdigit() or len(txt) > 15 or int(txt) < MINES_MIN_BET:
        await m.answer(f"Введите целое число от {MINES_MIN_BET}.")
        return
    bet = int(txt)
    bal = await get_balance(m.from_user.id)
    if bet > bal:
        await m.answer(f"Недостаточно средств. Ваш баланс: {bal} ⭐")
        return
    await state.clear()
    await m.answer(f"💣 Ставка: <b>{bet} ⭐</b>\n\nСколько мин на поле?\nЧем больше мин — тем выше множители.",
                   reply_markup=kb(mines_mine_rows(bet)))


@r.callback_query(F.data == "mball")
async def cb_mines_all(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    if bal < MINES_MIN_BET:
        await cb.answer("❌ Недостаточно средств. Пополните баланс.", show_alert=True)
        return
    await show(cb, f"💣 Ставка: <b>{bal} ⭐</b> (ва-банк)\n\nСколько мин на поле?", kb(mines_mine_rows(bal)))
    await cb.answer()


@r.callback_query(F.data.startswith("mb:"))
async def cb_mines_bet(cb: CallbackQuery):
    bet = int(cb.data.split(":")[1])
    if bet not in BET_PRESETS:
        await cb.answer("Ошибка", show_alert=True)
        return
    await show(cb, f"💣 Ставка: <b>{bet} ⭐</b>\n\nСколько мин на поле?\nЧем больше мин — тем выше множители.",
               kb(mines_mine_rows(bet)))
    await cb.answer()


@r.callback_query(F.data.startswith("mm:"))
async def cb_mines_start(cb: CallbackQuery):
    _, bet_s, m_s = cb.data.split(":")
    bet, m = int(bet_s), int(m_s)
    if bet < MINES_MIN_BET or m not in MINES_OPTIONS:
        await cb.answer("Ошибка", show_alert=True)
        return
    uid = cb.from_user.id
    async with pool.acquire() as con:
        async with con.transaction():
            exists = await con.fetchval("SELECT 1 FROM mines_games WHERE user_id=$1", uid)
            if exists:
                status = "active"
            else:
                ok = await con.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                    uid, bet,
                )
                if not ok:
                    status = "nofunds"
                else:
                    cells = secrets.SystemRandom().sample(range(CELLS), m)
                    await con.execute(
                        "INSERT INTO mines_games(user_id, bet, mines, mine_cells, opened) VALUES($1,$2,$3,$4,'')",
                        uid, bet, m, join_ints(cells),
                    )
                    status = "ok"
    if status == "nofunds":
        await cb.answer("❌ Недостаточно средств. Пополните баланс.", show_alert=True)
        return
    if status == "active":
        g = await pool.fetchrow("SELECT * FROM mines_games WHERE user_id=$1", uid)
        opened = parse_ints(g["opened"])
        await show(cb, mines_text(g["bet"], g["mines"], len(opened)), mines_kb(opened, bet=g["bet"], m=g["mines"]))
        await cb.answer()
        return
    await show(cb, mines_text(bet, m, 0), mines_kb([], bet=bet, m=m))
    await cb.answer()


@r.callback_query(F.data.startswith("mc:"))
async def cb_mines_click(cb: CallbackQuery):
    i = int(cb.data.split(":")[1])
    if not 0 <= i < CELLS:
        await cb.answer()
        return
    uid = cb.from_user.id
    res = None
    async with pool.acquire() as con:
        async with con.transaction():
            g = await con.fetchrow("SELECT * FROM mines_games WHERE user_id=$1 FOR UPDATE", uid)
            if g:
                mines = parse_ints(g["mine_cells"])
                opened = parse_ints(g["opened"])
                bet, m = int(g["bet"]), int(g["mines"])
                if i in opened:
                    res = {"status": "dup"}
                elif i in mines:
                    await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                    res = {"status": "boom"}
                else:
                    opened.append(i)
                    if len(opened) == CELLS - m or mines_capped(mines_payout(bet, m, len(opened))):
                        payout = mines_payout(bet, m, len(opened))
                        await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                        await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, payout)
                        await record_mines_win(con, uid, bet, payout)
                        res = {"status": "win", "payout": payout}
                    else:
                        await con.execute(
                            "UPDATE mines_games SET opened=$2 WHERE user_id=$1", uid, join_ints(opened)
                        )
                        res = {"status": "ok"}
                res.update({"bet": bet, "m": m, "mines": mines, "opened": opened})
    if res is None:
        await cb.answer("Игра не найдена", show_alert=True)
        await show(cb, await menu_text(uid), main_kb())
        return
    st = res["status"]
    if st == "dup":
        await cb.answer()
        return
    if st == "boom":
        text = f"💥 <b>БУМ!</b> Вы подорвались на мине.\nПотеряно: {res['bet']} ⭐\n💰 Баланс: <b>{await get_balance(uid)} ⭐</b>"
        await show(cb, text, mines_kb(res["opened"], res["mines"], hit=i, finished=True))
    elif st == "win":
        text = (f"🏆 <b>Максимум достигнут!</b>\nВыигрыш: <b>{res['payout']} ⭐</b>\n"
                f"💰 Баланс: <b>{await get_balance(uid)} ⭐</b>")
        await show(cb, text, mines_kb(res["opened"], res["mines"], finished=True))
    else:
        await show(cb, mines_text(res["bet"], res["m"], len(res["opened"])),
                   mines_kb(res["opened"], bet=res["bet"], m=res["m"]))
    await cb.answer()
    await notify_ach(cb.bot, uid)


@r.callback_query(F.data == "mcash")
async def cb_mines_cash(cb: CallbackQuery):
    uid = cb.from_user.id
    res = None
    async with pool.acquire() as con:
        async with con.transaction():
            g = await con.fetchrow("SELECT * FROM mines_games WHERE user_id=$1 FOR UPDATE", uid)
            if g:
                opened = parse_ints(g["opened"])
                if opened:
                    bet, m = int(g["bet"]), int(g["mines"])
                    payout = mines_payout(bet, m, len(opened))
                    await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                    await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, payout)
                    await record_mines_win(con, uid, bet, payout)
                    res = {"payout": payout, "bet": bet, "mines": parse_ints(g["mine_cells"]), "opened": opened}
    if res is None:
        await cb.answer("Сначала откройте хотя бы одну ячейку", show_alert=True)
        return
    text = (f"💰 <b>Выигрыш забран!</b>\nСтавка: {res['bet']} ⭐ → <b>{res['payout']} ⭐</b>\n"
            f"💰 Баланс: <b>{await get_balance(uid)} ⭐</b>")
    await show(cb, text, mines_kb(res["opened"], res["mines"], finished=True))
    await cb.answer()
    await notify_ach(cb.bot, uid)


# ───────────────────────── АПГРЕЙДЕР ─────────────────────────
@r.callback_query(F.data == "up")
async def cb_up(cb: CallbackQuery):
    text = (
        "🚀 <b>Апгрейдер</b>\n\nВыберите ставку и множитель. Чем выше множитель — тем меньше шанс. "
        "Победа: ставка × множитель. Проигрыш: ставка сгорает."
    )
    rows = [
        [btn("🎒 Предмет из инвентаря", "upinv")],
        [btn("⭐ Ставка с баланса", "upbal")],
        BACK_ROW,
    ]
    await show(cb, text, kb(rows))
    await cb.answer()


@r.callback_query(F.data == "upinv")
async def cb_up_inv(cb: CallbackQuery):
    rows_db = await pool.fetch(
        "SELECT item, count(*) AS n FROM inventory WHERE user_id=$1 GROUP BY item", cb.from_user.id
    )
    rows_db = [x for x in rows_db if x["item"] in ITEMS]
    if not rows_db:
        await show(cb, "🎒 Инвентарь пуст — апгрейдить нечего.", kb([[btn("🎁 Кейсы", "cases")], [btn("⬅️ Назад", "up")]]))
        await cb.answer()
        return
    rows_db.sort(key=lambda x: ITEMS[x["item"]]["value"], reverse=True)
    rows = []
    for x in rows_db:
        it = ITEMS[x["item"]]
        rows.append([btn(f"{it['emoji']} {it['name']} ×{x['n']} · {it['value']} ⭐", f"upi:{x['item']}")])
    rows.append([btn("⬅️ Назад", "up")])
    await show(cb, "🚀 Выберите предмет для апгрейда:", kb(rows))
    await cb.answer()


def mult_rows(prefix: str):
    rows = []
    row = []
    for key, mult in UP_MULTS.items():
        row.append(btn(f"x{key} ({UP_RTP / mult * 100:.1f}%)", f"{prefix}:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


@r.callback_query(F.data.startswith("upi:"))
async def cb_up_item(cb: CallbackQuery):
    item = cb.data.split(":")[1]
    if item not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    it = ITEMS[item]
    rows = mult_rows(f"upx:i:{item}") + [[btn("⬅️ Назад", "upinv")]]
    await show(cb, f"🚀 Апгрейд: {it['emoji']} {it['name']} ({it['value']} ⭐)\n\nВыберите множитель (шанс успеха в скобках):", kb(rows))
    await cb.answer()


@r.callback_query(F.data == "upbal")
async def cb_up_bal(cb: CallbackQuery):
    rows = []
    row = []
    for b in BET_PRESETS:
        row.append(btn(f"{b} ⭐", f"upb:{b}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("⬅️ Назад", "up")])
    await show(cb, "🚀 Выберите ставку с баланса:", kb(rows))
    await cb.answer()


@r.callback_query(F.data.startswith("upb:"))
async def cb_up_bet(cb: CallbackQuery):
    bet = int(cb.data.split(":")[1])
    if bet not in BET_PRESETS:
        await cb.answer("Ошибка", show_alert=True)
        return
    rows = mult_rows(f"upx:b:{bet}") + [[btn("⬅️ Назад", "upbal")]]
    await show(cb, f"🚀 Ставка: <b>{bet} ⭐</b>\n\nВыберите множитель (шанс успеха в скобках):", kb(rows))
    await cb.answer()


@r.callback_query(F.data.startswith("upx:"))
async def cb_up_go(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer("Ошибка", show_alert=True)
        return
    _, mode, ref, mkey = parts
    if mkey not in UP_MULTS or mode not in ("i", "b"):
        await cb.answer("Ошибка", show_alert=True)
        return
    if mode == "i" and ref not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    if mode == "b" and (not ref.isdigit() or int(ref) not in BET_PRESETS):
        await cb.answer("Ошибка", show_alert=True)
        return
    uid = cb.from_user.id
    mult = UP_MULTS[mkey]
    chance = UP_RTP / mult
    res = None
    async with pool.acquire() as con:
        async with con.transaction():
            if mode == "i":
                row = await con.fetchrow(
                    "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                    "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id",
                    uid, ref,
                )
                stake = ITEMS[ref]["value"] if row else None
            else:
                ok = await con.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                    uid, int(ref),
                )
                stake = int(ref) if ok else None
            if stake is not None:
                win = secrets.randbelow(10000) < int(chance * 10000)
                payout = int(stake * mult) if win else 0
                if payout:
                    await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, payout)
                    await con.execute(
                        "UPDATE users SET up_wins = up_wins + 1, best_up = GREATEST(best_up, $2) WHERE id=$1",
                        uid, float(mult),
                    )
                res = {"win": win, "stake": stake, "payout": payout}
    if res is None:
        await cb.answer("❌ Недостаточно средств или предмета нет", show_alert=True)
        return
    if res["win"]:
        text = f"✅ <b>Успех!</b> x{mkey}\nСтавка {res['stake']} ⭐ → <b>{res['payout']} ⭐</b>"
    else:
        text = f"❌ <b>Неудача.</b> Шанс был {chance * 100:.1f}%\nПотеряно: {res['stake']} ⭐"
    text += f"\n💰 Баланс: <b>{await get_balance(uid)} ⭐</b>"
    await show(cb, text, kb([[btn("🚀 Ещё раз", "up")], BACK_ROW]))
    await cb.answer()
    await notify_ach(cb.bot, uid)


# ───────────────────────── ПОПОЛНЕНИЕ (Telegram Stars) ─────────────────────────
@r.callback_query(F.data == "dep")
async def cb_dep(cb: CallbackQuery):
    rows = []
    row = []
    for a in DEPOSIT_PRESETS:
        row.append(btn(f"{a} ⭐", f"depa:{a}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✏️ Другая сумма", "depc")])
    if CRYPTO_TOKEN:
        rows.append([btn("💎 Пополнить криптой", "depcr")])
    rows.append(BACK_ROW)
    await show(cb, "💳 <b>Пополнение</b>\n\nОплата Telegram Stars ⭐. 1 ⭐ = 1 ⭐ на балансе.\nВыберите сумму:", kb(rows))
    await cb.answer()


async def send_deposit_invoice(bot: Bot, chat_id: int, amount: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title="Пополнение баланса",
        description=f"Пополнение баланса на {amount} ⭐",
        payload=f"dep:{chat_id}:{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение", amount=amount)],
    )


@r.callback_query(F.data.startswith("depa:"))
async def cb_dep_amount(cb: CallbackQuery):
    amount = int(cb.data.split(":")[1])
    if amount not in DEPOSIT_PRESETS:
        await cb.answer("Ошибка", show_alert=True)
        return
    await send_deposit_invoice(cb.bot, cb.from_user.id, amount)
    await cb.answer()


@r.callback_query(F.data == "depc")
async def cb_dep_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(St.dep_amount)
    await show(cb, f"✏️ Введите сумму пополнения числом ({MIN_DEPOSIT}–{MAX_DEPOSIT} ⭐):", kb([[btn("❌ Отмена", "menu")]]))
    await cb.answer()


@r.message(St.dep_amount, F.text)
async def msg_dep_amount(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt.isdigit() or not MIN_DEPOSIT <= int(txt) <= MAX_DEPOSIT:
        await m.answer(f"Введите число от {MIN_DEPOSIT} до {MAX_DEPOSIT}.")
        return
    await state.clear()
    await send_deposit_invoice(m.bot, m.from_user.id, int(txt))


CRYPTO_PRESETS = [100, 250, 500, 1000, 5000]


async def send_crypto_invoice(bot: Bot, chat_id: int, stars: int):
    try:
        inv = await crypto_create_invoice(chat_id, stars)
    except CryptoError as e:
        await bot.send_message(chat_id, f"❌ {e}")
        return
    except Exception:
        log.exception("crypto invoice failed")
        await bot.send_message(chat_id, "❌ Не удалось создать счёт. Попробуйте позже.")
        return
    await bot.send_message(
        chat_id,
        f"💎 <b>Счёт на оплату</b>\n\nК оплате: <b>{inv['amount']} {inv['asset']}</b>\n"
        f"Будет зачислено: <b>{stars} ⭐</b>\n\nОплатите в @CryptoBot — баланс пополнится автоматически (обычно до минуты).",
        reply_markup=kb([[InlineKeyboardButton(text="💎 Оплатить", url=inv["link"])], BACK_ROW]),
    )


@r.callback_query(F.data == "depcr")
async def cb_dep_crypto(cb: CallbackQuery, state: FSMContext):
    if not CRYPTO_TOKEN:
        await cb.answer("Крипто-оплата пока не подключена", show_alert=True)
        return
    rows, row = [], []
    for a in CRYPTO_PRESETS:
        row.append(btn(f"{a} ⭐", f"depcra:{a}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✏️ Другая сумма", "depcrc")])
    rows.append([btn("⬅️ Назад", "dep")])
    await show(cb, f"💎 <b>Пополнение криптой</b> (@CryptoBot)\n\nКурс: <b>100 ⭐ = ${100 / CRYPTO_STARS_PER_USD:g}</b> (1 {CRYPTO_ASSET} = {CRYPTO_STARS_PER_USD:g} ⭐)\n"
                   f"Выберите сумму в ⭐:", kb(rows))
    await cb.answer()


@r.callback_query(F.data.startswith("depcra:"))
async def cb_dep_crypto_amount(cb: CallbackQuery):
    amount = int(cb.data.split(":")[1])
    if amount not in CRYPTO_PRESETS:
        await cb.answer("Ошибка", show_alert=True)
        return
    await send_crypto_invoice(cb.bot, cb.from_user.id, amount)
    await cb.answer()


@r.callback_query(F.data == "depcrc")
async def cb_dep_crypto_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(St.dep_crypto)
    await show(cb, f"✏️ Введите сумму пополнения в ⭐ ({CRYPTO_MIN_DEPOSIT}–{CRYPTO_MAX_DEPOSIT}):", kb([[btn("❌ Отмена", "menu")]]))
    await cb.answer()


@r.message(St.dep_crypto, F.text)
async def msg_dep_crypto(m: Message, state: FSMContext):
    txt = m.text.strip().replace(" ", "")
    if not txt.isdigit() or not CRYPTO_MIN_DEPOSIT <= int(txt) <= CRYPTO_MAX_DEPOSIT:
        await m.answer(f"Введите число от {CRYPTO_MIN_DEPOSIT} до {CRYPTO_MAX_DEPOSIT}.")
        return
    await state.clear()
    await send_crypto_invoice(m.bot, m.from_user.id, int(txt))


@r.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@r.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    if sp.currency != "XTR":
        return
    done = await credit_deposit(m.from_user.id, sp.total_amount, sp.telegram_payment_charge_id)
    if done:
        await m.answer(
            f"✅ Баланс пополнен на <b>{sp.total_amount} ⭐</b>\n💰 Баланс: <b>{await get_balance(m.from_user.id)} ⭐</b>",
            reply_markup=main_kb(),
        )
        await notify_ach(m.bot, m.from_user.id)


# ───────────────────────── ВЫВОД ─────────────────────────
async def can_withdraw(uid: int):
    dep = await pool.fetchval("SELECT total_dep FROM users WHERE id=$1", uid)
    return (dep or 0) >= MIN_DEPOSIT_TO_WITHDRAW


async def notify_admins(bot: Bot, wid: int, user, text: str):
    uname = f"@{user.username}" if user.username else "без username"
    msg = f"🆕 <b>Заявка на вывод #{wid}</b>\nПользователь: {uname} (<code>{user.id}</code>)\n{text}"
    markup = kb([[btn("✅ Выполнено", f"wda:{wid}"), btn("❌ Отклонить", f"wdr:{wid}")]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, msg, reply_markup=markup)
        except Exception:
            log.exception("Не удалось уведомить админа %s", aid)


@r.callback_query(F.data == "wd")
async def cb_wd(cb: CallbackQuery):
    text = (
        "💸 <b>Вывод</b>\n\n"
        f"Минимум для вывода звёзд: {MIN_WITHDRAW} ⭐.\n"
        f"Вывод доступен после пополнений на сумму от {MIN_DEPOSIT_TO_WITHDRAW} ⭐.\n"
        "Заявки обрабатываются администратором вручную."
    )
    rows = [
        [btn("⭐ Вывести звёзды", "wds")],
        [btn("🎁 Вывести подарок", "wdg")],
        BACK_ROW,
    ]
    await show(cb, text, kb(rows))
    await cb.answer()


@r.callback_query(F.data == "wds")
async def cb_wd_stars(cb: CallbackQuery, state: FSMContext):
    if not await can_withdraw(cb.from_user.id):
        await cb.answer(f"Вывод откроется после пополнений от {MIN_DEPOSIT_TO_WITHDRAW} ⭐", show_alert=True)
        return
    await state.set_state(St.wd_amount)
    bal = await get_balance(cb.from_user.id)
    await show(cb, f"⭐ Введите сумму вывода числом (мин. {MIN_WITHDRAW}, у вас {bal} ⭐):", kb([[btn("❌ Отмена", "menu")]]))
    await cb.answer()


@r.message(St.wd_amount, F.text)
async def msg_wd_amount(m: Message, state: FSMContext):
    txt = m.text.strip()
    bal = await get_balance(m.from_user.id)
    if not txt.isdigit() or int(txt) < MIN_WITHDRAW:
        await m.answer(f"Введите число не меньше {MIN_WITHDRAW}.")
        return
    if int(txt) > bal:
        await m.answer(f"Недостаточно средств. Ваш баланс: {bal} ⭐")
        return
    await state.update_data(amount=int(txt))
    await state.set_state(St.wd_details)
    await m.answer("Укажите реквизиты для выплаты (ваш @username):")


@r.message(St.wd_details, F.text)
async def msg_wd_details(m: Message, state: FSMContext):
    data = await state.get_data()
    amount = int(data.get("amount", 0))
    details = m.text.strip()[:300]
    await state.clear()
    if amount < MIN_WITHDRAW:
        await m.answer("Что-то пошло не так, начните заново.", reply_markup=main_kb())
        return
    wid = None
    async with pool.acquire() as con:
        async with con.transaction():
            ok = await con.fetchval(
                "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                m.from_user.id, amount,
            )
            if ok:
                wid = await con.fetchval(
                    "INSERT INTO withdrawals(user_id, kind, amount, details) VALUES($1,'stars',$2,$3) RETURNING id",
                    m.from_user.id, amount, details,
                )
    if wid is None:
        await m.answer("❌ Недостаточно средств.", reply_markup=main_kb())
        return
    await m.answer(f"✅ Заявка #{wid} на вывод {amount} ⭐ создана. Ожидайте обработки.", reply_markup=main_kb())
    await notify_admins(m.bot, wid, m.from_user, f"Тип: звёзды\nСумма: {amount} ⭐\nРеквизиты: {details}")


@r.callback_query(F.data == "wdg")
async def cb_wd_gift(cb: CallbackQuery):
    if not await can_withdraw(cb.from_user.id):
        await cb.answer(f"Вывод откроется после пополнений от {MIN_DEPOSIT_TO_WITHDRAW} ⭐", show_alert=True)
        return
    rows_db = await pool.fetch(
        "SELECT item, count(*) AS n FROM inventory WHERE user_id=$1 GROUP BY item", cb.from_user.id
    )
    rows_db = [x for x in rows_db if x["item"] in ITEMS]
    if not rows_db:
        await show(cb, "🎒 В инвентаре нет подарков для вывода.", kb([[btn("🎁 Кейсы", "cases")], [btn("⬅️ Назад", "wd")]]))
        await cb.answer()
        return
    rows = []
    for x in rows_db:
        it = ITEMS[x["item"]]
        rows.append([btn(f"{it['emoji']} {it['name']} ×{x['n']}", f"wdi:{x['item']}")])
    rows.append([btn("⬅️ Назад", "wd")])
    await show(cb, "🎁 Выберите подарок для вывода:", kb(rows))
    await cb.answer()


@r.callback_query(F.data.startswith("wdi:"))
async def cb_wd_item(cb: CallbackQuery):
    item = cb.data.split(":")[1]
    if item not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    if not await can_withdraw(cb.from_user.id):
        await cb.answer(f"Вывод откроется после пополнений от {MIN_DEPOSIT_TO_WITHDRAW} ⭐", show_alert=True)
        return
    if not cb.from_user.username:
        await cb.answer("Установите username в настройках Telegram — подарок отправляется по нему.", show_alert=True)
        return
    it = ITEMS[item]
    text = (f"📤 Вывести {it['emoji']} <b>{it['name']}</b>?\n\nПодарок будет отправлен на ваш аккаунт "
            f"@{cb.from_user.username} администратором вручную.")
    await show(cb, text, kb([[btn("✅ Подтвердить", f"wdc:{item}")], [btn("❌ Отмена", "inv")]]))
    await cb.answer()


@r.callback_query(F.data.startswith("wdc:"))
async def cb_wd_confirm(cb: CallbackQuery):
    item = cb.data.split(":")[1]
    if item not in ITEMS:
        await cb.answer("Ошибка", show_alert=True)
        return
    uid = cb.from_user.id
    if not await can_withdraw(uid):
        await cb.answer(f"Вывод откроется после пополнений от {MIN_DEPOSIT_TO_WITHDRAW} ⭐", show_alert=True)
        return
    wid = None
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id",
                uid, item,
            )
            if row:
                wid = await con.fetchval(
                    "INSERT INTO withdrawals(user_id, kind, item, amount, details) "
                    "VALUES($1,'item',$2,$3,$4) RETURNING id",
                    uid, item, ITEMS[item]["value"], f"@{cb.from_user.username}" if cb.from_user.username else "",
                )
    if wid is None:
        await cb.answer("Предмета уже нет", show_alert=True)
        await render_inventory(cb)
        return
    it = ITEMS[item]
    await show(cb, f"✅ Заявка #{wid} на вывод {it['emoji']} {it['name']} создана. Ожидайте обработки.", kb([BACK_ROW]))
    await cb.answer()
    await notify_admins(cb.bot, wid, cb.from_user, f"Тип: подарок\n{it['emoji']} {it['name']} ({it['value']} ⭐)")


@r.callback_query(F.data.startswith("wda:"))
async def cb_wd_approve(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    wid = int(cb.data.split(":")[1])
    row = await pool.fetchrow(
        "UPDATE withdrawals SET status='done', processed_at=now() WHERE id=$1 AND status='pending' RETURNING *", wid
    )
    if not row:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return
    try:
        await cb.message.edit_text((cb.message.html_text or "") + "\n\n✅ <b>Выполнено</b>")
    except Exception:
        pass
    try:
        await cb.bot.send_message(row["user_id"], f"✅ Ваша заявка на вывод #{wid} выполнена!")
    except Exception:
        pass
    await cb.answer("Готово")


@r.callback_query(F.data.startswith("wdr:"))
async def cb_wd_reject(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    wid = int(cb.data.split(":")[1])
    row = None
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "UPDATE withdrawals SET status='rejected', processed_at=now() WHERE id=$1 AND status='pending' RETURNING *", wid
            )
            if row:
                if row["kind"] == "stars":
                    await con.execute(
                        "UPDATE users SET balance = balance + $2 WHERE id=$1", row["user_id"], row["amount"]
                    )
                elif row["item"] in LEGACY_NFT:
                    await con.execute(
                        "UPDATE users SET balance = balance + $2 WHERE id=$1", row["user_id"], LEGACY_NFT[row["item"]]
                    )
                elif row["item"]:
                    await con.execute(
                        "INSERT INTO inventory(user_id, item) VALUES($1,$2)", row["user_id"], row["item"]
                    )
    if not row:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return
    try:
        await cb.message.edit_text((cb.message.html_text or "") + "\n\n❌ <b>Отклонено (возврат выполнен)</b>")
    except Exception:
        pass
    try:
        await cb.bot.send_message(row["user_id"], f"❌ Заявка на вывод #{wid} отклонена. Средства/предмет возвращены.")
    except Exception:
        pass
    await cb.answer("Отклонено")


# ───────────────────────── АДМИН-КОМАНДЫ ─────────────────────────
def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMIN_IDS


@r.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m):
        return
    await m.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "/stats — статистика\n"
        "/pending — необработанные заявки\n"
        "/give &lt;id&gt; &lt;сумма&gt; — начислить звёзды\n"
        "/take &lt;id&gt; &lt;сумма&gt; — списать звёзды\n"
        "/broadcast &lt;текст&gt; — рассылка\n"
        "/backup — выгрузка всех данных в CSV"
    )


@r.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m):
        return
    users = await pool.fetchval("SELECT count(*) FROM users")
    bal = await pool.fetchval("SELECT COALESCE(sum(balance),0) FROM users")
    dep = await pool.fetchval("SELECT COALESCE(sum(amount),0) FROM payments")
    pend = await pool.fetchval("SELECT count(*) FROM withdrawals WHERE status='pending'")
    await m.answer(
        f"📊 <b>Статистика</b>\n\nПользователей: {users}\nСумма балансов: {bal} ⭐\n"
        f"Всего пополнено: {dep} ⭐\nОжидают вывода: {pend}"
    )


@r.message(Command("pending"))
async def cmd_pending(m: Message):
    if not is_admin(m):
        return
    rows = await pool.fetch("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 20")
    if not rows:
        await m.answer("Нет необработанных заявок.")
        return
    for w in rows:
        what = f"{w['amount']} ⭐" if w["kind"] == "stars" else (
            f"{ITEMS[w['item']]['emoji']} {ITEMS[w['item']]['name']}" if w["item"] in ITEMS else str(w["item"])
        )
        await m.answer(
            f"Заявка #{w['id']} · user <code>{w['user_id']}</code>\n{w['kind']}: {what}\n{w['details'] or ''}",
            reply_markup=kb([[btn("✅ Выполнено", f"wda:{w['id']}"), btn("❌ Отклонить", f"wdr:{w['id']}")]]),
        )


# ---------- пополнение NFT-подарками (ручное подтверждение админом) ----------
async def notify_gift_admins(bot: Bot, gid: int, user, name: str, link: str, claimed: int):
    from html import escape as esc
    uname = f"@{esc(user.username)}" if user.username else "без username"
    msg = (f"🎁 <b>Пополнение подарком #{gid}</b>\nИгрок: {uname} (<code>{user.id}</code>)\n"
           f"Подарок: {esc(name)}\nСсылка: {esc(link) if link else '—'}\n"
           f"Оценка игрока: {claimed} ⭐\n\nПроверьте, что подарок пришёл на аккаунт. "
           f"Своя сумма: <code>/gok {gid} сумма</code>")
    row = []
    if claimed > 0:
        row.append(btn(f"✅ Зачислить {claimed} ⭐", f"gda:{gid}"))
    row.append(btn("❌ Отклонить", f"gdr:{gid}"))
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, msg, reply_markup=kb([row]))
        except Exception:
            log.exception("Не удалось уведомить админа %s", aid)


async def gift_approve(bot: Bot, gid: int, amount: int):
    if amount <= 0:
        return None
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "UPDATE gift_deposits SET status='done', amount=$2, processed_at=now() "
                "WHERE id=$1 AND status='pending' RETURNING *", gid, amount)
            if row:
                await con.execute(
                    "UPDATE users SET balance = balance + $2, total_dep = total_dep + $2 WHERE id=$1",
                    row["user_id"], amount)
    if row:
        try:
            await bot.send_message(row["user_id"], f"✅ Подарок принят! На баланс зачислено {amount} ⭐")
        except Exception:
            pass
    return row


@r.callback_query(F.data.startswith("gda:"))
async def cb_gift_ok(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    gid = int(cb.data.split(":")[1])
    claimed = await pool.fetchval("SELECT claimed FROM gift_deposits WHERE id=$1", gid) or 0
    row = await gift_approve(cb.bot, gid, claimed)
    if not row:
        await cb.answer("Уже обработано", show_alert=True)
        return
    try:
        await cb.message.edit_text((cb.message.html_text or "") + f"\n\n✅ <b>Зачислено {claimed} ⭐</b>")
    except Exception:
        pass
    await cb.answer("Готово")


@r.callback_query(F.data.startswith("gdr:"))
async def cb_gift_no(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    gid = int(cb.data.split(":")[1])
    row = await pool.fetchrow(
        "UPDATE gift_deposits SET status='rejected', processed_at=now() WHERE id=$1 AND status='pending' RETURNING *", gid)
    if not row:
        await cb.answer("Уже обработано", show_alert=True)
        return
    try:
        await cb.message.edit_text((cb.message.html_text or "") + "\n\n❌ <b>Отклонено</b>")
    except Exception:
        pass
    try:
        await cb.bot.send_message(row["user_id"], f"❌ Заявка на пополнение подарком #{gid} отклонена: подарок не найден.")
    except Exception:
        pass
    await cb.answer("Отклонено")


@r.message(Command("gok"))
async def cmd_gift_ok(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    p = (command.args or "").split()
    if len(p) != 2 or not p[0].isdigit() or not p[1].isdigit():
        await m.answer("Формат: /gok <номер заявки> <сумма ⭐>")
        return
    row = await gift_approve(m.bot, int(p[0]), int(p[1]))
    await m.answer(f"✅ Зачислено {p[1]} ⭐" if row else "Заявка не найдена или уже обработана.")


@r.message(Command("give"))
async def cmd_give(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await m.answer("Формат: /give <id> <сумма>")
        return
    bal = await pool.fetchval(
        "UPDATE users SET balance = balance + $2 WHERE id=$1 RETURNING balance", int(parts[0]), int(parts[1])
    )
    await m.answer("Пользователь не найден." if bal is None else f"✅ Готово. Новый баланс: {bal} ⭐")


@r.message(Command("take"))
async def cmd_take(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await m.answer("Формат: /take <id> <сумма>")
        return
    bal = await pool.fetchval(
        "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING balance",
        int(parts[0]), int(parts[1]),
    )
    await m.answer("Пользователь не найден или недостаточно средств." if bal is None else f"✅ Готово. Новый баланс: {bal} ⭐")


@r.message(Command("backup"))
async def cmd_backup(m: Message):
    if not is_admin(m):
        return
    for table in ("users", "inventory", "withdrawals", "payments", "crypto_invoices", "achievements"):
        rows = await pool.fetch(f"SELECT * FROM {table}")
        if not rows:
            continue
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(list(rows[0].keys()))
        for row in rows:
            w.writerow(list(row.values()))
        await m.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8"), filename=f"{table}.csv"))
    await m.answer("✅ Резервная копия отправлена.")


@r.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    text = (command.args or "").strip()
    if not text:
        await m.answer("Формат: /broadcast <текст>")
        return
    ids = [x["id"] for x in await pool.fetch("SELECT id FROM users")]
    ok = 0
    for uid in ids:
        try:
            await m.bot.send_message(uid, text, parse_mode=None)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📣 Отправлено: {ok}/{len(ids)}")


# ───────────────────────── ФОЛБЭК ─────────────────────────
@r.message()
async def fallback(m: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    await m.answer(await menu_text(m.from_user.id), reply_markup=main_kb())


@r.error()
async def on_error(event: ErrorEvent):
    log.error("Необработанная ошибка: %s", event.exception, exc_info=event.exception)
    return True


# ───────────────────────── ЗАПУСК ─────────────────────────
async def health(request):
    return web.Response(text="ok")


async def keep_alive():
    if not EXTERNAL_URL:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(EXTERNAL_URL, timeout=aiohttp.ClientTimeout(total=20)):
                    pass
            except Exception as e:
                log.warning("keep_alive: %s", e)


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN")
    if not DATABASE_URL:
        raise SystemExit("Не задана переменная DATABASE_URL")

    for cid, c in CASES.items():
        log.info("Кейс %s (%s): RTP %.1f%%", cid, c["name"], case_rtp(c))

    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("Бот @%s запущен", BOT_USERNAME)

    app = web.Application()
    import webapp
    webapp.setup(app, bot, sys.modules[__name__])
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    if EXTERNAL_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=EXTERNAL_URL)))
        except Exception:
            log.exception("set_chat_menu_button failed")

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserMW())
    dp.include_router(r)

    keep_task = asyncio.create_task(keep_alive())
    db_task = asyncio.create_task(db_keepalive())
    crypto_task = asyncio.create_task(crypto_poller(bot)) if CRYPTO_TOKEN else None
    if CRYPTO_TOKEN:
        log.info("Крипто-оплата включена: %s, 1 %s = %s ⭐", CRYPTO_API, CRYPTO_ASSET, CRYPTO_STARS_PER_USD)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        keep_task.cancel()
        db_task.cancel()
        if crypto_task:
            crypto_task.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
