"""The shared retry policy: backoff, events, exhaustion, injected sleep and clock."""

import pytest

from steersync import retry
from steersync.retry import RetryEvent, RetryExhaustedError, RetryPolicy


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


def no_jitter(low: float, high: float) -> float:
    del high
    return low


def test_backoff_is_exponential_capped_and_jittered() -> None:
    policy = RetryPolicy(attempts=5, base_delay=1.0, max_delay=5.0, jitter=0.25)
    assert [retry.backoff_delay(policy, n, uniform=no_jitter) for n in range(5)] == [
        1.0,
        2.0,
        4.0,
        5.0,
        5.0,
    ]
    assert retry.backoff_delay(policy, 0, uniform=lambda lo, hi: hi) == 1.25


def test_call_with_retry_succeeds_after_transient_failures() -> None:
    clock = Clock()
    events: list[RetryEvent] = []
    outcomes: list[object] = [OSError("503"), OSError("503"), "ok"]

    def attempt() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    result = retry.call_with_retry(
        attempt,
        RetryPolicy(attempts=4, base_delay=1.0, max_delay=8.0, jitter=0.0),
        operation="GitLab GET merge_requests",
        should_retry=lambda exc, n: retry.RetryDecision(f"{exc}"),
        on_retry=events.append,
        sleep=clock.sleep,
        clock=clock.time,
    )
    assert result == "ok"
    assert clock.slept == [1.0, 2.0]
    assert [(e.attempt, e.of, e.delay, e.reason) for e in events] == [
        (1, 4, 1.0, "503"),
        (2, 4, 2.0, "503"),
    ]
    assert events[0].render() == "GitLab GET merge_requests: 503, retry 1 of 3 in 1.0s"


def test_exhaustion_reports_attempts_and_elapsed_time() -> None:
    clock = Clock()

    def attempt() -> str:
        raise OSError("503")

    with pytest.raises(RetryExhaustedError) as caught:
        retry.call_with_retry(
            attempt,
            RetryPolicy(attempts=3, base_delay=2.0, max_delay=8.0, jitter=0.0),
            operation="op",
            should_retry=lambda exc, n: retry.RetryDecision("503"),
            sleep=clock.sleep,
            clock=clock.time,
        )
    assert caught.value.attempts == 3
    assert caught.value.elapsed == pytest.approx(6.0)
    assert isinstance(caught.value.last, OSError)


def test_non_retryable_errors_propagate_at_once() -> None:
    calls = []

    def attempt() -> str:
        calls.append(1)
        raise ValueError("401")

    with pytest.raises(ValueError, match="401"):
        retry.call_with_retry(
            attempt,
            RetryPolicy(attempts=5, base_delay=0, max_delay=0),
            operation="op",
            should_retry=lambda exc, n: None,
            sleep=lambda s: None,
        )
    assert calls == [1]


def test_decision_can_override_delay_and_limit_attempts() -> None:
    clock = Clock()
    seen = []

    def attempt() -> str:
        seen.append(1)
        raise OSError("429")

    with pytest.raises(RetryExhaustedError) as caught:
        retry.call_with_retry(
            attempt,
            RetryPolicy(attempts=5, base_delay=1.0, max_delay=30.0, jitter=0.0),
            operation="op",
            should_retry=lambda exc, n: retry.RetryDecision("429", delay=7.0, max_attempts=2),
            sleep=clock.sleep,
            clock=clock.time,
        )
    assert clock.slept == [7.0]
    assert caught.value.attempts == 2
    assert len(seen) == 2
