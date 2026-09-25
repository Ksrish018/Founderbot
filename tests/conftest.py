from __future__ import annotations

import os
from collections import defaultdict, deque

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKENTESTTOKEN")
os.environ.setdefault("GEMINI_API_KEY", "AQ.test-key-not-real")

from app.config import Settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.gemini_client import CallResult  # noqa: E402

# An in-voice LinkedIn draft (~430 words) built only from Note 01 and canonical facts.
GOOD_POST = """Batch fourteen came back from our manufacturer last month with a finished pH about 0.4 units lower than the batch before it. Nothing on the label changed. The formula name, the INCI list and the concentrations were all identical.

The cause took a week to find. Our supplier had changed the preservative blend and sent a revised spec sheet three months earlier. It got buried in an inbox. The new preservative system is more acidic than the old one, and that was enough to push the finished product out of the range our emollient blend is designed for. The batch is not unsafe. It is also not the product our customers bought last time, because the texture is different in a way I think people would notice.

We are holding it.

This is the part of manufacturing that almost nobody talks about. A reorder of the same formula is treated as the same product. It often isn't. Suppliers reformulate raw materials for their own reasons - cost, availability, a regulatory change in another market - and the notification can arrive as a single revised document that looks like every other document in the folder. The first thing that catches it is the CoA, if someone compares it against a baseline. The second thing is a pH reading on the finished batch. The third thing, if neither of those happens, is the customer.

I'm not saying suppliers are careless or that this was done to mislead anyone. What I'm saying is that the system assumes nothing changes between batches, and that assumption is where the gap sits.

Our contract manufacturer brief requires pH documentation, stability testing at three-month intervals for the first year, and mid-batch CoA sampling. Our serum pH is documented at 5.5-5.8 on every batch. That process is the reason we caught this one before it shipped. It is also fair to say the revised spec sheet sat unread for three months, which is a gap on our side that we have now closed by logging every supplier document against the batch it affects.

If you buy skincare regularly, you can ask any brand whether the finished pH is checked on every batch, whether the CoA is compared against a baseline, and whether supplier changes are logged against specific batches. If the answer is vague, that is useful information.

If you are building a brand, start with the supplier documents. The label percentage can stay the same while the product changes underneath it."""


class FakeGemini:
    """Queue canned structured responses per purpose prefix."""

    def __init__(self):
        self.model = "fake-flash"
        self.responses: dict[str, deque] = defaultdict(deque)
        self.calls: list[dict] = []

    def queue(self, purpose: str, obj) -> None:
        self.responses[purpose].append(obj)

    async def generate_json(self, *, purpose, prompt, schema, system=None, temperature=0.2):
        self.calls.append({"purpose": purpose, "prompt": prompt, "system": system})
        key = next((k for k in self.responses if purpose.startswith(k) and self.responses[k]), None)
        if key is None and purpose == "audit":
            return CallResult(schema.model_validate({"problems": []}), self.model, 10, 5, 0.0001, "")
        if key is None:
            raise AssertionError(f"No fake response queued for {purpose}")
        obj = self.responses[key].popleft()
        data = obj if isinstance(obj, schema) else schema.model_validate(obj)
        return CallResult(data, self.model, 100, 50, 0.001, "")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, telegram_bot_token="123456:TESTTOKENTESTTOKEN", gemini_api_key="AQ.test",
                    meera_user_id=42, db_path=":memory:")


@pytest.fixture
def db():
    """SQLite by default. Set PG_TEST_URL=postgresql://... to run the same tests against Postgres."""
    url = os.environ.get("PG_TEST_URL")
    if not url:
        yield DB(":memory:")
        return
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    d = DB(url)
    yield d
    d.close()


@pytest.fixture
def fake() -> FakeGemini:
    return FakeGemini()
