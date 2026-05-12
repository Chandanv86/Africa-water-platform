"""Safe Earth Engine geometry helpers."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)

DEFAULT_MAX_ERROR_M = 1


def ee_area_m2(geom: Any, *, max_error: int = DEFAULT_MAX_ERROR_M) -> Any:
    return geom.area(maxError=max_error)


def ee_buffer(geom: Any, distance_m: float, *, max_error: int = DEFAULT_MAX_ERROR_M) -> Any:
    try:
        return geom.buffer(distance_m, maxError=max_error)
    except Exception:
        logger.exception("Earth Engine geometry buffer failed")
        raise


def ee_bounds(geom: Any, *, max_error: int = DEFAULT_MAX_ERROR_M) -> Any:
    try:
        return geom.bounds(maxError=max_error)
    except Exception:
        logger.exception("Earth Engine geometry bounds failed")
        raise


def ee_buffer_bounds(
    geom: Any, distance_m: float, *, max_error: int = DEFAULT_MAX_ERROR_M
) -> Any:
    return ee_bounds(ee_buffer(geom, distance_m, max_error=max_error), max_error=max_error)
