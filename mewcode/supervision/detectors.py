"""Conservative built-in detectors; advice never changes execution limits."""
from __future__ import annotations

from .bus import Advisory, AdvisoryBus


def warn_iteration_budget(bus: AdvisoryBus, limit: int, iteration: int) -> None:
    """Warn once per run in the final 10% of admitted loop iterations.

    This uses the loop's hard allowance, not response turns, tool counts or
    tokens. Retries within an iteration do not spend an additional allowance.
    The current iteration is included in remaining. Small runs get one warning
    opportunity on their last iteration; there is no extra completion request.
    """
    if not bus.enabled or not 1 <= iteration <= limit:
        return
    remaining = limit - iteration + 1
    threshold = max(1, (limit + 9) // 10)
    if remaining > threshold:
        return
    if bus.publish(Advisory(
        key="builtin:iteration-budget", category="iteration_budget", priority=90,
        ttl=remaining,
        text=(
            f"Execution loop budget is low: this run has entered its final {threshold} "
            f"iterations out of a limit of {limit}. Delivery budgets may defer this reminder. "
            "Request retries may occur within an iteration; "
            "this is not a token budget or a count of tool calls. "
            "Prioritize applicable verification of the current work and report what is "
            "completed, what failed, and what remains unverified. Avoid starting unrelated "
            "work. Only claim checks that actually ran; this reminder grants no new permissions."
        ),
    )):
        bus._event("iteration_budget_warning", limit=limit, iteration=iteration,
                   remaining=remaining, threshold=threshold)
