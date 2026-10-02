"""Network stall detection and retry policy for the DeepSeek CLI.

Implements Strategy.md Parts IV-VII (see docs/Strategy.md):

  * the configured timeout is a *stall detector*: it fires when no bytes
    arrive from the server for that long, never because total elapsed time
    exceeded it, so a stream that keeps producing data runs forever;
  * a stall with no bytes received at all deletes the previous message and
    resends the input as a fresh message;
  * a stall after some bytes arrived reattaches to the same message id;
  * the retry counter counts both kinds of consecutive failures and any
    successful response resets it to zero;
  * after ``max_consecutive_retries`` the turn is stopped with a network
    connection error.

Exactly one :class:`StallDetector` (and therefore one timer) is active per
attempt of a logical request; it is cancelled before a retry starts.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_RESPONSE_TIMEOUT_MINUTES = 3.0
DEFAULT_MAX_CONSECUTIVE_RETRIES = 5
DEFAULT_WATCH_INTERVAL_SECONDS = 60.0
DEFAULT_CONTINUATION_GRACE_MS = 500

NETWORK_ERROR_TEMPLATE = (
    "Network Connection Error\n"
    "The model did not respond after {attempts} consecutive attempts.\n"
    "The operation has been stopped."
)


class StallTimeout(Exception):
    """Raised when no bytes arrive from the server within the stall window."""


def network_error_message(attempts: int) -> str:
    return NETWORK_ERROR_TEMPLATE.format(attempts=attempts)


@dataclass(frozen=True)
class NetworkRetryConfig:
    response_timeout_minutes: float = DEFAULT_RESPONSE_TIMEOUT_MINUTES
    max_consecutive_retries: int = DEFAULT_MAX_CONSECUTIVE_RETRIES

    @property
    def stall_timeout_seconds(self) -> float:
        return float(self.response_timeout_minutes) * 60.0


@dataclass(frozen=True)
class WatchConfig:
    """``<<WATCH>>`` polling settings (config.json -> "watch")."""

    interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS
    poll_chars: str = ""


@dataclass(frozen=True)
class ContinuationConfig:
    """Post-FINISHED grace period before the completion check runs."""

    grace_ms: int = DEFAULT_CONTINUATION_GRACE_MS


def _coerce_minutes(value: object) -> float:
    try:
        minutes = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RESPONSE_TIMEOUT_MINUTES
    return minutes if minutes > 0 else DEFAULT_RESPONSE_TIMEOUT_MINUTES


def _coerce_retries(value: object) -> int:
    try:
        retries = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONSECUTIVE_RETRIES
    return retries if retries > 0 else DEFAULT_MAX_CONSECUTIVE_RETRIES


def load_network_retry_config(path: Optional[Path | str] = None) -> NetworkRetryConfig:
    """Read ``config.json`` fresh. Never cached across request lifecycles."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    section = raw.get("network_retry") if isinstance(raw, dict) else None
    section = section if isinstance(section, dict) else {}
    return NetworkRetryConfig(
        response_timeout_minutes=_coerce_minutes(
            section.get("response_timeout_minutes", DEFAULT_RESPONSE_TIMEOUT_MINUTES)
        ),
        max_consecutive_retries=_coerce_retries(
            section.get("max_consecutive_retries", DEFAULT_MAX_CONSECUTIVE_RETRIES)
        ),
    )


def load_watch_config(path: Optional[Path | str] = None) -> WatchConfig:
    """Read the ``watch`` section of ``config.json`` fresh, like network_retry."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    section = raw.get("watch") if isinstance(raw, dict) else None
    section = section if isinstance(section, dict) else {}
    try:
        interval = float(section.get("interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        interval = DEFAULT_WATCH_INTERVAL_SECONDS
    if interval <= 0:
        interval = DEFAULT_WATCH_INTERVAL_SECONDS
    poll_chars = section.get("poll_chars", "")
    if not isinstance(poll_chars, str):
        poll_chars = ""
    return WatchConfig(interval_seconds=interval, poll_chars=poll_chars)


def load_continuation_config(path: Optional[Path | str] = None) -> ContinuationConfig:
    """Read the ``continuation`` section of ``config.json`` fresh."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    section = raw.get("continuation") if isinstance(raw, dict) else None
    section = section if isinstance(section, dict) else {}
    try:
        grace = int(section.get("grace_ms", DEFAULT_CONTINUATION_GRACE_MS))
    except (TypeError, ValueError):
        grace = DEFAULT_CONTINUATION_GRACE_MS
    if grace < 0:
        grace = 0
    return ContinuationConfig(grace_ms=grace)


class StallDetector:
    """Fire ``on_stall`` once when no bytes arrive for ``timeout_seconds``.

    Only one timer exists at a time: :meth:`feed` cancels the previous timer
    before arming a new one, and :meth:`stop` cancels it when the attempt ends.
    """

    def __init__(
        self,
        timeout_seconds: float,
        on_stall: Callable[[], None],
        timer_factory: Callable[[float, Callable[[], None]], object] = threading.Timer,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self._on_stall = on_stall
        self._timer_factory = timer_factory
        self._timer: object | None = None
        self.fired = False

    @property
    def active(self) -> bool:
        return self._timer is not None and not self.fired

    def start(self) -> None:
        self.fired = False
        self._arm()

    def feed(self) -> None:
        """Record that bytes arrived; restart the stall window."""
        self._arm()

    def stop(self) -> None:
        self._cancel()

    def _arm(self) -> None:
        self._cancel()
        timer = self._timer_factory(self.timeout_seconds, self._fire)
        try:
            setattr(timer, "daemon", True)
        except Exception:
            pass
        self._timer = timer
        start = getattr(timer, "start", None)
        if start is not None:
            start()

    def _cancel(self) -> None:
        timer, self._timer = self._timer, None
        cancel = getattr(timer, "cancel", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass

    def _fire(self) -> None:
        if self.fired:
            return
        self.fired = True
        self._timer = None
        self._on_stall()


class StallRetrier:
    """Drive one logical request through consecutive stall retries.

    ``stream_factory`` returns an iterator of chunks for a single attempt. It
    may raise :class:`StallTimeout` before yielding (no bytes at all) or in the
    middle of iteration (partial bytes then a stall).
    """

    def __init__(
        self,
        config: NetworkRetryConfig,
        on_delete_previous: Callable[[], None] | None = None,
        on_reattach: Callable[[], None] | None = None,
        on_exhausted: Callable[[int], None] | None = None,
        error_stream=None,
    ) -> None:
        self.config = config
        self.consecutive_failures = 0
        self._on_delete_previous = on_delete_previous or (lambda: None)
        self._on_reattach = on_reattach or (lambda: None)
        self._on_exhausted = on_exhausted
        self._error_stream = error_stream if error_stream is not None else sys.stderr

    def run(self, stream_factory: Callable[[], Iterator]) -> Iterator:
        while True:
            received_bytes = False
            try:
                for chunk in stream_factory():
                    received_bytes = True
                    yield chunk
            except StallTimeout:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.config.max_consecutive_retries:
                    print(
                        network_error_message(self.consecutive_failures),
                        file=self._error_stream,
                    )
                    if self._on_exhausted is not None:
                        self._on_exhausted(self.consecutive_failures)
                    return
                if received_bytes:
                    self._on_reattach()
                else:
                    self._on_delete_previous()
                continue
            self.consecutive_failures = 0
            return
