"""One retry policy for every transport: git over the network and the GitLab REST API.

``call_with_retry`` runs an operation, asks a caller supplied ``should_retry`` whether a
failure is worth another attempt, waits with exponential backoff and jitter, reports
each retry through ``on_retry`` (the CLI logs it, the fleet UI shows it on the
repository's row) and raises :class:`RetryExhaustedError` with the attempt count and the
elapsed time when the attempts run out. Sleep, clock and randomness are parameters, so
tests run instantly and deterministically.
"""

import random
import time
from collections.abc import Callable
from typing import NamedTuple


class RetryPolicy(NamedTuple):
    """How often and how patiently to retry."""

    attempts: int
    base_delay: float
    max_delay: float
    jitter: float = 0.25


class RetryDecision(NamedTuple):
    """Retry this failure: ``reason`` for the user, optional ``delay`` (``Retry-After``)
    and an optional lower ``max_attempts`` for this kind of failure (a transient 404)."""

    reason: str
    delay: float | None = None
    max_attempts: int | None = None


class RetryEvent(NamedTuple):
    operation: str
    attempt: int  # the attempt that failed, from 1
    of: int  # the attempts allowed
    delay: float
    reason: str

    def render(self) -> str:
        return (
            f"{self.operation}: {self.reason}, retry {self.attempt} of {self.of - 1} "
            f"in {self.delay:.1f}s"
        )


class RetryExhaustedError(Exception):
    """Every attempt failed with a retryable error."""

    def __init__(self, operation: str, attempts: int, elapsed: float, last: BaseException) -> None:
        super().__init__(
            f"{operation} failed after {attempts} attempts over {elapsed:.0f}s: {last}"
        )
        self.operation = operation
        self.attempts = attempts
        self.elapsed = elapsed
        self.last = last


def backoff_delay(
    policy: RetryPolicy,
    attempt: int,
    *,
    uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    """``base * 2**attempt`` capped at ``max_delay``, plus up to ``jitter`` of it at random."""
    capped = min(policy.base_delay * float(1 << min(attempt, 30)), policy.max_delay)
    return capped + uniform(0, capped * policy.jitter)


def call_with_retry[T](
    func: Callable[[], T],
    policy: RetryPolicy,
    *,
    operation: str,
    should_retry: Callable[[BaseException, int], RetryDecision | None],
    on_retry: Callable[[RetryEvent], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    uniform: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run ``func`` until it succeeds, a failure is not retryable, or attempts run out."""
    started = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except Exception as exc:
            decision = should_retry(exc, attempt)
            if decision is None:
                raise
            allowed = min(policy.attempts, decision.max_attempts or policy.attempts)
            if attempt >= allowed:
                raise RetryExhaustedError(operation, attempt, clock() - started, exc) from exc
            delay = (
                min(decision.delay, policy.max_delay)
                if decision.delay is not None
                else backoff_delay(policy, attempt - 1, uniform=uniform)
            )
            if on_retry is not None:
                on_retry(RetryEvent(operation, attempt, allowed, delay, decision.reason))
            sleep(delay)
