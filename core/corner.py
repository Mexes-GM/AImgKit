from __future__ import annotations
import random
import threading
from typing import Optional

CORNERS = ["bottom-left", "bottom-right", "top-left", "top-right"]


class CornerSelector:
    """Thread-safe corner selector that avoids consecutive repetition."""

    def __init__(self) -> None:
        self._last: Optional[str] = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._last = None

    def choose(self) -> str:
        with self._lock:
            choices = [c for c in CORNERS if c != self._last]
            selected = random.choice(choices)
            self._last = selected
            return selected
