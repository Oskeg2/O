"""Provably fair: HMAC-SHA256(server_seed, "client:nonce:block") -> поток 32-битных чисел.
Игроку заранее виден sha256(server_seed); сам seed раскрывается после смены (rotate)."""
import hashlib
import hmac
import secrets

DDL = ("CREATE TABLE IF NOT EXISTS fair(user_id BIGINT PRIMARY KEY, seed TEXT NOT NULL, client TEXT NOT NULL DEFAULT 'oskeg', "
       "nonce BIGINT NOT NULL DEFAULT 0, p_seed TEXT, p_client TEXT, p_nonce BIGINT)")
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()


class Rng:
    def __init__(s, seed, client, nonce):
        s.k, s.m, s.i = seed.encode(), f"{client}:{nonce}:", 0

    def _u32(s):
        b, j = divmod(s.i, 8)
        s.i += 1
        h = hmac.new(s.k, f"{s.m}{b}".encode(), hashlib.sha256).digest()
        return int.from_bytes(h[j * 4:j * 4 + 4], "big")

    def randbelow(s, n):                       # без смещения (rejection sampling)
        lim = 2 ** 32 // n * n
        while (v := s._u32()) >= lim:
            pass
        return v % n

    def sample(s, n, k):                       # частичный Фишер–Йейтс
        a = list(range(n))
        for i in range(k):
            j = i + s.randbelow(n - i)
            a[i], a[j] = a[j], a[i]
        return a[:k]

    def choice(s, seq):
        return seq[s.randbelow(len(seq))]


async def rng(con, uid):
    """Новая ставка: атомарно берёт следующий nonce. Вызывать после списания ставки."""
    r = await con.fetchrow(
        "INSERT INTO fair(user_id, seed) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET nonce=fair.nonce+1 "
        "RETURNING seed, client, nonce", uid, secrets.token_hex(32))
    return Rng(r["seed"], r["client"], r["nonce"])


async def rng_n(con, uid, n):
    """n ставок одним запросом: резервирует n подряд идущих nonce (те же значения, что дали бы n вызовов rng())."""
    r = await con.fetchrow(
        "INSERT INTO fair(user_id, seed, nonce) VALUES($1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET nonce=fair.nonce+$4 "
        "RETURNING seed, client, nonce", uid, secrets.token_hex(32), n - 1, n)
    return [Rng(r["seed"], r["client"], r["nonce"] - n + 1 + i) for i in range(n)]


async def state(pool, uid):
    return state_from_row(await pool.fetchrow("SELECT * FROM fair WHERE user_id=$1", uid))


def state_from_row(r):
    if not r:
        return {"hash": None, "client": "oskeg", "nonce": 0, "prev": None}
    prev = {"seed": r["p_seed"], "hash": sha(r["p_seed"]), "client": r["p_client"], "n": r["p_nonce"]} if r["p_seed"] else None
    return {"hash": sha(r["seed"]), "client": r["client"], "nonce": r["nonce"] + 1, "prev": prev}


async def rotate(con, uid, client):
    """Раскрывает текущий seed, выдаёт новый. Нельзя, пока идёт игра с несохранённым исходом."""
    if await con.fetchval("SELECT 1 FROM mines_games WHERE user_id=$1 UNION ALL SELECT 1 FROM chick_games WHERE user_id=$1 "
                          "UNION ALL SELECT 1 FROM tower_games WHERE user_id=$1", uid):
        return False
    await con.execute(
        "INSERT INTO fair(user_id, seed, client) VALUES($1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET "
        "p_seed=fair.seed, p_client=fair.client, p_nonce=fair.nonce+1, seed=$2, client=$3, nonce=0", uid, secrets.token_hex(32), client)
    return True
