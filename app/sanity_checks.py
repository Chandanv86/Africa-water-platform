from __future__ import annotations

import json

from app.main import app
from app.services.sensor_bands import (
    LANDSAT_OLI,
    LANDSAT_TM_ETM,
    SENTINEL2_SR,
    SENTINEL3_OLCI,
)
from app.services.utils import normalize_aoi_geometry


def run() -> dict:
    assert SENTINEL2_SR["red"] == "B4"
    assert SENTINEL2_SR["green"] == "B3"
    assert SENTINEL2_SR["nir"] == "B8"
    for bad in [f"B0{digit}" for digit in (3, 4, 8)]:
        assert bad not in SENTINEL2_SR.values()
    assert LANDSAT_OLI["green"].startswith("SR_B")
    assert LANDSAT_TM_ETM["swir1"].startswith("SR_B")
    assert SENTINEL3_OLCI["red_edge"].startswith("Oa")

    bowtie = {
        "type": "Polygon",
        "coordinates": [[
            [0.0, 0.0],
            [1.0, 1.0],
            [1.0, 0.0],
            [0.0, 1.0],
            [0.0, 0.0],
        ]],
    }
    _, repaired = normalize_aoi_geometry(bowtie)
    assert repaired["type"] in {"Polygon", "MultiPolygon"}

    routes = {route.path for route in app.routes}
    for route in {
        "/health",
        "/metadata",
        "/water/inspect",
        "/water/timeline",
        "/aoi/analyze",
        "/aoi/tif",
        "/tif/export",
        "/api/v1/water/inspect",
        "/api/v1/aoi/analyze",
    }:
        assert route in routes, f"Missing route: {route}"

    return {
        "status": "ok",
        "checks": ["band_mappings", "aoi_geometry_repair", "route_availability"],
        "route_count": len(routes),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
