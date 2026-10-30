"""Bounded, run-local advisory delivery and observable expectation accounting."""
from __future__ import annotations

import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Literal

Channel = Literal["system", "reminder", "status"]


@dataclass(frozen=True)
class Expectation:
    event: Literal["tool_call", "tool_success"]
    tool: str
    within_turns: int = 2

    def __post_init__(self) -> None:
        if self.event not in ("tool_call", "tool_success") or not self.tool or self.within_turns < 1:
            raise ValueError("invalid observable expectation")


@dataclass(frozen=True)
class Advisory:
    key: str
    text: str
    channel: Channel = "reminder"
    priority: int = 50
    category: str = "operational"
    ttl: int = 2
    expect: Expectation | None = None

    def __post_init__(self) -> None:
        if (not self.key or not self.text or self.channel not in ("system", "reminder", "status")
                or not 0 <= self.priority <= 100 or self.ttl < 1
                or len(self.key.encode("utf-8")) > 256 or len(self.category.encode("utf-8")) > 128
                or len(self.text.encode("utf-8")) > 16384):
            raise ValueError("invalid advisory")


@dataclass
class Entry:
    advisory: Advisory
    expires: int
    state: str = "pending"
    delivered_turn: int | None = None
    request_id: str = ""


class AdvisoryBus:
    """Budgets include rendered envelopes; no tier/channel exemptions.

    Turn = request receiving its first response event, never a tool invocation.
    Failed-before-response attempts neither deliver nor age TTL. Remote receipt
    without a response is unknown, conservatively unconfirmed. A later stream
    failure retains confirmed delivery. Retry attempts get distinct request IDs.
    """

    def __init__(self, enabled: bool = False, *, limits: dict[str, tuple[int, int]] | None = None) -> None:
        self.enabled = enabled
        self.limits = dict(limits if limits is not None else {
            "system": (2, 2048), "reminder": (3, 4096), "status": (8, 2048),
        })
        if set(self.limits) != {"system", "reminder", "status"} or any(
            count < 0 or size < 0 for count, size in self.limits.values()
        ):
            raise ValueError("invalid channel budgets")
        self.entries: dict[str, Entry] = {}
        self.events: deque[dict] = deque(maxlen=256)
        self.run_id = uuid.uuid4().hex
        self.turn = 0
        self.last_user_input_turn = 0
        self.attempt = 0
        self.last_confirmed_request = ""
        self.started = False
        self.closed = False

    def _event(self, kind: str, **fields: object) -> None:
        if self.enabled:
            self.events.append({"type": kind, "run_id": self.run_id, "turn": self.turn, **fields})

    def begin(self) -> None:
        # A resumed invocation/new service fix attempt is a fresh run. Nothing
        # persists or replays from disk; prequeued entries survive first begin.
        if self.started:
            self.entries.clear()
            self.events.clear()
            self.run_id = uuid.uuid4().hex
            self.turn = self.attempt = self.last_user_input_turn = 0
            self.last_confirmed_request = ""
        self.started, self.closed = True, False
        self._event("run_started")

    def steer(self) -> None:
        self.last_user_input_turn = self.turn
        for entry in self.entries.values():
            if entry.state in ("pending", "waiting"):
                entry.state = "superseded"
        self._event("user_steer")

    def publish(self, advisory: Advisory) -> bool:
        if not self.enabled or self.closed or advisory.key in self.entries or len(self.entries) >= 128:
            return False
        self.entries[advisory.key] = Entry(advisory, self.turn + advisory.ttl)
        self._event("published", key=advisory.key, channel=advisory.channel)
        return True

    @staticmethod
    def render(advisory: Advisory) -> str:
        return f"<advisory key={advisory.key!r} category={advisory.category!r}>\n{advisory.text}\n</advisory>\n"

    def select(self) -> dict[str, list[str]]:
        chosen: dict[str, list[str]] = {channel: [] for channel in self.limits}
        for channel, (count, budget) in self.limits.items():
            if channel == "reminder":
                budget -= len(b"<system-reminder>\n\n</system-reminder>")
            for entry in sorted(self.entries.values(), key=lambda e: -e.advisory.priority):
                item = entry.advisory
                if entry.state != "pending" or item.channel != channel:
                    continue
                if entry.expires < self.turn + 1:
                    entry.state = "expired"
                    self._event("expired", key=item.key)
                    continue
                size = len(self.render(item).encode("utf-8"))
                if len(chosen[channel]) < count and size <= budget:
                    chosen[channel].append(item.key)
                    budget -= size
        return chosen

    def request_started(self) -> str:
        self.attempt += 1
        request_id = f"{self.run_id}:{self.attempt}"
        self._event("request_started", request_id=request_id)
        return request_id

    def confirm(self, request_id: str, keys: list[str]) -> None:
        if request_id != f"{self.run_id}:{self.attempt}" or request_id == self.last_confirmed_request:
            return
        self.last_confirmed_request = request_id
        self.turn += 1
        self._event("model_turn", request_id=request_id)
        for key in keys:
            entry = self.entries[key]
            # Steer invalidates the expectation, but cannot undo a request
            # already sent. Preserve physical delivery without resurrecting it.
            if entry.state == "pending":
                entry.state = "waiting" if entry.advisory.expect else "delivered"
            entry.delivered_turn, entry.request_id = self.turn, request_id
            self._event("delivered", key=key, request_id=request_id)

    def observe(self, event: str, tool: str) -> None:
        for key, entry in self.entries.items():
            expect = entry.advisory.expect
            if (entry.state == "waiting" and expect and expect.event == event and expect.tool == tool
                    and entry.delivered_turn is not None
                    and self.turn < entry.delivered_turn + expect.within_turns):
                entry.state = "adopted"
                self._event("adopted", key=key, observation=event, tool=tool)

    def settle(self) -> None:
        for key, entry in self.entries.items():
            expect = entry.advisory.expect
            if (entry.state == "waiting" and expect and entry.delivered_turn is not None
                    and self.turn >= entry.delivered_turn + expect.within_turns - 1):
                entry.state = "ignored"
                self._event("ignored", key=key)

    def close(self, outcome: str) -> None:
        self.closed = True
        # Stop/cancel never fabricates expiry of an unfinished window.
        self._event("run_closed", outcome=outcome)

    def report(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        status = [self.render(self.entries[key].advisory) for key in self.select()["status"]]
        return {"enabled": True, "run_id": self.run_id, "turn": self.turn,
                "last_user_input_turn": self.last_user_input_turn, "attempts": self.attempt,
                "closed": self.closed,
                "counts": dict(Counter(entry.state for entry in self.entries.values())),
                "status": status,
                "entries": {key: asdict(value) for key, value in self.entries.items()},
                "events": list(self.events)}
