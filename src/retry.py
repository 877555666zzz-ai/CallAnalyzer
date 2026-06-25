"""Простые ретраи с экспоненциальной паузой (§11: ретраи при сбоях движков/API)."""
from __future__ import annotations
import time
import functools
from typing import Callable, Any


def retry(attempts: int = 3, base_delay: float = 1.0, exc: tuple = (Exception,)) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            last = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except exc as e:  # noqa
                    last = e
                    if i < attempts - 1:
                        time.sleep(base_delay * (2 ** i))
            raise last
        return wrapper
    return deco
