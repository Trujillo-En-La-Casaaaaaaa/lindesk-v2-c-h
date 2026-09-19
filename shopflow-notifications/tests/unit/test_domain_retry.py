"""Unit tests of the bounded retry policy (component: retry-policy)."""

from __future__ import annotations

import pytest

from app.domain.retry import RetryPolicy


def test_a_policy_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=1, base_delay_seconds=-1)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=1, max_delay_seconds=-1)


def test_should_retry_stops_at_the_bound() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=1, max_delay_seconds=10)

    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True
    assert policy.should_retry(3) is False
    assert policy.should_retry(4) is False
    assert policy.attempts_left(1) == 2
    assert policy.attempts_left(3) == 0


def test_delay_grows_exponentially_and_is_capped() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay_seconds=0.5, max_delay_seconds=2)

    assert policy.delay_for(1) == 0.5
    assert policy.delay_for(2) == 1.0
    assert policy.delay_for(3) == 2.0
    assert policy.delay_for(4) == 2.0
    assert policy.delay_for(5) == 2.0
    assert policy.delay_milliseconds(2) == 1000


def test_a_zero_backoff_policy_never_sleeps() -> None:
    policy = RetryPolicy(max_attempts=2)

    assert policy.delay_for(1) == 0
    assert policy.delay_milliseconds(1) == 0


def test_delay_requires_a_real_attempt_number() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=2, base_delay_seconds=1, max_delay_seconds=1).delay_for(0)
