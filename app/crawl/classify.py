from dataclasses import dataclass
import random


@dataclass(frozen=True)
class FailureDecision:
    retry: bool
    error_class: str
    error_code: str


# Infrastructure / I/O faults that should re-enter the queue, not fail permanently.
_TRANSIENT_EXCEPTION_TYPES = (
    OSError,
    TimeoutError,
    ConnectionError,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
)


def is_transient_exception(exc: BaseException) -> bool:
    """True when the failure is likely infrastructure and worth retrying."""
    return isinstance(exc, _TRANSIENT_EXCEPTION_TYPES)


def classify_failure(reason: str | None, status: int | None) -> FailureDecision:
    if reason in {
        "unsafe_url", "blocked_robots", "policy_error", "invalid_input",
        "browser_budget_exhausted",
    }:
        return FailureDecision(False, "policy", reason or "policy_error")
    if reason in {"worker_exception", "internal_error"}:
        # Programming defects and unexpected worker faults must not retry as
        # transport failures or look like provider outages.
        return FailureDecision(False, "internal", reason)
    if status == 429 or (status is not None and 500 <= status <= 599):
        return FailureDecision(True, "http", f"http_{status}")
    if reason in {"transport_error", "timeout", "dns_error", "tls_error", "lease_expired"}:
        return FailureDecision(True, "transport", reason)
    if status is not None and 400 <= status <= 499:
        return FailureDecision(False, "http", f"http_{status}")
    return FailureDecision(False, "permanent", reason or "unknown_failure")


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return max(0.0, retry_after)
    ceiling = min(60.0, 2 ** max(0, attempt - 1))
    return random.uniform(0.0, ceiling)
