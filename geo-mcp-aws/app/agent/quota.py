"""Daily limits for an open demo: a site-wide spend cap and per-user question limits.

Spend is estimated from the token counts Bedrock returns with every reply (input/output tokens x
the model's price) plus a small fixed amount per address search. Counters live in the sessions
table (keys 'quota#<day>#...', which never collide with the SHA-256 session keys), updated
atomically, and expire after a few days. Days are UTC (midnight UTC = 8 pm Eastern in summer).

A question that would go over a limit is answered with a short message and never reaches the
model. One question can make several model calls, so the cap can be exceeded by at most one
question's worth (about half a cent with Nova Lite).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "0") or 0)        # 0 = no cap
DEMO_DAILY_QUESTIONS = int(os.environ.get("DEMO_DAILY_QUESTIONS", "30") or 0)
USER_DAILY_QUESTIONS = int(os.environ.get("USER_DAILY_QUESTIONS", "200") or 0)
EXEMPT = {u.strip().lower() for u in os.environ.get("QUOTA_EXEMPT_USERS", "").split(",") if u.strip()}
PRICE_IN = float(os.environ.get("BEDROCK_PRICE_IN_PER_MTOK", "0.06"))     # USD per million input tokens
PRICE_OUT = float(os.environ.get("BEDROCK_PRICE_OUT_PER_MTOK", "0.24"))   # USD per million output tokens
GEOCODE_PRICE = float(os.environ.get("GEOCODE_PRICE_USD", "0.0005"))      # per address search
MICRO = 1_000_000                                                        # spend stored in micro-dollars


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def model_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * PRICE_IN + output_tokens * PRICE_OUT) / 1_000_000


class MemoryQuota:
    def __init__(self):
        self.n: dict[str, int] = {}

    def _add(self, key: str, by: int) -> int:
        self.n[key] = self.n.get(key, 0) + by
        return self.n[key]

    def _get(self, key: str) -> int:
        return self.n.get(key, 0)

    def _once(self, key: str) -> bool:
        if key in self.n:
            return False
        self.n[key] = 1
        return True


class DynamoQuota(MemoryQuota):
    def __init__(self, table: str):
        import boto3
        self.t = boto3.resource("dynamodb").Table(table)

    def _add(self, key: str, by: int) -> int:
        r = self.t.update_item(Key={"sid": key}, UpdateExpression="ADD n :by SET expires = :exp",
                               ExpressionAttributeValues={":by": by, ":exp": int(time.time()) + 3 * 86400},
                               ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["n"])

    def _get(self, key: str) -> int:
        item = self.t.get_item(Key={"sid": key}, ConsistentRead=True).get("Item")
        return int(item["n"]) if item else 0

    def _once(self, key: str) -> bool:
        """True for exactly one caller per key (conditional put), across all Lambda instances."""
        try:
            self.t.put_item(Item={"sid": key, "n": 1, "expires": int(time.time()) + 3 * 86400},
                            ConditionExpression="attribute_not_exists(sid)")
            return True
        except self.t.meta.client.exceptions.ConditionalCheckFailedException:
            return False


class Quota:
    def __init__(self, store: MemoryQuota):
        self.s = store

    def _site(self) -> str:
        return f"quota#{today()}#site-spend"

    def _user(self, user: str) -> str:
        return f"quota#{today()}#user#{user.lower()}"

    def spent(self) -> float:
        return self.s._get(self._site()) / MICRO

    def add_spend(self, usd: float) -> None:
        if usd > 0:
            self.s._add(self._site(), max(1, round(usd * MICRO)))

    def limit_for(self, demo: bool) -> int:
        return DEMO_DAILY_QUESTIONS if demo else USER_DAILY_QUESTIONS

    def status(self, user: str, demo: bool) -> dict:
        exempt = user.lower() in EXEMPT
        limit = self.limit_for(demo)
        used = self.s._get(self._user(user))
        return {"exempt": exempt, "questions_used": used,
                "questions_left": None if exempt or not limit else max(0, limit - used),
                "site_budget_usd": DAILY_BUDGET_USD or None, "site_spent_usd": round(self.spent(), 4)}

    def blocked(self, user: str, demo: bool) -> str | None:
        """Reason this user may not ask another question today, or None."""
        if user.lower() in EXEMPT:
            return None
        if DAILY_BUDGET_USD and self.spent() >= DAILY_BUDGET_USD:
            return ("The demo has reached today's usage budget, so I can't answer more questions until "
                    "midnight UTC (8 pm Eastern). Thanks for trying it!")
        limit = self.limit_for(demo)
        if limit and self.s._get(self._user(user)) >= limit:
            return (f"You've used today's {limit} questions for this demo account. The limit resets at "
                    "midnight UTC (8 pm Eastern).")
        return None

    def search_blocked(self, user: str) -> bool:
        return bool(user.lower() not in EXEMPT and DAILY_BUDGET_USD and self.spent() >= DAILY_BUDGET_USD)

    def once(self, name: str) -> bool:
        """True the first time `name` is claimed today (e.g. one alert email per threshold per day)."""
        return self.s._once(f"quota#{today()}#{name}")

    def count_question(self, user: str) -> None:
        self.s._add(self._user(user), 1)
