from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional, TypeVar

from cachetools import TTLCache

T = TypeVar("T")


_AOI_CACHE = TTLCache(maxsize=256, ttl=1800)
_TIMELINE_CACHE = TTLCache(maxsize=512, ttl=3600)
_RASTER_CACHE = TTLCache(maxsize=256, ttl=3600)
_STAC_CACHE = TTLCache(maxsize=1024, ttl=1800)
_AGRICULTURE_CACHE = TTLCache(maxsize=128, ttl=3600)


def stable_cache_key(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def cached(cache_name: str, key: str, factory: Callable[[], T]) -> T:
    cache = _cache_by_name(cache_name)
    if key in cache:
        return cache[key]
    value = factory()
    cache[key] = value
    return value


def get(cache_name: str, key: str, default: Optional[T] = None) -> Optional[T]:
    return _cache_by_name(cache_name).get(key, default)


def set(cache_name: str, key: str, value: T) -> T:
    _cache_by_name(cache_name)[key] = value
    return value


def _cache_by_name(name: str) -> TTLCache:
    if name == "aoi":
        return _AOI_CACHE
    if name == "timeline":
        return _TIMELINE_CACHE
    if name == "raster":
        return _RASTER_CACHE
    if name == "stac":
        return _STAC_CACHE
    if name == "agriculture":
        return _AGRICULTURE_CACHE
    raise ValueError(f"Unknown cache name: {name}")
