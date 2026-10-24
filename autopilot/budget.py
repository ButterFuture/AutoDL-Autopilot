"""Grants and the budget judgement. The console's billing detail is the record of what was spent; nothing here
estimates past sessions by itself.

A grant says which modes the AI may power an instance on in, and how much it may spend on it per calendar month
(Beijing time, the console's billing period), or that there is no money limit. Before a power-on, or before keeping
an instance on longer:

    spent this month   = the instance's charge rows since the month began
  + in flight          = while it runs: price per hour x the time since its last charge row (or since it started)
  + planned            = price per hour x the hours asked for
  must not exceed the budget.
"""
from __future__ import annotations

import datetime as dt

from .console import BJ

MODES = ("gpu", "nogpu", "both")


def month_start(t: int) -> int:
    d = dt.datetime.fromtimestamp(t, BJ)
    return int(d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def parse_yuan(text: str) -> int | None:
    """'100', '100yuan', '12.5' -> fen; 'none' -> None (no money limit)."""
    s = text.strip().lower()
    for unit in ("yuan", "元"):
        if s.endswith(unit):
            s = s[: -len(unit)].strip()
    if s in ("none", "no", "不设", ""):
        return None
    try:
        value = round(float(s) * 100)
    except ValueError:
        raise ValueError(f"budget {text!r}: a monthly amount in yuan such as 100, or none") from None
    if value <= 0:
        raise ValueError(f"budget {text!r}: more than 0, or none")
    return value


def make_grant(modes: str, budget_fen: int | None, said: str, now: int) -> dict:
    if modes not in MODES:
        raise ValueError(f"modes {modes!r}: one of {', '.join(MODES)}")
    if not said.strip():
        raise ValueError("--said: the user's own words that gave this permission")
    return {"modes": modes, "budget_fen": budget_fen, "at": now, "said": said.strip()}


def allows(grant: dict | None, mode: str) -> bool:
    return grant is not None and grant["modes"] in ("both", mode)


def spent_fen(book: dict, since: int) -> int:
    return sum(r["fen"] for r in book.values() if r["at"] >= since)


def in_flight_fen(inst: dict, book: dict, now: int) -> int:
    """What a running instance has used since the last charge the console booked for it."""
    if inst["state"] not in ("running", "starting") or not inst.get("started_at"):
        return 0
    last = max([r["to"] or r["at"] for r in book.values() if (r["to"] or r["at"]) >= inst["started_at"]],
               default=inst["started_at"])
    price = inst["price_fen_h"][inst["mode"] or "gpu"]
    return -(-max(0, now - last) * price // 3600)


def judge(grant: dict | None, inst: dict, mode: str, hours: float, book: dict, now: int) -> dict:
    """{"ok": bool, "reason": str, and the figures}. Refuses without a grant for the mode."""
    if not allows(grant, mode):
        return {"ok": False, "reason": f"no grant for {mode} mode on {inst['id']}: ask the user, then autopilot grant"}
    if hours <= 0:
        return {"ok": False, "reason": "--hours must be more than 0"}
    price = inst["price_fen_h"][mode]
    spent = spent_fen(book, month_start(now))
    flight = in_flight_fen(inst, book, now)
    planned = -(-int(price * hours * 100) // 100)
    figures = {"price_fen_h": price, "spent_fen": spent, "in_flight_fen": flight, "planned_fen": planned,
               "budget_fen": grant["budget_fen"]}
    if grant["budget_fen"] is None:
        return {"ok": True, "reason": "no money limit", **figures}
    left = grant["budget_fen"] - spent - flight
    figures["left_fen"] = left
    if planned > left:
        return {"ok": False, "reason": f"over budget: {planned / 100:.2f} yuan planned, {left / 100:.2f} left this "
                                       f"month", **figures}
    return {"ok": True, "reason": "within budget", **figures}
