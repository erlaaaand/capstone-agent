# core/logger.py
from __future__ import annotations

import logging
import sys
from functools import lru_cache

_FMT  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def _setup() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(_FMT, datefmt=_DATE))
    root.addHandler(h)
    root.setLevel(logging.INFO)


_setup()


@lru_cache(maxsize=None)
def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
