from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from pyproj import Geod, Transformer
from shapely.geometry import shape, mapping, Point, MultiPolygon, Polygon
from shapely.ops import transform as shapely_transform
from shapely.validation import explain_validity

from app.config import get_settings

_GEOD = Geod(ellps="WGS84")
_WGS84_TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_3857_TO_WGS84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        value = float(value)
    except Exception:
        value = low
    return max(low, min(high, value))


def africa_bbox_check(lat: float, lon: float) -> bool:
    s = get_settings()
    return s.africa_min_lat <= lat <= s.africa_max_lat and s.africa_min_lon <= lon <= s.africa_max_lon


def bbox_around_point(lat: float, lon: float, radius_km: float) -> list[float]:
    lat = float(lat)
    lon = float(lon)
    radius_km = max(float(radius_km), 0.1)
    d_lat = radius_km / 111.32
    denom = 111.32 * max(math.cos(math.radians(lat)), 0.01)
    d_lon = radius_km / denom
    return [lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat]


def km_between_points(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    _, _, dist_m = _GEOD.inv(float(lon1), float(lat1), float(lon2), float(lat2))
    return dist_m / 1000.0


def first_non_null(values: Iterable):
    for v in values:
        if v is not None:
            return v
    return None


def point_to_3857(lat: float, lon: float):
    return shapely_transform(_WGS84_TO_3857.transform, Point(float(lon), float(lat)))


def geom_to_3857(geom):
    return shapely_transform(_WGS84_TO_3857.transform, geom)


def geom_to_wgs84(geom):
    return shapely_transform(_3857_TO_WGS84.transform, geom)


def geojson_to_shapely(geojson_geom: dict):
    return shape(geojson_geom)


def wkt_or_geojson_to_polygon(payload: Any):
    if isinstance(payload, dict) and payload.get("type") in {"Polygon", "MultiPolygon"}:
        return shape(payload)
    if isinstance(payload, dict) and payload.get("type") == "Feature":
        return shape(payload["geometry"])
    if isinstance(payload, dict) and payload.get("geometry"):
        return shape(payload["geometry"])
    raise ValueError("Expected GeoJSON Polygon, MultiPolygon, or Feature geometry")


def polygon_area_km2(poly_wgs84) -> float:
    # area from geodesic is in square meters
    if poly_wgs84.is_empty:
        return 0.0
    polygons = list(poly_wgs84.geoms) if poly_wgs84.geom_type == "MultiPolygon" else [poly_wgs84]
    total_m2 = 0.0
    for polygon in polygons:
        lon, lat = polygon.exterior.coords.xy
        area_m2, _ = _GEOD.polygon_area_perimeter(lon, lat)
        holes_m2 = 0.0
        for interior in polygon.interiors:
            h_lon, h_lat = interior.coords.xy
            hole_m2, _ = _GEOD.polygon_area_perimeter(h_lon, h_lat)
            holes_m2 += abs(hole_m2)
        total_m2 += max(abs(area_m2) - holes_m2, 0.0)
    return total_m2 / 1e6


def polygon_perimeter_km(poly_wgs84) -> float:
    if poly_wgs84.is_empty:
        return 0.0
    polygons = list(poly_wgs84.geoms) if poly_wgs84.geom_type == "MultiPolygon" else [poly_wgs84]
    total_m = 0.0
    for polygon in polygons:
        lon, lat = polygon.exterior.coords.xy
        _, per_m = _GEOD.polygon_area_perimeter(lon, lat)
        total_m += abs(per_m)
        for interior in polygon.interiors:
            h_lon, h_lat = interior.coords.xy
            _, hole_per_m = _GEOD.polygon_area_perimeter(h_lon, h_lat)
            total_m += abs(hole_per_m)
    return total_m / 1000.0


def centroid_geojson(poly_wgs84):
    c = poly_wgs84.centroid
    return {"lat": float(c.y), "lon": float(c.x)}


def normalize_polygon(obj: Any):
    poly = wkt_or_geojson_to_polygon(obj)
    if poly.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("AOI must be a Polygon or MultiPolygon")
    if poly.is_empty:
        raise ValueError("AOI geometry is empty")
    if not poly.is_valid:
        repaired = poly.buffer(0)
        if repaired.is_empty:
            raise ValueError(f"AOI geometry is invalid and could not be repaired: {explain_validity(poly)}")
        poly = repaired
    if poly.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("AOI repair produced a non-polygon geometry")
    if poly.area <= 0:
        raise ValueError("AOI geometry has no measurable area")
    return poly


def normalize_aoi_geometry(obj: Any) -> tuple[Any, dict]:
    poly = normalize_polygon(obj)
    return poly, mapping(poly)


def is_valid_aoi_for_africa(poly) -> bool:
    minx, miny, maxx, maxy = poly.bounds
    s = get_settings()
    return not (maxx < s.africa_min_lon or minx > s.africa_max_lon or maxy < s.africa_min_lat or miny > s.africa_max_lat)


def percent(a: float, b: float) -> float:
    if not b:
        return 0.0
    return round((float(a) / float(b)) * 100.0, 3)


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v, default=None):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def make_download_filename(prefix: str, lat: float = None, lon: float = None, suffix: str = "tif") -> str:
    parts = [prefix]
    if lat is not None and lon is not None:
        parts.append(f"{round(float(lat),4)}_{round(float(lon),4)}")
    return "_".join(parts) + f".{suffix}"


def make_download_filename_for_aoi(prefix: str, label: Optional[str] = None, suffix: str = "tif") -> str:
    safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in (label or "aoi"))
    return f"{prefix}_{safe_label}.{suffix}"
