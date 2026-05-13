"""FEWS NET food-security classification — categorical IPC phase lookup.

Queries the public FEWS NET API for IPC-phase data and finds the admin zone
that contains (or is nearest to) the AOI centroid.

**Important**: this is a centroid-based approximation, NOT full AOI-level
food-security analysis.  The lookup method is always explicitly labeled in
the response.

The IPC phase (1–5) is stored as an integer classification and is NEVER
converted to a continuous score.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from shapely.geometry import Point, shape

from app.models.agriculture_models import FoodSecurityRecord, IPC_LABELS

logger = logging.getLogger(__name__)

_FEWSNET_API = "https://fdw.fews.net/api/ipcphase/"
_REQUEST_TIMEOUT = 15  # seconds
_PROCESSING_VERSION = "fewsnet-v1"

# Simple in-memory cache: {(lat_rounded, lon_rounded, year): FoodSecurityRecord}
_CACHE: Dict[tuple, FoodSecurityRecord] = {}
_CACHE_MAX = 512


def food_security_yearly(
    lat: float,
    lon: float,
    year: int,
) -> FoodSecurityRecord:
    """Retrieve the FEWS NET IPC-phase classification for a single year.

    This queries by the AOI centroid.  The result is explicitly labeled as
    ``lookup_method = "centroid-nearest zone (approximation)"`` so the caller
    knows it is NOT full AOI coverage.

    Parameters
    ----------
    lat, lon : float
        AOI centroid in WGS-84.
    year : int
        Calendar year to query.
    """

    # ── Cache check ──────────────────────────────────────────────────
    cache_key = (round(lat, 2), round(lon, 2), year)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # ── API call ─────────────────────────────────────────────────────
    try:
        resp = requests.get(
            _FEWSNET_API,
            params={"format": "json", "year": year, "fields": "simple"},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return _cache_and_return(cache_key, _unavailable(
                year, f"FEWS NET returned 404 for year {year}. Data may not be available.",
            ))
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return _unavailable(year, "FEWS NET API timed out. Try again later.")
    except requests.exceptions.ConnectionError:
        return _unavailable(year, "Could not connect to FEWS NET API.")
    except Exception as exc:
        logger.warning("FEWS NET request failed for %d: %s", year, exc)
        return _unavailable(year, f"FEWS NET API error: {exc}")

    # ── Parse features ───────────────────────────────────────────────
    features = _extract_features(data)
    if not features:
        return _cache_and_return(cache_key, _unavailable(
            year, f"No FEWS NET features returned for year {year}.",
        ))

    # ── Point-in-polygon lookup ──────────────────────────────────────
    point = Point(lon, lat)
    best_feature = None
    best_distance = float("inf")

    for feat in features:
        try:
            geom = shape(feat.get("geometry", {}))
            if geom.contains(point):
                best_feature = feat
                best_distance = 0.0
                break
            dist = geom.distance(point)
            if dist < best_distance:
                best_distance = dist
                best_feature = feat
        except Exception:
            continue

    if best_feature is None:
        return _cache_and_return(cache_key, _unavailable(
            year, "No FEWS NET zone found near the AOI centroid.",
        ))

    # ── Extract IPC phase ────────────────────────────────────────────
    props = best_feature.get("properties", {})
    phase_raw = _extract_phase(props)
    region_name = (
        props.get("admin1", "")
        or props.get("admin_name", "")
        or props.get("name", "")
        or "Unknown zone"
    )

    if phase_raw is None:
        return _cache_and_return(cache_key, FoodSecurityRecord(
            year=year,
            phase=None,
            phase_label=None,
            confidence=0.3,
            source_region=region_name,
            lookup_method="centroid-nearest zone (approximation — not full AOI coverage)",
            status="unavailable",
            notes=["FEWS NET zone found but IPC phase field is missing or unrecognized."],
        ))

    phase = int(phase_raw)
    phase = max(1, min(5, phase))  # clamp to valid range
    label = IPC_LABELS.get(phase)

    record = FoodSecurityRecord(
        year=year,
        phase=phase,
        phase_label=label,
        confidence=0.7 if best_distance == 0.0 else 0.4,
        source_region=region_name,
        lookup_method="centroid-nearest zone (approximation — not full AOI coverage)",
        status="ok",
        notes=[
            f"Zone: {region_name}. Distance to centroid: {best_distance:.4f}°.",
            "This is a centroid-based lookup, not full AOI-level analysis.",
        ],
    )
    return _cache_and_return(cache_key, record)


# ── Helpers ──────────────────────────────────────────────────────────────


def _extract_features(data: Any) -> List[Dict[str, Any]]:
    """Pull the feature list from various FEWS NET response shapes."""
    if isinstance(data, dict):
        if "features" in data:
            return data["features"]
        if "results" in data and isinstance(data["results"], list):
            return data["results"]
    if isinstance(data, list):
        return data
    return []


def _extract_phase(props: Dict[str, Any]) -> Optional[int]:
    """Try several known field names for the IPC phase."""
    for key in ("ipc_phase", "phase", "CS", "classification", "ipc"):
        val = props.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return None


def _unavailable(year: int, reason: str) -> FoodSecurityRecord:
    return FoodSecurityRecord(
        year=year,
        phase=None,
        phase_label=None,
        confidence=0.0,
        source_region=None,
        lookup_method="centroid-nearest zone (approximation — not full AOI coverage)",
        status="unavailable",
        notes=[reason],
    )


def _cache_and_return(key: tuple, record: FoodSecurityRecord) -> FoodSecurityRecord:
    if len(_CACHE) >= _CACHE_MAX:
        # Evict oldest entries (simple FIFO)
        keys_to_remove = list(_CACHE.keys())[: _CACHE_MAX // 4]
        for k in keys_to_remove:
            _CACHE.pop(k, None)
    _CACHE[key] = record
    return record
