from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from shapely.geometry import shape, Point
from shapely.ops import transform as shapely_transform

from app.config import get_settings
from app.services.utils import geom_to_3857, point_to_3857

try:
    from pyproj import Transformer
except Exception:
    Transformer = None

_SETTINGS = get_settings()


@lru_cache(maxsize=1)
def _load_features() -> List[Dict[str, Any]]:
    base = Path(_SETTINGS.natural_earth_dir)
    files = [
        (base / "ne_10m_lakes.geojson", "Lake"),
        (base / "ne_10m_rivers.geojson", "River"),
        (base / "ne_10m_coastline.geojson", "Coastline"),
        (base / "ne_10m_reservoirs_lakes.geojson", "Reservoir"),
    ]
    features: List[Dict[str, Any]] = []
    for path, ftype in files:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for idx, feat in enumerate(data.get("features", [])):
                geom = shape(feat["geometry"])
                props = feat.get("properties") or {}
                name = (
                    props.get("name")
                    or props.get("name_en")
                    or props.get("NAME")
                    or props.get("featurecla")
                    or f"Unknown {ftype}"
                )
                features.append({
                    "name": str(name),
                    "type": ftype,
                    "feature_id": f"{path.stem}_{idx}",
                    "geometry_3857": geom_to_3857(geom),
                    "geometry_type": geom.geom_type,
                })
        except Exception:
            continue
    return features


def find_nearest_water_body(lat: float, lon: float) -> dict:
    features = _load_features()
    if not features:
        return {
            "name": "Unknown",
            "type": "Unknown",
            "distance_km": None,
            "source_dataset": "Natural Earth",
            "geometry_type": None,
            "feature_id": None,
        }

    pt = point_to_3857(lat, lon)
    best = None
    best_dist = None
    for feat in features:
        try:
            d = feat["geometry_3857"].distance(pt)
            if best_dist is None or d < best_dist:
                best_dist = d
                best = feat
        except Exception:
            continue

    if best is None:
        return {
            "name": "Unknown",
            "type": "Unknown",
            "distance_km": None,
            "source_dataset": "Natural Earth",
            "geometry_type": None,
            "feature_id": None,
        }

    return {
        "name": best["name"],
        "type": best["type"],
        "distance_km": round(float(best_dist) / 1000.0, 3) if best_dist is not None else None,
        "source_dataset": "Natural Earth",
        "geometry_type": best["geometry_type"],
        "feature_id": best["feature_id"],
    }
