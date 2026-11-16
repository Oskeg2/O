"""API для мини-апп Oskeg Case. Подключается из bot.py: webapp.setup(app, bot, модуль_bot)."""
import asyncio
import hashlib
import hmac
import json
import os
import math
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

CRAFT_RTP = float(os.getenv("CRAFT_RTP", "0.9"))  # средняя отдача крафта
CRAFT_MIN, CRAFT_MAX = 2, 6
NFT_PAY_X = float(os.getenv("NFT_PAY_X", "2"))   # ставка подарком: с этого икса выплата подарком, ниже — звёздами

import fair as F
from aiogram.types import LabeledPrice
from aiohttp import web

INDEX = Path(__file__).with_name("index.html")
GIFTS = Path(__file__).with_name("gifts")      # картинки подарков: gifts/<ключ>.png
CRASH_RATE = 0.12          # множитель ракеты = e^(0.12 * секунды)
BET_TIME, PAUSE = 8, 4     # секунд на ставки / пауза после взрыва
CRASH_RTP = 0.92
CHICK = {"easy": 0.12, "mid": 0.22, "hard": 0.35}   # шанс гибели на каждом шаге
CHICK_MAX = 15
BET_MIN, BET_MAX = 10, None   # максимума ставки нет — ограничивает только баланс
COIN_MULT = 1.94
DICE_RTP = 0.95                      # кости: множитель = 95 / шанс
DICE_MIN, DICE_MAX = 2, 95           # шанс выигрыша, %
# Колесо удачи: 12 равновероятных секторов, RTP каждого уровня = 94.2%
WHEEL = {
    "low":  [0.5, 1.5, 0.5, 1.2, 0.5, 1.5, 0.5, 1.2, 0.2, 2.0, 0.5, 1.2],
    "mid":  [0, 2, 0, 1.5, 0, 3, 0, 1.5, 0.3, 2.0, 0.5, 0.5],
    "high": [0, 0, 0, 0, 0, 0.3, 0, 0, 2.0, 0, 0, 9.0],
}



# Плинко: 10 рядов, 11 корзин, вероятности биномиальные. Множители нормируются под RTP.
PLINKO_ROWS, PLINKO_RTP = 10, 0.85
_PL_RAW = {
    "low":  [5.6, 2.1, 1.1, 0.9, 0.5, 0.4, 0.5, 0.9, 1.1, 2.1, 5.6],
    "mid":  [20, 5, 2, 0.9, 0.4, 0.2, 0.4, 0.9, 2, 5, 20],
    "high": [110, 18, 4, 0.8, 0.2, 0.1, 0.2, 0.8, 4, 18, 110],
}
_PL_P = [math.comb(PLINKO_ROWS, i) / 2 ** PLINKO_ROWS for i in range(PLINKO_ROWS + 1)]
PLINKO = {k: [round(PLINKO_RTP * x / sum(p * y for p, y in zip(_PL_P, v)), 2) for x in v] for k, v in _PL_RAW.items()}

# Башня: уровни, на каждом уровне N плиток и одна бомба. Отдача 80%.
TOWER = {"easy": 4, "mid": 3, "hard": 2}       # плиток в ряду (безопасных N-1)
TOWER_MAX, TOWER_RTP = 8, 0.80


class Err(Exception):
    pass


def setup(app: web.Application, bot, B):
    ready = {"ok": False}

    async def ensure():
        if ready["ok"]:
            return
        await B.pool.execute(
            "CREATE TABLE IF NOT EXISTS crash_bets(user_id BIGINT PRIMARY KEY, bet BIGINT NOT NULL)")
        await B.pool.execute(
            "CREATE TABLE IF NOT EXISTS chick_games(user_id BIGINT PRIMARY KEY, bet BIGINT NOT NULL, "
            "diff TEXT NOT NULL, steps INT NOT NULL DEFAULT 0)")
        await B.pool.execute(
            "CREATE TABLE IF NOT EXISTS tower_games(user_id BIGINT PRIMARY KEY, bet BIGINT NOT NULL, "
            "diff TEXT NOT NULL, lvl INT NOT NULL DEFAULT 0)")
        for t in ("crash_bets", "chick_games", "tower_games"):
            await B.pool.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS nft BOOLEAN NOT NULL DEFAULT false")
        await B.pool.execute(F.DDL)
        ready["ok"] = True

    tmult = lambda d, k: TOWER_RTP / ((TOWER[d] - 1) / TOWER[d]) ** k
    cmult = lambda d, k: B.MINES_RTP / (1 - CHICK[d]) ** k

    cap = lambda x: min(x, B.MAX_PAYOUT) if B.MAX_PAYOUT > 0 else x

    def check_bet(bet):
        if bet < BET_MIN:
            raise Err(f"Минимальная ставка {BET_MIN} ⭐")

    async def take(con, uid, amt):
        ok = await con.fetchval(
            "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1", uid, amt)
        if not ok:
            raise Err("Недостаточно звёзд. Пополните баланс.")

    async def give(con, uid, amt):
        await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, amt)

    async def pay_out(con, uid, pay, nft, bet):
        """Ставка подарком: при иксе ≥ NFT_PAY_X выплата подарком (самый дорогой ≤ выплаты, остаток звёздами), иначе звёздами."""
        k = None
        if nft and pay > 0 and pay >= bet * NFT_PAY_X:
            c = [(v["value"], x) for x, v in B.ITEMS.items() if v["value"] <= pay]
            if c:
                val, k = max(c)
                await con.execute("INSERT INTO inventory(user_id, item) VALUES($1,$2)", uid, k)
                pay -= val
        if pay > 0:
            await give(con, uid, pay)
        return k

    async def stake(con, uid, b):
        """Ставка звёздами или подарком со склада (ценность подарка = ставка). Возвращает сумму ставки."""
        item = b.get("item")
        if item:
            if item not in B.ITEMS:
                raise Err("Ошибка")
            bet = B.ITEMS[item]["value"]
            check_bet(bet)
            if not await con.fetchrow(
                    "DELETE FROM inventory WHERE id = (SELECT id FROM inventory WHERE user_id=$1 AND item=$2 "
                    "ORDER BY id LIMIT 1) RETURNING id", uid, item):
                raise Err("Подарка уже нет")
            return bet, True
        try:
            bet = int(b.get("bet", 0))
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная ставка")
        check_bet(bet)
        await take(con, uid, bet)
        return bet, False

    secret = hmac.new(b"WebAppData", B.BOT_TOKEN.encode(), hashlib.sha256).digest()

    def parse_user(request):
        d = dict(parse_qsl(request.headers.get("X-Init", ""), keep_blank_values=True))
        got = d.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            raise web.HTTPUnauthorized(text="bad auth")
        if time.time() - int(d.get("auth_date", "0")) > 86400 * 2:
            raise web.HTTPUnauthorized(text="expired")
        return json.loads(d["user"])

    async def snapshot(uid):
        """Всё состояние игрока ОДНИМ запросом к базе (раньше было ~15 последовательных — на Neon это секунды)."""
        r = await B.pool.fetchrow(
            "SELECT u.*, "
            "(SELECT count(*) FROM users x WHERE x.ref_by = u.id) AS refs, "
            "GREATEST(0, extract(epoch from (u.last_bonus + interval '24 hours' - now())))::int AS bonus_left, "
            "GREATEST(0, extract(epoch from (u.last_free_case + make_interval(hours => $2) - now())))::int AS free_left, "
            "COALESCE((SELECT json_object_agg(item, n) FROM (SELECT item, count(*) AS n FROM inventory "
            "          WHERE user_id = u.id GROUP BY item) i), '{}'::json) AS inv, "
            "(SELECT row_to_json(m) FROM mines_games m WHERE m.user_id = u.id) AS mines, "
            "(SELECT row_to_json(c) FROM chick_games c WHERE c.user_id = u.id) AS chick, "
            "(SELECT row_to_json(t) FROM tower_games t WHERE t.user_id = u.id) AS tower, "
            "ARRAY(SELECT key FROM achievements WHERE user_id = u.id) AS ach, "
            "(SELECT row_to_json(f) FROM fair f WHERE f.user_id = u.id) AS fair "
            "FROM users u WHERE u.id = $1", uid, B.FREE_CASE_COOLDOWN_H)
        if r is None:
            raise Err("Пользователь не найден")
        inv = {k: n for k, n in json.loads(r["inv"]).items() if k in B.ITEMS}
        g = json.loads(r["mines"]) if r["mines"] else None
        mines = None
        if g:
            op = B.parse_ints(g["opened"])
            mines = {"bet": g["bet"], "m": g["mines"], "opened": op,
                     "payout": B.mines_payout(g["bet"], g["mines"], len(op)) if op else 0}
        ck = json.loads(r["chick"]) if r["chick"] else None
        chick = {"bet": ck["bet"], "d": ck["diff"], "steps": ck["steps"]} if ck else None
        tw = json.loads(r["tower"]) if r["tower"] else None
        tower = {"bet": tw["bet"], "d": tw["diff"], "lvl": tw["lvl"]} if tw else None
        missing = B.ach_missing(r, set(r["ach"]))
        new = await B.ach_grant(uid, missing) if missing else []
        ach = list(r["ach"]) + missing
        keys = ("balance", "total_dep", "cases_opened", "rare_drops", "mines_wins", "best_mines", "up_wins", "best_up")
        return {"id": uid, "u": {k: r[k] for k in keys}, "inv": inv, "mines": mines, "chick": chick, "tower": tower, "ach": ach,
                "ach_new": new, "refs": r["refs"], "bonus_left": r["bonus_left"] or 0, "free_left": r["free_left"] or 0,
                "fair": F.state_from_row(json.loads(r["fair"]) if r["fair"] else None)}

    _imgs = []

    def imgs():
        if not _imgs and GIFTS.is_dir():
            _imgs.extend(sorted(p.stem for p in GIFTS.glob("*.png")))
        return _imgs

    def cfg():
        cases = {}
        for cid, c in B.CASES.items():
            tot = sum(w for _, w in c["drops"])
            cases[cid] = {"name": c["name"], "price": c["price"],
                          "icon": c.get("icon"),
                          "rtp": round(B.case_rtp(c), 1),
                          "drops": [[k, w / tot * 100, B.drop_value(k)] for k, w in c["drops"]]}
        return {
            "cases": cases, "items": B.ITEMS, "bets": B.BET_PRESETS, "mines_opts": B.MINES_OPTIONS,
            "cells": B.CELLS, "mines_rtp": B.MINES_RTP, "ups": B.UP_MULTS, "up_rtp": B.UP_RTP,
            "dep_presets": B.DEPOSIT_PRESETS, "min_dep": B.MIN_DEPOSIT, "max_dep": B.MAX_DEPOSIT,
            "min_wd": B.MIN_WITHDRAW, "min_dep_wd": B.MIN_DEPOSIT_TO_WITHDRAW, "ref_pct": B.REF_PERCENT,
            "bot": B.BOT_USERNAME or "Oskegcase",
            "ach": [[k, t, d] for k, (t, d, _) in B.ACHIEVEMENTS.items()],
            "imgs": imgs(),
            "mines_min": B.MINES_MIN_BET,
            "crypto": {"on": bool(B.CRYPTO_TOKEN), "rate": B.CRYPTO_STARS_PER_USD, "asset": B.CRYPTO_ASSET,
                       "min": B.CRYPTO_MIN_DEPOSIT, "max": B.CRYPTO_MAX_DEPOSIT},
            "bet_min": BET_MIN, "bet_max": BET_MAX, "crash_rate": CRASH_RATE, "coin_mult": COIN_MULT,
            "wheel": WHEEL, "dice": {"rtp": DICE_RTP, "min": DICE_MIN, "max": DICE_MAX},
            "chick": {"d": CHICK, "max": CHICK_MAX, "rtp": B.MINES_RTP},
            "plinko": {"rows": PLINKO_ROWS, "m": PLINKO, "rtp": PLINKO_RTP},
            "tower": {"d": TOWER, "max": TOWER_MAX, "rtp": TOWER_RTP},
            "gift_to": B.GIFT_DEPOSIT_TO, "nft_x": NFT_PAY_X,
            "tasks": {"on": B.TASKS_ON, "reward": B.TASK_REWARD},
            "craft": {"min": CRAFT_MIN, "max": CRAFT_MAX, "rtp": CRAFT_RTP},
        }

    def api(fn, light=False):
        async def handler(request):
            try:
                u = parse_user(request)
                body = await request.json() if request.can_read_body else {}
                await ensure()
                if light:
                    return web.json_response(await fn(u, body) or {})
                await B.touch_user(SimpleNamespace(id=u["id"], username=u.get("username")))
                out = await fn(u, body) or {}
                out["me"] = await snapshot(u["id"])
                return web.json_response(out)
            except Err as e:
                return web.json_response({"error": str(e)}, status=400)
            except web.HTTPException:
                raise
            except Exception:
                B.log.exception("api error")
                return web.json_response({"error": "Ошибка сервера"}, status=500)
        return handler

    def route(path, light=False):
        def deco(fn):
            app.router.add_post("/api/" + path, api(fn, light))
            return fn
        return deco

    @route("me")
    async def me(u, b):
        return {"cfg": cfg()}

    feed_gc = {"t": 0.0}

    @route("feed", light=True)
    async def feed(u, b):
        """Live-лента: последние дропы игроков. Отдаются только уже «созревшие» (visible_at <= now),
        поэтому свой выигрыш нельзя увидеть в ленте до конца анимации."""
        if time.time() - feed_gc["t"] > 600:
            feed_gc["t"] = time.time()
            await B.pool.execute("DELETE FROM live_drops WHERE created_at < now() - interval '2 days'")
        rows = await B.pool.fetch(
            "SELECT d.id, d.user_id, d.case_id, d.item, u.username FROM live_drops d "
            "LEFT JOIN users u ON u.id = d.user_id "
            "WHERE d.visible_at <= now() ORDER BY d.id DESC LIMIT 25")
        out = []
        for r in rows:
            if r["item"] not in B.ITEMS:
                continue
            nm = (r["username"] or "").strip()
            name = (nm[:2] + "•••") if len(nm) > 2 else "ID" + str(r["user_id"])[-3:]
            cname = B.CASES[r["case_id"]]["name"] if r["case_id"] in B.CASES else ""
            out.append({"id": r["id"], "u": name, "me": r["user_id"] == u["id"], "k": "i:" + r["item"], "c": cname})
        return {"feed": out}

    @route("feed/reveal", light=True)
    async def feed_reveal(u, b):
        """Игрок закончил анимацию и нажал «Забрать» — его дропы сразу появляются в ленте."""
        await B.pool.execute("UPDATE live_drops SET visible_at = now() WHERE user_id=$1 AND visible_at > now()", u["id"])
        return {}

    @route("fair/rotate")
    async def fair_rotate(u, b):
        async with B.pool.acquire() as con:
            async with con.transaction():
                if not await F.rotate(con, u["id"], (str(b.get("client") or "oskeg").strip() or "oskeg")[:32]):
                    raise Err("Сначала завершите текущую игру (Сапёр/Курочка/Башня)")
        return {}

    @route("tasks")
    async def tasks(u, b):
        """Задания BotoHub: показать список и засчитать выполненные (+TASK_REWARD ⭐ за каждое)."""
        if not B.TASKS_ON:
            return {"tasks": {"on": False, "list": [], "new": 0, "gained": 0, "frac": 0, "done": 0, "reward": B.TASK_REWARD}}
        st = await B.tasks_sync(u["id"])
        return {"tasks": {"on": True, "ok": st["ok"], "list": [{"n": t["name"], "url": t["url"], "paid": t["paid"]} for t in st["tasks"]],
                          "new": st["new"], "gained": st["gained_cents"] / 100, "frac": st["frac_cents"] / 100,
                          "done": st["done_total"], "reward": B.TASK_REWARD}}

    @route("bonus")
    async def bonus(u, b):
        amount = secrets.randbelow(5) + 1
        got = await B.pool.fetchval(
            "UPDATE users SET balance = balance + $2, last_bonus = now() "
            "WHERE id=$1 AND (last_bonus IS NULL OR last_bonus < now() - interval '24 hours') RETURNING 1",
            u["id"], amount)
        if not got:
            raise Err("Бонус ещё не доступен")
        return {"bonus": amount}

    @route("case")
    async def case(u, b):
        cid, n = b.get("cid"), int(b.get("n", 1))
        if cid not in B.CASES or n not in (1, 2, 3, 4, 5, 10):
            raise Err("Ошибка")
        res = await B.open_cases(u["id"], cid, n)
        if not res:
            raise Err("Недостаточно звёзд. Пополните баланс.")
        return {"drops": res}

    @route("case/free")
    async def case_free(u, b):
        """Ежедневный бесплатный кейс: 0 ⭐ — 60%, 1 ⭐ — 20%, 5 ⭐ — 10%, 10 ⭐ — 10% (см. FREE_CASE_DROPS в bot.py)."""
        bad = await B.missing_subs(bot, u["id"])
        if bad:
            return {"need_sub": [{"n": c["name"], "url": c["url"]} for c in bad]}
        key = await B.open_free_case(u["id"])
        if key is None:
            raise Err("Бесплатный кейс уже открыт. Приходите позже")
        return {"free": key}

    @route("sell")
    async def sell(u, b):
        item = b.get("item")
        if item not in B.ITEMS:
            raise Err("Ошибка")
        n = await B.sell_items(u["id"], item, bool(b.get("all")))
        if not n:
            raise Err("Предмета уже нет")
        return {"sold": n, "stars": n * B.ITEMS[item]["value"]}

    @route("mines/start")
    async def mines_start(u, b):
        try:
            bet, m = int(b.get("bet", 0)), int(b.get("m", 0))
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная ставка")
        if m not in B.MINES_OPTIONS:
            raise Err("Ошибка")
        if bet < B.MINES_MIN_BET:
            raise Err(f"Минимальная ставка {B.MINES_MIN_BET} ⭐")
        async with B.pool.acquire() as con:
            async with con.transaction():
                if await con.fetchval("SELECT 1 FROM mines_games WHERE user_id=$1", u["id"]):
                    return {}
                ok = await con.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                    u["id"], bet)
                if not ok:
                    raise Err("Недостаточно звёзд. Пополните баланс.")
                cells = (await F.rng(con, u["id"])).sample(B.CELLS, m)
                await con.execute(
                    "INSERT INTO mines_games(user_id, bet, mines, mine_cells, opened) VALUES($1,$2,$3,$4,'')",
                    u["id"], bet, m, B.join_ints(cells))
        return {}

    @route("mines/click")
    async def mines_click(u, b):
        i = int(b.get("i", -1))
        if not 0 <= i < B.CELLS:
            raise Err("Ошибка")
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM mines_games WHERE user_id=$1 FOR UPDATE", uid)
                if not g:
                    raise Err("Игра не найдена")
                mines, opened = B.parse_ints(g["mine_cells"]), B.parse_ints(g["opened"])
                bet, m = int(g["bet"]), int(g["mines"])
                if i in opened:
                    return {"status": "dup"}
                if i in mines:
                    await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                    return {"status": "boom", "mines": mines, "hit": i, "opened": opened, "bet": bet}
                opened.append(i)
                pay = B.mines_payout(bet, m, len(opened))
                if len(opened) == B.CELLS - m or B.mines_capped(pay):
                    await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                    await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, pay)
                    await B.record_mines_win(con, uid, bet, pay)
                    return {"status": "win", "mines": mines, "opened": opened, "payout": pay, "bet": bet}
                await con.execute("UPDATE mines_games SET opened=$2 WHERE user_id=$1", uid, B.join_ints(opened))
                return {"status": "ok", "i": i}

    @route("mines/cash")
    async def mines_cash(u, b):
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM mines_games WHERE user_id=$1 FOR UPDATE", uid)
                opened = B.parse_ints(g["opened"]) if g else []
                if not opened:
                    raise Err("Сначала откройте хотя бы одну ячейку")
                bet, m = int(g["bet"]), int(g["mines"])
                pay = B.mines_payout(bet, m, len(opened))
                await con.execute("DELETE FROM mines_games WHERE user_id=$1", uid)
                await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, pay)
                await B.record_mines_win(con, uid, bet, pay)
                return {"status": "cash", "payout": pay, "bet": bet, "opened": opened,
                        "mines": B.parse_ints(g["mine_cells"])}

    @route("up")
    async def up(u, b):
        mode, ref, mk = b.get("mode"), str(b.get("ref")), str(b.get("mult"))
        if mk not in B.UP_MULTS or mode not in ("i", "b"):
            raise Err("Ошибка")
        if mode == "i" and ref not in B.ITEMS:
            raise Err("Ошибка")
        if mode == "b" and (not ref.isdigit() or int(ref) < BET_MIN):
            raise Err("Ошибка")
        uid, mult = u["id"], B.UP_MULTS[mk]
        chance = B.UP_RTP / mult
        async with B.pool.acquire() as con:
            async with con.transaction():
                if mode == "i":
                    row = await con.fetchrow(
                        "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                        "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id", uid, ref)
                    stake = B.ITEMS[ref]["value"] if row else None
                else:
                    ok = await con.fetchval(
                        "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1",
                        uid, int(ref))
                    stake = int(ref) if ok else None
                if stake is None:
                    raise Err("Недостаточно звёзд или предмета нет")
                win = (await F.rng(con, uid)).randbelow(10000) < int(chance * 10000)
                pay = int(stake * mult) if win else 0
                if pay:
                    await con.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, pay)
                    await con.execute(
                        "UPDATE users SET up_wins = up_wins + 1, best_up = GREATEST(best_up, $2) WHERE id=$1",
                        uid, float(mult))
        return {"win": win, "stake": stake, "payout": pay, "chance": chance}

    @route("up/item")
    async def up_item(u, b):
        mode, ref, dst, uid = b.get("mode"), str(b.get("ref")), b.get("dst"), u["id"]
        if dst not in B.ITEMS or mode not in ("b", "i"):
            raise Err("Ошибка")
        vd = B.ITEMS[dst]["value"]
        if mode == "i":
            if ref not in B.ITEMS:
                raise Err("Ошибка")
            stake = B.ITEMS[ref]["value"]
        else:
            if not ref.isdigit():
                raise Err("Ошибка")
            stake = int(ref)
            check_bet(stake)
        if vd <= stake:
            raise Err("Выберите более ценный подарок")
        chance = B.UP_RTP * stake / vd
        if chance < 0.01:
            raise Err("Слишком большая разница в цене")
        async with B.pool.acquire() as con:
            async with con.transaction():
                if mode == "i":
                    row = await con.fetchrow(
                        "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                        "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id", uid, ref)
                    if not row:
                        raise Err("Подарка уже нет")
                else:
                    await take(con, uid, stake)
                win = (await F.rng(con, uid)).randbelow(10000) < int(chance * 10000)
                if win:
                    await con.execute("INSERT INTO inventory(user_id, item) VALUES($1,$2)", uid, dst)
                    await con.execute(
                        "UPDATE users SET up_wins = up_wins + 1, best_up = GREATEST(best_up, $2) WHERE id=$1",
                        uid, float(vd / stake))
        return {"win": win, "chance": chance, "stake": stake, "dst": dst}

    @route("craft")
    async def craft(u, b):
        keys = b.get("items")
        if (not isinstance(keys, list) or not CRAFT_MIN <= len(keys) <= CRAFT_MAX
                or any(not isinstance(k, str) or k not in B.ITEMS for k in keys)):
            raise Err(f"Нужно от {CRAFT_MIN} до {CRAFT_MAX} подарков")
        total = sum(B.ITEMS[k]["value"] for k in keys)
        async with B.pool.acquire() as con:
            rg = await F.rng(con, u["id"])
        mult = 0.5 + 1.5 * (rg.randbelow(10001) / 10000) ** 2   # x0.5..x2.0, среднее x1.0
        target = total * CRAFT_RTP * mult
        pool = [k for k in B.ITEMS if B.ITEMS[k]["value"] <= total * CRAFT_RTP * 2]
        if not pool:
            pool = [min(keys, key=lambda k: B.ITEMS[k]["value"])]
        near = sorted(pool, key=lambda k: abs(B.ITEMS[k]["value"] - target))[:3]
        dst = rg.choice(near)
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                for k in keys:
                    row = await con.fetchrow(
                        "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                        "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id", uid, k)
                    if not row:
                        raise Err("Подарка уже нет")
                await con.execute("INSERT INTO inventory(user_id, item) VALUES($1,$2)", uid, dst)
        return {"dst": dst, "value": B.ITEMS[dst]["value"], "src": total}

    @route("deposit/gift")
    async def deposit_gift(u, b):
        name = str(b.get("name", "")).strip()[:100]
        link = str(b.get("link", "")).strip()[:200]
        try:
            val = max(0, min(int(b.get("value") or 0), 10_000_000))
        except (TypeError, ValueError):
            val = 0
        if len(name) < 2:
            raise Err("Укажите название подарка")
        uid = u["id"]
        async with B.pool.acquire() as con:
            if await con.fetchval("SELECT count(*) FROM gift_deposits WHERE user_id=$1 AND status='pending'", uid) >= 5:
                raise Err("Слишком много необработанных заявок")
            gid = await con.fetchval(
                "INSERT INTO gift_deposits(user_id, name, link, claimed) VALUES($1,$2,$3,$4) RETURNING id",
                uid, name, link, val)
        await B.notify_gift_admins(bot, gid, SimpleNamespace(id=uid, username=u.get("username")), name, link, val)
        return {"gid": gid}

    @route("deposit")
    async def deposit(u, b):
        amt = int(b.get("amount", 0))
        if not B.MIN_DEPOSIT <= amt <= B.MAX_DEPOSIT:
            raise Err(f"Сумма от {B.MIN_DEPOSIT} до {B.MAX_DEPOSIT} ⭐")
        link = await bot.create_invoice_link(
            title="Пополнение баланса", description=f"Пополнение баланса на {amt} ⭐",
            payload=f"dep:{u['id']}:{amt}", currency="XTR", prices=[LabeledPrice(label="Пополнение", amount=amt)])
        return {"link": link}

    @route("deposit/crypto")
    async def deposit_crypto(u, b):
        try:
            amt = int(b.get("amount", 0))
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная сумма")
        try:
            inv = await B.crypto_create_invoice(u["id"], amt)
        except B.CryptoError as e:
            raise Err(str(e))
        if not inv.get("link"):
            raise Err("Не удалось получить ссылку на оплату")
        return {"link": inv["link"], "amount": inv["amount"], "asset": inv["asset"]}

    @route("deposit/crypto/check")
    async def deposit_crypto_check(u, b):
        try:
            n = await B.crypto_sync(bot, u["id"])
        except Exception as e:
            B.log.warning("crypto check: %s", e)
            n = 0
        return {"credited": n}

    async def check_wd(uid):
        return None  # вывод доступен без пополнения, минимум — B.MIN_WITHDRAW

    @route("withdraw/stars")
    async def wd_stars(u, b):
        uid, amt, details = u["id"], int(b.get("amount", 0)), str(b.get("details", "")).strip()[:300]
        await check_wd(uid)
        if amt < B.MIN_WITHDRAW:
            raise Err(f"Минимум {B.MIN_WITHDRAW} ⭐")
        if not details:
            raise Err("Укажите реквизиты")
        wid = None
        async with B.pool.acquire() as con:
            async with con.transaction():
                ok = await con.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE id=$1 AND balance >= $2 RETURNING 1", uid, amt)
                if ok:
                    wid = await con.fetchval(
                        "INSERT INTO withdrawals(user_id, kind, amount, details) VALUES($1,'stars',$2,$3) RETURNING id",
                        uid, amt, details)
        if wid is None:
            raise Err("Недостаточно звёзд")
        su = SimpleNamespace(id=uid, username=u.get("username"))
        await B.notify_admins(bot, wid, su, f"Тип: звёзды\nСумма: {amt} ⭐\nРеквизиты: {details}")
        return {"wid": wid}

    @route("withdrawals")
    async def wd_list(u, b):
        rows = await B.pool.fetch(
            "SELECT id, kind, item, amount, status, "
            "extract(epoch from created_at)::bigint AS c, extract(epoch from processed_at)::bigint AS p "
            "FROM withdrawals WHERE user_id=$1 ORDER BY id DESC LIMIT 50", u["id"])
        out = []
        for r in rows:
            it = B.ITEMS.get(r["item"]) if r["item"] else None
            out.append({"id": r["id"], "kind": r["kind"], "amount": r["amount"], "status": r["status"],
                        "name": it["name"] if it else None, "emoji": it["emoji"] if it else "⭐",
                        "c": r["c"], "p": r["p"]})
        return {"list": out}

    @route("withdraw/item")
    async def wd_item(u, b):
        uid, item = u["id"], b.get("item")
        if item not in B.ITEMS:
            raise Err("Ошибка")
        await check_wd(uid)
        if not u.get("username"):
            raise Err("Установите username в настройках Telegram — подарок отправляется по нему")
        wid = None
        async with B.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    "DELETE FROM inventory WHERE id = (SELECT id FROM inventory "
                    "WHERE user_id=$1 AND item=$2 ORDER BY id LIMIT 1) RETURNING id", uid, item)
                if row:
                    wid = await con.fetchval(
                        "INSERT INTO withdrawals(user_id, kind, item, amount, details) "
                        "VALUES($1,'item',$2,$3,$4) RETURNING id",
                        uid, item, B.ITEMS[item]["value"], "@" + u["username"])
        if wid is None:
            raise Err("Предмета уже нет")
        it = B.ITEMS[item]
        su = SimpleNamespace(id=uid, username=u.get("username"))
        await B.notify_admins(bot, wid, su, f"Тип: подарок\n{it['emoji']} {it['name']} ({it['value']} ⭐)")
        return {"wid": wid}

    # ───────────── РАКЕТА: общие раунды (летит сама, игроки ставят) ─────────────
    R = {"id": 0, "phase": "bet", "t_end": 0.0, "t0": 0.0, "point": 1.0, "bets": {}, "hist": []}
    cur_m = lambda: math.exp(CRASH_RATE * (time.monotonic() - R["t0"]))

    async def pay_cash(uid, mult):
        b = R["bets"].get(uid)
        if not b or b["cash"] is not None:
            return None
        b["cash"] = mult                      # помечаем сразу, чтобы не выплатить дважды
        pay = cap(int(b["bet"] * mult))
        try:
            async with B.pool.acquire() as con:
                async with con.transaction():
                    await con.execute("DELETE FROM crash_bets WHERE user_id=$1", uid)
                    b["gift"] = await pay_out(con, uid, pay, b["nft"], b["bet"])
        except Exception:
            b["cash"] = None
            raise
        b["win"] = pay
        return pay

    async def crash_loop():
        while B.pool is None:
            await asyncio.sleep(1)
        await ensure()
        # после перезапуска сервера возвращаем ставки незавершённого раунда
        left = await B.pool.fetch("DELETE FROM crash_bets RETURNING user_id, bet")
        for r_ in left:
            await B.pool.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", r_["user_id"], r_["bet"])
        while True:
            try:
                seed = secrets.token_hex(32)
                x = F.Rng(seed, "crash", R["id"] + 1).randbelow(10 ** 9) / 1e9
                point = max(1.0, min(1000.0, math.floor(CRASH_RTP / (1 - x) * 100) / 100))
                R.update(id=R["id"] + 1, phase="bet", point=point, seed=seed, fh=F.sha(seed), bets={}, t_end=time.monotonic() + BET_TIME)
                await asyncio.sleep(BET_TIME)
                R["phase"], R["t0"] = "fly", time.monotonic()
                while cur_m() < R["point"]:
                    m = cur_m()
                    for uid, b in list(R["bets"].items()):
                        if b["cash"] is None and b["auto"] and m >= b["auto"]:
                            await pay_cash(uid, b["auto"])
                    await asyncio.sleep(0.05)
                R["phase"], R["t_end"] = "crash", time.monotonic() + PAUSE
                R["hist"] = (R["hist"] + [R["point"]])[-15:]
                await B.pool.execute("DELETE FROM crash_bets")
                await asyncio.sleep(PAUSE)
            except asyncio.CancelledError:
                raise
            except Exception:
                B.log.exception("crash loop")
                await asyncio.sleep(2)

    async def start_loop(a):
        a["crash_task"] = asyncio.create_task(crash_loop())

    async def stop_loop(a):
        a["crash_task"].cancel()

    app.on_startup.append(start_loop)
    app.on_cleanup.append(stop_loop)

    @route("crash/state", light=True)
    async def crash_state(u, b):
        now = time.monotonic()
        out = {"id": R["id"], "phase": R["phase"], "hist": R["hist"], "count": len(R["bets"]), "fh": R.get("fh")}
        if R["phase"] == "bet":
            out["left"] = max(0.0, R["t_end"] - now)
        elif R["phase"] == "fly":
            out["t"] = now - R["t0"]
        else:
            out["left"], out["point"], out["seed"] = max(0.0, R["t_end"] - now), R["point"], R["seed"]
        rows = sorted(R["bets"].values(), key=lambda v: -v["bet"])[:10]
        out["pl"] = [{"n": v["name"], "b": v["bet"], "c": v["cash"]} for v in rows]
        my = R["bets"].get(u["id"])
        out["my"] = {"bet": my["bet"], "cash": my["cash"], "auto": my["auto"], "win": my.get("win", 0), "gift": my.get("gift"), "nft": my["nft"]} if my else None
        return out

    @route("crash/bet")
    async def crash_bet(u, b):
        uid = u["id"]
        auto = float(b.get("auto") or 0)
        if auto and not 1.01 <= auto <= 1000:
            raise Err("Автовывод от x1.01 до x1000")
        if R["phase"] != "bet" or time.monotonic() >= R["t_end"]:
            raise Err("Ставки закрыты — дождитесь следующего раунда")
        if uid in R["bets"]:
            raise Err("Вы уже поставили в этом раунде")
        rid = R["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                bet, nft = await stake(con, uid, b)
                await con.execute("INSERT INTO crash_bets(user_id, bet, nft) VALUES($1,$2,$3) ON CONFLICT (user_id) DO NOTHING", uid, bet, nft)
        if R["id"] != rid or R["phase"] != "bet":     # раунд успел начаться — возвращаем
            await B.pool.execute("UPDATE users SET balance = balance + $2 WHERE id=$1", uid, bet)
            await B.pool.execute("DELETE FROM crash_bets WHERE user_id=$1", uid)
            raise Err("Ставки закрыты — дождитесь следующего раунда")
        name = (u.get("first_name") or u.get("username") or "Игрок")[:12]
        R["bets"][uid] = {"bet": bet, "name": name, "auto": auto, "cash": None, "nft": nft}
        return {}

    @route("crash/cash")
    async def crash_cash(u, b):
        uid = u["id"]
        my = R["bets"].get(uid)
        if R["phase"] != "fly" or not my or my["cash"] is not None:
            raise Err("Сейчас забрать нельзя")
        m = cur_m()
        if m >= R["point"]:
            raise Err("Ракета уже взорвалась")
        pay = await pay_cash(uid, m)
        if pay is None:
            raise Err("Уже забрано")
        return {"payout": pay, "mult": m, "gift": R["bets"][uid].get("gift")}

    # ───────────── КУРОЧКА ─────────────
    @route("chick/start")
    async def chick_start(u, b):
        d, uid = b.get("d"), u["id"]
        if d not in CHICK:
            raise Err("Ошибка")
        async with B.pool.acquire() as con:
            async with con.transaction():
                if await con.fetchval("SELECT 1 FROM chick_games WHERE user_id=$1", uid):
                    return {}
                bet, nft = await stake(con, uid, b)
                await con.execute("INSERT INTO chick_games(user_id, bet, diff, nft) VALUES($1,$2,$3,$4)", uid, bet, d, nft)
        return {}

    @route("chick/step")
    async def chick_step(u, b):
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM chick_games WHERE user_id=$1 FOR UPDATE", uid)
                if not g:
                    raise Err("Игра не найдена")
                d, bet, k = g["diff"], int(g["bet"]), int(g["steps"])
                if (await F.rng(con, uid)).randbelow(10000) < int(CHICK[d] * 10000):
                    await con.execute("DELETE FROM chick_games WHERE user_id=$1", uid)
                    return {"status": "dead", "steps": k + 1, "bet": bet}
                k += 1
                pay = cap(int(bet * cmult(d, k)))
                if k >= CHICK_MAX or (B.MAX_PAYOUT > 0 and pay >= B.MAX_PAYOUT):
                    await con.execute("DELETE FROM chick_games WHERE user_id=$1", uid)
                    return {"status": "win", "steps": k, "payout": pay, "gift": await pay_out(con, uid, pay, g["nft"], g["bet"])}
                await con.execute("UPDATE chick_games SET steps=$2 WHERE user_id=$1", uid, k)
                return {"status": "ok", "steps": k}

    @route("chick/cash")
    async def chick_cash(u, b):
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM chick_games WHERE user_id=$1 FOR UPDATE", uid)
                if not g or g["steps"] < 1:
                    raise Err("Сделайте хотя бы один шаг")
                pay = cap(int(g["bet"] * cmult(g["diff"], g["steps"])))
                await con.execute("DELETE FROM chick_games WHERE user_id=$1", uid)
                return {"status": "cash", "payout": pay, "steps": g["steps"], "gift": await pay_out(con, uid, pay, g["nft"], g["bet"])}

    # ───────────── МОНЕТКА ─────────────
    @route("coin/flip")
    async def coin_flip(u, b):
        bet, side, uid = int(b.get("bet", 0)), b.get("side"), u["id"]
        check_bet(bet)
        if side not in ("h", "t"):
            raise Err("Ошибка")
        async with B.pool.acquire() as con:
            async with con.transaction():
                await take(con, uid, bet)
                win = (await F.rng(con, uid)).randbelow(1000) < 485
                pay = int(bet * COIN_MULT) if win else 0
                if pay:
                    await give(con, uid, pay)
        return {"win": win, "res": side if win else ("t" if side == "h" else "h"), "payout": pay, "bet": bet}

    # ───────────── КОСТИ ─────────────
    @route("dice/roll")
    async def dice_roll(u, b):
        try:
            bet, chance, uid = int(b.get("bet", 0)), int(b.get("chance", 0)), u["id"]
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная ставка")
        mode = b.get("mode")
        check_bet(bet)
        if mode not in ("under", "over") or not (DICE_MIN <= chance <= DICE_MAX):
            raise Err("Ошибка")
        mult = DICE_RTP * 100 / chance
        async with B.pool.acquire() as con:
            async with con.transaction():
                await take(con, uid, bet)
                rn = (await F.rng(con, uid)).randbelow(10000)          # 0.00 … 99.99
                win = rn < chance * 100 if mode == "under" else rn >= 10000 - chance * 100
                pay = int(bet * mult) if win else 0
                if pay:
                    await give(con, uid, pay)
        return {"win": win, "roll": rn / 100, "mult": round(mult, 4), "payout": pay, "bet": bet}

    # ───────────── КОЛЕСО УДАЧИ ─────────────
    @route("wheel/spin")
    async def wheel_spin(u, b):
        try:
            bet, uid = int(b.get("bet", 0)), u["id"]
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная ставка")
        lvl = b.get("lvl")
        check_bet(bet)
        if lvl not in WHEEL:
            raise Err("Ошибка")
        segs = WHEEL[lvl]
        async with B.pool.acquire() as con:
            async with con.transaction():
                await take(con, uid, bet)
                idx = (await F.rng(con, uid)).randbelow(len(segs))
                pay = int(bet * segs[idx])
                if pay:
                    await give(con, uid, pay)
        return {"idx": idx, "mult": segs[idx], "payout": pay, "bet": bet, "win": pay > bet}

    # ───────────── ПЛИНКО ─────────────
    @route("plinko/drop")
    async def plinko_drop(u, b):
        try:
            bet, uid = int(b.get("bet", 0)), u["id"]
        except (TypeError, ValueError, OverflowError):
            raise Err("Некорректная ставка")
        risk = b.get("risk")
        check_bet(bet)
        if risk not in PLINKO:
            raise Err("Ошибка")
        async with B.pool.acquire() as con:
            async with con.transaction():
                await take(con, uid, bet)
                rg = await F.rng(con, uid)
                path = [rg.randbelow(2) for _ in range(PLINKO_ROWS)]   # 0 — влево, 1 — вправо
                slot = sum(path)
                mult = PLINKO[risk][slot]
                pay = int(bet * mult)
                if pay:
                    await give(con, uid, pay)
        return {"path": path, "slot": slot, "mult": mult, "payout": pay, "bet": bet}

    # ───────────── БАШНЯ ─────────────
    @route("tower/start")
    async def tower_start(u, b):
        d, uid = b.get("d"), u["id"]
        if d not in TOWER:
            raise Err("Ошибка")
        async with B.pool.acquire() as con:
            async with con.transaction():
                if await con.fetchval("SELECT 1 FROM tower_games WHERE user_id=$1", uid):
                    return {}
                bet, nft = await stake(con, uid, b)
                await con.execute("INSERT INTO tower_games(user_id, bet, diff, nft) VALUES($1,$2,$3,$4)", uid, bet, d, nft)
        return {}

    @route("tower/pick")
    async def tower_pick(u, b):
        uid, i = u["id"], int(b.get("i", -1))
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM tower_games WHERE user_id=$1 FOR UPDATE", uid)
                if not g:
                    raise Err("Игра не найдена")
                d, bet, k = g["diff"], int(g["bet"]), int(g["lvl"])
                n = TOWER[d]
                if not 0 <= i < n:
                    raise Err("Ошибка")
                rg = await F.rng(con, uid)
                bomb = rg.randbelow(n)
                if bomb == i:
                    await con.execute("DELETE FROM tower_games WHERE user_id=$1", uid)
                    return {"status": "dead", "bomb": bomb, "lvl": k + 1, "bet": bet}
                bomb = rg.choice([x for x in range(n) if x != i])      # для показа: где была бомба
                k += 1
                pay = cap(int(bet * tmult(d, k)))
                if k >= TOWER_MAX:
                    await con.execute("DELETE FROM tower_games WHERE user_id=$1", uid)
                    return {"status": "win", "bomb": bomb, "lvl": k, "payout": pay, "gift": await pay_out(con, uid, pay, g["nft"], g["bet"])}
                await con.execute("UPDATE tower_games SET lvl=$2 WHERE user_id=$1", uid, k)
                return {"status": "ok", "bomb": bomb, "lvl": k}

    @route("tower/cash")
    async def tower_cash(u, b):
        uid = u["id"]
        async with B.pool.acquire() as con:
            async with con.transaction():
                g = await con.fetchrow("SELECT * FROM tower_games WHERE user_id=$1 FOR UPDATE", uid)
                if not g or g["lvl"] < 1:
                    raise Err("Пройдите хотя бы один уровень")
                pay = cap(int(g["bet"] * tmult(g["diff"], g["lvl"])))
                await con.execute("DELETE FROM tower_games WHERE user_id=$1", uid)
                return {"status": "cash", "payout": pay, "lvl": g["lvl"], "gift": await pay_out(con, uid, pay, g["nft"], g["bet"])}

    async def index(request):
        return web.FileResponse(INDEX, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/", index)
    if GIFTS.is_dir():
        app.router.add_static("/img/", GIFTS)

        async def _cache_img(request, response):
            if request.path.startswith("/img/"):
                response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        app.on_response_prepare.append(_cache_img)
