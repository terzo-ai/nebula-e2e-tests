import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class PollTimeoutError(Exception):
    def __init__(self, description: str, timeout: float, last_value: object = None):
        self.description = description
        self.timeout = timeout
        self.last_value = last_value
        super().__init__(
            f"Timed out waiting for {description} after {timeout}s. "
            f"Last value: {last_value}"
        )


async def poll_until(
    check_fn: Callable[[], Awaitable[T]],
    predicate: Callable[[T], bool],
    *,
    timeout: float = 300,
    interval: float = 2.0,
    backoff: float = 1.5,
    max_interval: float = 15.0,
    description: str = "condition",
) -> T:
    """Poll check_fn until predicate returns True or timeout is reached.

    Uses exponential backoff between polls to avoid hammering the API.
    Returns the value that satisfied the predicate.
    Raises PollTimeoutError if timeout is exceeded.
    """
    start = time.monotonic()
    current_interval = interval
    last_value: T | None = None

    while True:
        last_value = await check_fn()
        if predicate(last_value):
            return last_value

        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise PollTimeoutError(description, timeout, last_value)

        remaining = timeout - elapsed
        sleep_time = min(current_interval, max_interval, remaining)
        await asyncio.sleep(sleep_time)
        current_interval *= backoff
