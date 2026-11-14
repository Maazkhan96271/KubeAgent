"""Runtime SRE safety policy engine.

Policies are enforced server-side on every remediation approval, so the guard
rails hold even when the dashboard is bypassed with a direct API call.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class PolicyViolation(Exception):
    """Raised when an approval violates an active safety policy."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class PolicyState:
    """Mutable, in-process policy settings plus approval rate limiting."""

    def __init__(
        self,
        *,
        read_only: bool = False,
        protected_namespaces: list[str] | None = None,
        max_approvals_per_window: int = 2,
        window_seconds: int = 300,
    ) -> None:
        self.read_only = read_only
        self.protected_namespaces = set(protected_namespaces or ["production"])
        self.max_approvals_per_window = max(1, max_approvals_per_window)
        self.window_seconds = max(1, window_seconds)
        self._approvals: deque[float] = deque()
        self._lock = threading.Lock()
        self.remediations_success = 0
        self.remediations_failed = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            recent = [at for at in self._approvals if now - at < self.window_seconds]
            return {
                "read_only": self.read_only,
                "protected_namespaces": sorted(self.protected_namespaces),
                "max_approvals_per_window": self.max_approvals_per_window,
                "window_seconds": self.window_seconds,
                "approvals_in_window": len(recent),
                "remediations_success": self.remediations_success,
                "remediations_failed": self.remediations_failed,
            }

    def update(
        self,
        *,
        read_only: bool | None = None,
        protected_namespaces: list[str] | None = None,
        max_approvals_per_window: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if read_only is not None:
                self.read_only = bool(read_only)
            if protected_namespaces is not None:
                self.protected_namespaces = {
                    item.strip() for item in protected_namespaces if item.strip()
                }
            if max_approvals_per_window is not None:
                self.max_approvals_per_window = max(1, int(max_approvals_per_window))
        return self.snapshot()

    def check_approval(
        self, *, namespace: str, override: bool, approved_by: str
    ) -> None:
        """Raise :class:`PolicyViolation` when the approval must not proceed."""
        if self.read_only:
            self.remediations_failed += 1
            if not override:
                raise PolicyViolation(
                    403,
                    "Read-only mode is enabled: diagnostic analysis is allowed, remediation is not. "
                    "Disable read-only mode in Safety Policy or retry with override=true.",
                )
        if namespace in self.protected_namespaces and not override:
            self.remediations_failed += 1
            raise PolicyViolation(
                403,
                f"Namespace '{namespace}' is protected by policy: explicit override=true is required, "
                f"even from '{approved_by}'.",
            )
        with self._lock:
            now = time.time()
            while self._approvals and now - self._approvals[0] >= self.window_seconds:
                self._approvals.popleft()
            if len(self._approvals) >= self.max_approvals_per_window:
                self.remediations_failed += 1
                raise PolicyViolation(
                    429,
                    f"Rate limit hit: max {self.max_approvals_per_window} remediation approvals per "
                    f"{self.window_seconds}s.",
                )
            self._approvals.append(now)

    def record_success(self, namespace: str) -> None:  # pragma: no cover - trivial
        self.remediations_success += 1
