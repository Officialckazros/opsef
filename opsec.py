import json
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "opsec" / "data.json"

OPSEC_OWNER_ID = "1534261140077678602"
_WORK_COOLDOWN_SECONDS = 60
_WORK_COOLDOWNS: Dict[str, float] = {}
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


def _load_data() -> Dict[str, Any]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"template": {"balance": 0, "deposit": 0}, "bot": {}}


def _save_data(data: Dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_user(data: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    user_id = str(user_id)
    template = data.get("template", {"balance": 0, "deposit": 0})
    if user_id not in data:
        data[user_id] = {**template}
    return data[user_id]


def get_data() -> Dict[str, Any]:
    return _load_data()


def get_user_data(user_id: str) -> Dict[str, Any]:
    data = _load_data()
    return _ensure_user(data, str(user_id))


def modify_user_data(user_id: str, key: str, value: Any) -> Dict[str, Any]:
    data = _load_data()
    record = _ensure_user(data, str(user_id))
    record[key] = value
    _save_data(data)
    return record


def get_balance(user_id: str) -> int:
    record = get_user_data(str(user_id))
    return int(record.get("balance", 0))


def add_balance(user_id: str, amount: int) -> int:
    data = _load_data()
    record = _ensure_user(data, str(user_id))
    record["balance"] = int(record.get("balance", 0)) + int(amount)
    if record["balance"] < 0:
        record["balance"] = 0
    _save_data(data)
    return record["balance"]


def get_leaderboard(limit: int = 10) -> List[Tuple[str, Dict[str, Any]]]:
    data = _load_data()
    rows = [
        (uid, rec)
        for uid, rec in data.items()
        if uid not in ("template", "bot")
    ]
    rows.sort(key=lambda item: int(item[1].get("balance", 0)), reverse=True)
    return rows[:limit]


def work_cooldown_left(user_id: str) -> int:
    now_ts = time.time()
    last = _WORK_COOLDOWNS.get(str(user_id), 0.0)
    elapsed = now_ts - last
    if elapsed >= _WORK_COOLDOWN_SECONDS:
        return 0
    return int(_WORK_COOLDOWN_SECONDS - elapsed)


def perform_work(user_id: str) -> Tuple[int, int, str]:
    now_ts = time.time()
    _WORK_COOLDOWNS[str(user_id)] = now_ts
    reward = random.randint(50, 499)
    position = random.choice(_POSITIONS)
    balance = add_balance(user_id, reward)
    return reward, balance, position


def opsec_result(user_id: str) -> str:
    return random.choice(_OPSEC_RESULTS)


def gayrate(user_id: str) -> int:
    return random.randint(0, 99)


def owner_can_eval(user_id: str) -> bool:
    return str(user_id) in {str(config.OWNER_ID), OPSEC_OWNER_ID}


def eval_helper(user_id: str, code: str, reply_func) -> str:
    code = code.strip()
    if not code:
        return "usage: `eval <js-style code or $preset>`"

    if code.startswith("$"):
        parts = code[1:].split()
        if not parts:
            return "unknown helper"
        command = parts[0]
        args = parts[1:]
        if command == "returnUserData":
            return reply_func("returnUserData", *args)
        if command == "modifyUserData":
            return reply_func("modifyUserData", *args)
        if command == "say":
            return reply_func("say", " ".join(args) if args else "")
        return f"unknown helper `{command}`"

    env: Dict[str, Any] = {
        "__builtins__": __builtins__,
        "get_user_data": get_user_data,
        "modify_user_data": modify_user_data,
        "get_balance": get_balance,
        "add_balance": add_balance,
    }
    try:
        exec(code, env, env)
        return "eval passed"
    except Exception as exc:
        return f"eval error: {exc}"
