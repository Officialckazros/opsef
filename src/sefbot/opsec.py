import random
from typing import Dict, List, Tuple

from sefbot import db

_WORK_COOLDOWN_SECONDS = 60
_POSITIONS = [
    "cashier",
    "janitor",
    "waiter",
    "software engineer",
    "game developer",
    "programmer",
    "mother fucker",
]
_OPSEC_RESULTS = ["small", "big", "VERY", "HUGE", "no"]
_RNG = random.SystemRandom()


def get_balance(user_id: str) -> int:
    return db.economy_balance(str(user_id))


def add_balance(user_id: str, amount: int) -> int:
    return db.economy_adjust(str(user_id), int(amount))


def get_leaderboard(limit: int = 10) -> List[Tuple[str, Dict[str, int]]]:
    return db.economy_leaderboard(limit)


def work_cooldown_left(user_id: str) -> int:
    row = db.conn().execute(
        "SELECT last_work FROM work_cooldowns WHERE user_id=?", (str(user_id),)
    ).fetchone()
    if not row:
        return 0
    return max(0, int(_WORK_COOLDOWN_SECONDS - (db.now() - float(row["last_work"]))))


def perform_work(user_id: str) -> Tuple[int, int, str]:
    reward = _RNG.randint(50, 499)
    position = _RNG.choice(_POSITIONS)
    remaining, balance = db.economy_claim_work(user_id, reward, _WORK_COOLDOWN_SECONDS)
    return (0 if remaining else reward), balance, position


def opsec_result(user_id: str) -> str:
    return _RNG.choice(_OPSEC_RESULTS)


def gayrate(user_id: str) -> int:
    return _RNG.randint(0, 99)
