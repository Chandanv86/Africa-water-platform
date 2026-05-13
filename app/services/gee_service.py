from __future__ import annotations

import os
import tempfile
import logging
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.config import get_settings
from app.services.ee_geometry import ee_area_m2, ee_buffer_bounds
from app.services.sensor_bands import (
    CHIRPS,
    SENTINEL1_GRD,
    SENTINEL2_SR,
    SENTINEL3_OLCI,
    TERRACLIMATE,
    JRC_GSW,
    landsat_oli_ndsi,
    landsat_tm_etm_ndsi,
    mask_sentinel2_sr_scl,
    s2_mndwi,
    s2_ndsi,
    s2_ndti,
    s2_ndwi,
    s3_olci_ndci,
)
from app.services.utils import bbox_around_point, clamp, ensure_dir, normalize_aoi_geometry

try:
    import ee
except Exception:
    ee = None  # type: ignore

_SETTINGS = get_settings()
_EE_READY = False
_EE_ERROR = None
logger = logging.getLogger(__name__)


def _key_file_from_raw_json(json_text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="gee_sa_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json_text)
    return path


def _clean_env_path(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    raw = str(value).strip().strip('"').strip("'")
    if not raw:
        return None

    # python-dotenv decodes double-quoted Windows paths, so "\a" can become a bell character.
    control_map = {
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    for bad, replacement in control_map.items():
        raw = raw.replace(bad, replacement)
    return Path(raw).expanduser()


def _json_text_is_service_account(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("type") == "service_account" and bool(parsed.get("private_key"))


def initialize_earth_engine() -> bool:
    global _EE_READY, _EE_ERROR
    if _EE_READY:
        return True
    if ee is None:
        _EE_ERROR = "earthengine-api is not installed"
        return False

    email = _SETTINGS.gee_service_account_email
    key_path = _SETTINGS.gee_service_account_key_path
    key_json = _SETTINGS.gee_service_account_json
    project_id = _SETTINGS.gee_project_id

    if not email:
        _EE_ERROR = "GEE_SERVICE_ACCOUNT_EMAIL is not configured"
        return False

    try:
        key_path_obj = _clean_env_path(key_path)
        key_json_path_obj = _clean_env_path(key_json)
        if key_path_obj and key_path_obj.exists():
            credentials = ee.ServiceAccountCredentials(email, key_file=str(key_path_obj))
        elif key_json and key_json_path_obj and key_json_path_obj.exists():
            credentials = ee.ServiceAccountCredentials(email, key_file=str(key_json_path_obj))
        elif key_json and key_json.strip() and _json_text_is_service_account(key_json):
            credentials = ee.ServiceAccountCredentials(email, key_file=_key_file_from_raw_json(key_json))
        elif key_json and key_json.strip():
            _EE_ERROR = (
                "GEE_SERVICE_ACCOUNT_JSON is set but is neither valid service-account JSON nor an existing key-file path. "
                "Unset it or point GEE_SERVICE_ACCOUNT_KEY_PATH to a valid service-account JSON file."
            )
            return False
        else:
            _EE_ERROR = "GEE_SERVICE_ACCOUNT_KEY_PATH or GEE_SERVICE_ACCOUNT_JSON is not configured"
            return False

        ee.Initialize(credentials=credentials, project=project_id)
        _EE_READY = True
        _EE_ERROR = None
        return True
    except Exception as e:
        _EE_ERROR = str(e)
        logger.exception("Earth Engine initialization failed")
        return False


def auth_status() -> Dict[str, Any]:
    key_path_obj = _clean_env_path(_SETTINGS.gee_service_account_key_path)
    json_path_obj = _clean_env_path(_SETTINGS.gee_service_account_json)
    return {
        "ready": initialize_earth_engine(),
        "error": _EE_ERROR,
        "configured": bool(_SETTINGS.gee_service_account_email and (_SETTINGS.gee_service_account_key_path or _SETTINGS.gee_service_account_json)),
        "auth_source": "key_path" if key_path_obj and key_path_obj.exists() else ("json_path" if json_path_obj and json_path_obj.exists() else ("raw_json" if _SETTINGS.gee_service_account_json and _json_text_is_service_account(_SETTINGS.gee_service_account_json) else None)),
        "key_path_exists": bool(key_path_obj and key_path_obj.exists()),
    }


def is_available() -> bool:
    return initialize_earth_engine()


def _pt(lat: float, lon: float):
    return ee.Geometry.Point([float(lon), float(lat)])


def _geom_from_geojson(geometry: dict):
    return ee.Geometry(geometry)


def safe_getinfo(obj: Any, fallback: Any = None, label: str = "earth_engine") -> Any:
    try:
        if obj is None:
            return fallback
        return obj.getInfo()
    except Exception as exc:
        logger.warning("Earth Engine getInfo failed for %s: %s", label, exc)
        return fallback


def safe_reduce_mean(img, geom, scale: int = 30, fallback: Optional[Dict[str, Any]] = None, label: str = "reduce_mean") -> Dict[str, Any]:
    try:
        result = img.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=scale,
            bestEffort=True,
            maxPixels=1e8,
        )
        return safe_getinfo(result, fallback or {}, label=label) or {}
    except Exception as exc:
        logger.warning("Earth Engine reduceRegion(mean) failed for %s: %s", label, exc)
        return fallback or {}


def safe_reduce_sum(img, geom, scale: int = 30, fallback: Optional[Dict[str, Any]] = None, label: str = "reduce_sum") -> Dict[str, Any]:
    try:
        result = img.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=scale,
            bestEffort=True,
            maxPixels=1e8,
        )
        return safe_getinfo(result, fallback or {}, label=label) or {}
    except Exception as exc:
        logger.warning("Earth Engine reduceRegion(sum) failed for %s: %s", label, exc)
        return fallback or {}


def safe_reduce_first(img, geom, scale: int = 30, fallback: Optional[Dict[str, Any]] = None, label: str = "reduce_first") -> Dict[str, Any]:
    try:
        result = img.reduceRegion(
            reducer=ee.Reducer.first(),
            geometry=geom,
            scale=scale,
            bestEffort=True,
            maxPixels=1e8,
        )
        return safe_getinfo(result, fallback or {}, label=label) or {}
    except Exception as exc:
        logger.warning("Earth Engine reduceRegion(first) failed for %s: %s", label, exc)
        return fallback or {}


def safe_area_m2(geom, fallback: Optional[float] = None, label: str = "geometry_area") -> Optional[float]:
    try:
        value = safe_getinfo(ee_area_m2(geom), fallback, label=label)
        return None if value is None else float(value)
    except Exception as exc:
        logger.warning("Earth Engine geometry area failed for %s: %s", label, exc)
        return fallback


def safe_first_image(collection, label: str = "first_image"):
    try:
        size = int(safe_getinfo(collection.size(), 0, label=f"{label}_size") or 0)
        if size <= 0:
            return None
        return ee.Image(collection.first())
    except Exception as exc:
        logger.warning("Earth Engine first image failed for %s: %s", label, exc)
        return None


def safe_latest_timestamp(collection, fallback: Any = None, label: str = "latest_timestamp") -> Any:
    try:
        return safe_getinfo(
            collection.sort("system:time_start", False).first().date().format("YYYY-MM-dd"),
            fallback,
            label=label,
        )
    except Exception as exc:
        logger.warning("Earth Engine latest timestamp failed for %s: %s", label, exc)
        return fallback


def safe_feature_rows(feature_collection, fallback: Optional[List[Dict[str, Any]]] = None, label: str = "feature_rows") -> List[Dict[str, Any]]:
    info = safe_getinfo(feature_collection, {"features": []}, label=label) or {"features": []}
    features = info.get("features", []) if isinstance(info, dict) else []
    rows: List[Dict[str, Any]] = []
    for feature in features:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if isinstance(props, dict):
            rows.append(props)
    return rows or fallback or []


def _reduce_first(img, geom, scale: int = 30) -> Dict[str, Any]:
    return safe_reduce_first(img, geom, scale=scale)


def _reduce_mean(img, geom, scale: int = 30) -> Dict[str, Any]:
    return safe_reduce_mean(img, geom, scale=scale)


def _reduce_sum(img, geom, scale: int = 30) -> Dict[str, Any]:
    return safe_reduce_sum(img, geom, scale=scale)


def _pick_band_value(vals: Dict[str, Any], *names: str):
    lower = {str(k).lower(): v for k, v in vals.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    if vals:
        return next(iter(vals.values()))
    return None


def jrc_point_context(lat: float, lon: float) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    geom = _pt(lat, lon)
    img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select(
        [JRC_GSW[0], JRC_GSW[3], JRC_GSW[5], JRC_GSW[6]]
    )
    vals = _reduce_first(img, geom, scale=30)

    occurrence = vals.get("occurrence")
    seasonality = vals.get("seasonality")
    transition = vals.get("transition")
    max_extent = vals.get("max_extent")

    if occurrence is None:
        water_class = "unknown"
    elif occurrence >= 90:
        water_class = "permanent"
    elif occurrence >= 20:
        water_class = "seasonal_or_wetland"
    else:
        water_class = "unlikely_water"

    return {
        "status": "ok",
        "occurrence_pct": occurrence,
        "seasonality_index": seasonality,
        "transition_class": transition,
        "max_extent": max_extent,
        "water_class": water_class,
        "source_dataset": "JRC/GSW1_4/GlobalSurfaceWater",
        "data_timestamp": "1984-2021",
        "metrics": vals,
    }


def jrc_aoi_context(geom) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select(
        [JRC_GSW[0], JRC_GSW[3], JRC_GSW[5], JRC_GSW[6]]
    )
    vals = _reduce_mean(img, geom, scale=30)
    return {
        "status": "ok",
        "occurrence_pct": vals.get("occurrence"),
        "seasonality_index": vals.get("seasonality"),
        "transition_class": vals.get("transition"),
        "max_extent": vals.get("max_extent"),
        "water_class": "mixed",
        "source_dataset": "JRC/GSW1_4/GlobalSurfaceWater",
        "data_timestamp": "1984-2021",
        "metrics": vals,
    }


def yearly_history_point(lat: float, lon: float, start_year: int = 1984, end_year: int = 2021) -> List[Dict[str, Any]]:
    return yearly_history_aoi(_pt(lat, lon), start_year=start_year, end_year=end_year)


def yearly_history_aoi(geom, start_year: int = 1984, end_year: int = 2021) -> List[Dict[str, Any]]:
    if not initialize_earth_engine():
        return []

    try:
        col = ee.ImageCollection("JRC/GSW1_4/YearlyHistory").filter(ee.Filter.calendarRange(start_year, end_year, "year"))
    except Exception:
        return []

    def _year_feature(img):
        img = ee.Image(img)
        year = ee.Date(img.get("system:time_start")).get("year")
        band = ee.String(img.bandNames().get(0))
        vals = img.reduceRegion(
            reducer=ee.Reducer.mode(),
            geometry=geom,
            scale=30,
            bestEffort=True,
            maxPixels=1e8,
        )
        return ee.Feature(None, {"year": year, "water_class": vals.get(band)})

    rows = safe_feature_rows(ee.FeatureCollection(col.map(_year_feature)), label="yearly_history_rows")
    out: List[Dict[str, Any]] = []
    for row in rows:
        raw_val = row.get("water_class")
        year = row.get("year")
        if raw_val is None or year is None:
            continue
        raw_val = int(raw_val)
        out.append({
            "year": int(year),
            "water_class": raw_val,
            "permanent": raw_val == 3,
            "seasonal": raw_val == 2,
            "occurrence_pct": None,
            "value": raw_val,
        })
    return sorted(out, key=lambda item: item["year"])


def timeline_context_point(lat: float, lon: float, buffer_km: float = 5.0) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {
            "yearly": [],
            "monthly": [],
            "flood_history": [],
            "flood_yearly_trends": [],
            "turbidity_trends": [],
            "turbidity_yearly_trends": [],
            "chlorophyll_trends": [],
            "chlorophyll_yearly_trends": [],
            "soil_moisture_trends": [],
            "soil_moisture_yearly_trends": [],
            "drought_trends": [],
            "drought_yearly_trends": [],
            "glacier_trends": [],
            "glacier_yearly_trends": [],
            "water_quality_trends": [],
            "water_quality_yearly_trends": [],
            "anomaly_trends": [],
            "anomaly_yearly_trends": [],
            "status": "permission_denied",
            "reason": _EE_ERROR,
        }
    return timeline_context_aoi(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000), yearly_history_point(lat, lon))


def timeline_context_aoi(geom, yearly: Optional[List[Dict[str, Any]]] = None, months: int = 0, area_km2: Optional[float] = None) -> Dict[str, Any]:
    yearly = yearly if yearly is not None else yearly_history_aoi(geom)
    if not initialize_earth_engine():
        return {
            "yearly": yearly,
            "monthly": [],
            "flood_history": [],
            "flood_yearly_trends": [],
            "turbidity_trends": [],
            "turbidity_yearly_trends": [],
            "chlorophyll_trends": [],
            "chlorophyll_yearly_trends": [],
            "soil_moisture_trends": [],
            "soil_moisture_yearly_trends": [],
            "drought_trends": [],
            "drought_yearly_trends": [],
            "glacier_trends": [],
            "glacier_yearly_trends": [],
            "water_quality_trends": [],
            "water_quality_yearly_trends": [],
            "anomaly_trends": [],
            "anomaly_yearly_trends": [],
            "status": "permission_denied",
            "reason": _EE_ERROR,
        }

    import datetime as dt

    current_year = dt.datetime.utcnow().year
    years = list(range(current_year - 4, current_year + 1))
    trend_scale = 2000 if (area_km2 or 0) > 100000 else (1000 if (area_km2 or 0) > 20000 else 250)
    s1_scale = max(30, trend_scale)
    s2_scale = max(20, trend_scale)
    s3_scale = max(300, trend_scale)
    climate_scale = max(5000, trend_scale)
    area_m2 = safe_area_m2(geom, None, label="timeline_aoi_area") or 0.0

    def _row_value(row: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = row.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
        return None if value is None else round(float(value), digits)

    def _ee_clamp(value):
        return ee.Number(value).max(0).min(1)

    def _null_or_clamped(value):
        return ee.Algorithms.If(
            ee.Algorithms.IsEqual(value, None),
            None,
            _ee_clamp(value),
        )

    def _null_or_number(value):
        return ee.Algorithms.If(ee.Algorithms.IsEqual(value, None), None, ee.Number(value))

    def _year_feature(year_value):
        year = ee.Number(year_value).toInt()
        start = ee.Date.fromYMD(year, 1, 1)
        end = start.advance(1, "year")
        area = ee.Number(max(float(area_m2 or 0.0), 1.0))

        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(geom)
            .filterDate(start, end)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", SENTINEL1_GRD["vv"]))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", SENTINEL1_GRD["vh"]))
        )
        s1_count = s1.size()
        empty_s1 = ee.Image.constant([0, 0]).rename([SENTINEL1_GRD["vv"], SENTINEL1_GRD["vh"]]).updateMask(ee.Image.constant(0))
        sar = ee.Image(ee.Algorithms.If(s1_count.gt(0), s1.median(), empty_s1)).clip(geom)
        vv_img = sar.select(SENTINEL1_GRD["vv"])
        vh_img = sar.select(SENTINEL1_GRD["vh"])
        vv_raw = vv_img.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=s1_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get(SENTINEL1_GRD["vv"])
        wet_area_raw = vv_img.lt(-17).And(vh_img.lt(-22)).multiply(ee.Image.pixelArea()).rename("wet").reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=s1_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get("wet")
        wet_area = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(wet_area_raw, None), 0, wet_area_raw))
        wet_fraction = wet_area.divide(area)
        flood_signal = ee.Algorithms.If(s1_count.gt(0), _ee_clamp(wet_fraction.multiply(4.0)), None)
        moisture_proxy = ee.Algorithms.If(
            ee.Algorithms.IsEqual(vv_raw, None),
            None,
            _ee_clamp(ee.Number(vv_raw).add(22.0).divide(12.0)),
        )
        soil_stress = ee.Algorithms.If(
            ee.Algorithms.IsEqual(moisture_proxy, None),
            None,
            _ee_clamp(ee.Number(1).subtract(ee.Number(moisture_proxy))),
        )

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(geom)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 70))
            .map(_mask_s2_sr)
        )
        s2_count = s2.size()
        empty_s2 = ee.Image.constant([0, 0, 0, 0]).rename([
            SENTINEL2_SR["green"],
            SENTINEL2_SR["red"],
            SENTINEL2_SR["nir"],
            SENTINEL2_SR["swir1"],
        ]).updateMask(ee.Image.constant(0))
        s2_img = ee.Image(ee.Algorithms.If(s2_count.gt(0), s2.median(), empty_s2)).clip(geom)
        water = s2_mndwi(s2_img).gt(0.0).Or(s2_ndwi(s2_img).gt(0.0))
        ndti = s2_ndti(s2_img)
        ndti_raw = ndti.updateMask(water).reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=s2_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get("NDTI")
        turbidity_proxy = ee.Algorithms.If(
            ee.Algorithms.IsEqual(ndti_raw, None),
            None,
            _ee_clamp(ee.Number(ndti_raw).add(0.2).divide(0.6)),
        )
        snow_area_raw = s2_ndsi(s2_img).gt(0.4).multiply(ee.Image.pixelArea()).rename("snow").reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=s2_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get("snow")
        snow_area = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(snow_area_raw, None), 0, snow_area_raw))
        snow_fraction = ee.Algorithms.If(s2_count.gt(0), _ee_clamp(snow_area.divide(area)), None)

        s3 = ee.ImageCollection("COPERNICUS/S3/OLCI").filterBounds(geom).filterDate(start, end)
        s3_count = s3.size()
        empty_s3 = ee.Image.constant([0, 0]).rename([SENTINEL3_OLCI["red_edge"], SENTINEL3_OLCI["red"]]).updateMask(ee.Image.constant(0))
        s3_img = ee.Image(ee.Algorithms.If(s3_count.gt(0), s3.median(), empty_s3)).clip(geom)
        ndci_raw = _olci_ndci(s3_img).reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=s3_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get("NDCI")
        chlorophyll_proxy = ee.Algorithms.If(
            ee.Algorithms.IsEqual(ndci_raw, None),
            None,
            _ee_clamp(ee.Number(ndci_raw).add(0.15).divide(0.5)),
        )

        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(geom).filterDate(start, end)
        precip_raw = chirps.select(CHIRPS["precipitation"]).sum().reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=climate_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get(CHIRPS["precipitation"])
        tc = ee.ImageCollection("IDAHO_EPSCOR/TERRACLIMATE").filterBounds(geom).filterDate(start, end)
        tc_count = tc.size()
        empty_pdsi = ee.Image.constant(0).rename(TERRACLIMATE["pdsi"]).updateMask(ee.Image.constant(0))
        pdsi_img = ee.Image(ee.Algorithms.If(tc_count.gt(0), _terraclimate_pdsi(tc.mean()), empty_pdsi))
        pdsi_raw = pdsi_img.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=climate_scale,
            bestEffort=True,
            maxPixels=1e8,
        ).get(TERRACLIMATE["pdsi"])
        drought_stress = ee.Algorithms.If(
            ee.Algorithms.IsEqual(pdsi_raw, None),
            None,
            _ee_clamp(ee.Number(0.5).subtract(ee.Number(pdsi_raw).divide(10.0))),
        )

        return ee.Feature(None, {
            "year": year,
            "flood_signal": flood_signal,
            "flood_extent_fraction": ee.Algorithms.If(s1_count.gt(0), wet_fraction, None),
            "s1_scene_count": s1_count,
            "moisture_proxy": moisture_proxy,
            "soil_moisture_proxy": moisture_proxy,
            "soil_stress": soil_stress,
            "turbidity_proxy": turbidity_proxy,
            "ndti": _null_or_number(ndti_raw),
            "s2_scene_count": s2_count,
            "snow_ice_fraction": snow_fraction,
            "chlorophyll_proxy": chlorophyll_proxy,
            "ndci": _null_or_number(ndci_raw),
            "s3_scene_count": s3_count,
            "drought_stress": drought_stress,
            "pdsi": _null_or_number(pdsi_raw),
            "precip_mm": _null_or_number(precip_raw),
            "terraclimate_count": tc_count,
        })

    rows = safe_feature_rows(
        ee.FeatureCollection(ee.List(years).map(_year_feature)),
        [],
        label="timeline_yearly_batched",
    )

    flood_yearly_trends: List[Dict[str, Any]] = []
    turbidity_yearly_trends: List[Dict[str, Any]] = []
    chlorophyll_yearly_trends: List[Dict[str, Any]] = []
    soil_moisture_yearly_trends: List[Dict[str, Any]] = []
    drought_yearly_trends: List[Dict[str, Any]] = []
    glacier_yearly_trends: List[Dict[str, Any]] = []
    water_quality_yearly_trends: List[Dict[str, Any]] = []
    anomaly_yearly_trends: List[Dict[str, Any]] = []
    failure_notes: List[str] = []

    if not rows:
        failure_notes.append("Batched yearly EO trend reducer returned no rows.")

    for row in sorted(rows, key=lambda item: item.get("year") or 0):
        year = int(row.get("year"))
        anomaly_inputs: List[float] = []

        flood_signal = _row_value(row, "flood_signal")
        if flood_signal is not None:
            flood_yearly_trends.append({
                "year": year,
                "flood_signal": _round(flood_signal),
                "value": _round(flood_signal),
                "extent_fraction": _round(_row_value(row, "flood_extent_fraction"), 5),
                "scene_count": int(row.get("s1_scene_count") or 0),
                "dataset": "Sentinel-1 GRD",
            })
            anomaly_inputs.append(flood_signal)

        moisture_proxy = _row_value(row, "moisture_proxy", "soil_moisture_proxy")
        if moisture_proxy is not None:
            stress = _row_value(row, "soil_stress")
            soil_moisture_yearly_trends.append({
                "year": year,
                "moisture_proxy": _round(moisture_proxy),
                "soil_moisture_proxy": _round(moisture_proxy),
                "stress": _round(stress),
                "value": _round(moisture_proxy),
                "scene_count": int(row.get("s1_scene_count") or 0),
                "dataset": "Sentinel-1 SAR proxy",
            })
            if stress is not None:
                anomaly_inputs.append(stress)

        turbidity_proxy = _row_value(row, "turbidity_proxy")
        if turbidity_proxy is not None:
            turbidity_yearly_trends.append({
                "year": year,
                "turbidity_proxy": _round(turbidity_proxy),
                "ndti": _round(_row_value(row, "ndti"), 4),
                "value": _round(turbidity_proxy),
                "scene_count": int(row.get("s2_scene_count") or 0),
                "dataset": "Sentinel-2 SR Harmonized",
            })
            anomaly_inputs.append(turbidity_proxy)

        snow_fraction = _row_value(row, "snow_ice_fraction")
        if snow_fraction is not None:
            glacier_yearly_trends.append({
                "year": year,
                "snow_ice_fraction": _round(snow_fraction, 5),
                "value": _round(snow_fraction, 5),
                "scene_count": int(row.get("s2_scene_count") or 0),
                "dataset": "Sentinel-2 NDSI",
            })

        chlorophyll_proxy = _row_value(row, "chlorophyll_proxy")
        if chlorophyll_proxy is not None:
            chlorophyll_yearly_trends.append({
                "year": year,
                "chlorophyll_proxy": _round(chlorophyll_proxy),
                "ndci": _round(_row_value(row, "ndci"), 4),
                "value": _round(chlorophyll_proxy),
                "scene_count": int(row.get("s3_scene_count") or 0),
                "dataset": "Sentinel-3 OLCI",
            })
            anomaly_inputs.append(chlorophyll_proxy)

        drought_stress = _row_value(row, "drought_stress")
        precip = _row_value(row, "precip_mm")
        pdsi = _row_value(row, "pdsi")
        if drought_stress is not None or precip is not None:
            drought_yearly_trends.append({
                "year": year,
                "drought_stress": _round(drought_stress),
                "pdsi": _round(pdsi, 3),
                "precip_mm": _round(precip, 2),
                "value": _round(drought_stress),
                "dataset": "CHIRPS + TerraClimate",
            })
            if drought_stress is not None:
                anomaly_inputs.append(drought_stress)

        turbidity_row = next((item for item in turbidity_yearly_trends if item["year"] == year), None)
        chlorophyll_row = next((item for item in chlorophyll_yearly_trends if item["year"] == year), None)
        turbidity_value = _row_value(turbidity_row or {}, "turbidity_proxy")
        chlorophyll_value = _row_value(chlorophyll_row or {}, "chlorophyll_proxy")
        if turbidity_value is not None or chlorophyll_value is not None:
            values = [v for v in [turbidity_value, chlorophyll_value] if v is not None]
            degradation = sum(values) / len(values)
            water_quality_yearly_trends.append({
                "year": year,
                "quality_proxy": _round(1.0 - degradation),
                "water_quality_proxy": _round(1.0 - degradation),
                "degradation_indicator": _round(degradation),
                "value": _round(1.0 - degradation),
                "dataset": "Sentinel-2 + Sentinel-3 derived proxy",
            })

        if anomaly_inputs:
            anomaly = max(anomaly_inputs)
            anomaly_yearly_trends.append({
                "year": year,
                "value": _round(anomaly),
                "anomaly": _round(anomaly),
            })

    status = "ok" if anomaly_yearly_trends else ("partial" if rows else "insufficient_data")

    return {
        "yearly": yearly,
        "monthly": [],
        "flood_history": flood_yearly_trends,
        "flood_yearly_trends": flood_yearly_trends,
        "turbidity_trends": turbidity_yearly_trends,
        "turbidity_yearly_trends": turbidity_yearly_trends,
        "chlorophyll_trends": chlorophyll_yearly_trends,
        "chlorophyll_yearly_trends": chlorophyll_yearly_trends,
        "soil_moisture_trends": soil_moisture_yearly_trends,
        "soil_moisture_yearly_trends": soil_moisture_yearly_trends,
        "drought_trends": drought_yearly_trends,
        "drought_yearly_trends": drought_yearly_trends,
        "glacier_trends": glacier_yearly_trends,
        "glacier_yearly_trends": glacier_yearly_trends,
        "water_quality_trends": water_quality_yearly_trends,
        "water_quality_yearly_trends": water_quality_yearly_trends,
        "anomaly_trends": anomaly_yearly_trends,
        "anomaly_yearly_trends": anomaly_yearly_trends,
        "scale_note": f"Yearly EO trends use scale {trend_scale} m for AOI stability; missing years indicate unavailable observations, not zero.",
        "status": status,
        "reason": "; ".join(failure_notes) if failure_notes else None,
    }


def _safe_collection(collection_name: str):
    try:
        return ee.ImageCollection(collection_name)
    except Exception:
        return None


def flood_point_context(lat: float, lon: float, buffer_km: float = 5.0, pre_days: int = 30, post_days: int = 12) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    post_start = end.advance(-post_days, "day")
    pre_end = post_start
    pre_start = end.advance(-(pre_days + post_days), "day")
    aoi = ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000)

    col = ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(aoi).filter(ee.Filter.eq("instrumentMode", "IW"))
    col = col.filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    col = col.filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))

    pre_col = col.filterDate(pre_start, pre_end)
    post_col = col.filterDate(post_start, end)
    pre_n = int(safe_getinfo(pre_col.size(), 0, label="flood_point_pre_count") or 0)
    post_n = int(safe_getinfo(post_col.size(), 0, label="flood_point_post_count") or 0)

    if pre_n == 0 or post_n == 0:
        return {
            "status": "insufficient_data",
            "flooded": False,
            "pre_window_days": pre_days,
            "post_window_days": post_days,
            "confidence": 0.0,
            "timestamp": None,
            "metrics": {"pre_scene_count": pre_n, "post_scene_count": post_n},
            "notes": ["No suitable Sentinel-1 scenes found in the requested windows."],
        }

    pre = pre_col.median().clip(aoi)
    post = post_col.median().clip(aoi)

    pre_vv = pre.select(SENTINEL1_GRD["vv"])
    post_vv = post.select(SENTINEL1_GRD["vv"])
    pre_vh = pre.select(SENTINEL1_GRD["vh"])
    post_vh = post.select(SENTINEL1_GRD["vh"])

    delta_vv = post_vv.subtract(pre_vv)
    delta_vh = post_vh.subtract(pre_vh)

    flood_mask = post_vv.lt(-17).And(post_vh.lt(-22)).And(delta_vv.lt(-1.5))
    flood_area_m2 = safe_reduce_sum(flood_mask.multiply(ee.Image.pixelArea()), aoi, scale=30, label="flood_point_area")
    flood_area_val = next(iter(flood_area_m2.values()), 0.0) if flood_area_m2 else 0.0

    area_m2 = safe_area_m2(aoi, 0.0, label="flood_point_aoi_area")
    flooded_pct = float(flood_area_val) / float(area_m2) if area_m2 else 0.0
    severity = clamp((flooded_pct * 3.0) + (0.5 if abs(float(_pick_band_value(_reduce_first(delta_vv, _pt(lat, lon), 30), SENTINEL1_GRD["vv"]) or 0.0)) > 1.5 else 0.0))
    flooded = severity >= 0.25

    return {
        "status": "ok",
        "score": round(float(severity), 3),
        "value": round(float(flood_area_val or 0.0) / 1e6, 4),
        "severity": round(float(severity), 3),
        "flooded": flooded,
        "flood_extent_km2": round(float(flood_area_val or 0.0) / 1e6, 4),
        "inundation_severity": round(float(severity), 3),
        "confidence": round(clamp(0.35 + min(0.45, post_n / 10.0) + min(0.2, pre_n / 10.0)), 2),
        "anomaly_score": round(clamp(severity), 3),
        "pre_window_days": pre_days,
        "post_window_days": post_days,
        "timestamp": safe_getinfo(post_col.first().date().format("YYYY-MM-dd"), None, label="flood_point_timestamp"),
        "metrics": {
            "pre_scene_count": pre_n,
            "post_scene_count": post_n,
            "pre_vv_db": _pick_band_value(_reduce_first(pre_vv, _pt(lat, lon), 30), SENTINEL1_GRD["vv"]),
            "post_vv_db": _pick_band_value(_reduce_first(post_vv, _pt(lat, lon), 30), SENTINEL1_GRD["vv"]),
            "pre_vh_db": _pick_band_value(_reduce_first(pre_vh, _pt(lat, lon), 30), SENTINEL1_GRD["vh"]),
            "post_vh_db": _pick_band_value(_reduce_first(post_vh, _pt(lat, lon), 30), SENTINEL1_GRD["vh"]),
            "delta_vv_db": _pick_band_value(_reduce_first(delta_vv, _pt(lat, lon), 30), SENTINEL1_GRD["vv"]),
            "delta_vh_db": _pick_band_value(_reduce_first(delta_vh, _pt(lat, lon), 30), SENTINEL1_GRD["vh"]),
        },
        "notes": [
            "Flood detection uses Sentinel-1 GRD backscatter change and low-backscatter thresholding.",
            "This is an EO indicator, not a hydraulic flood-depth model.",
        ],
    }


def flood_aoi_context(geom, buffer_km: float = 5.0, pre_days: int = 30, post_days: int = 12) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    post_start = end.advance(-post_days, "day")
    pre_end = post_start
    pre_start = end.advance(-(pre_days + post_days), "day")

    aoi = geom
    col = ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(aoi).filter(ee.Filter.eq("instrumentMode", "IW"))
    col = col.filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    col = col.filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))

    pre_col = col.filterDate(pre_start, pre_end)
    post_col = col.filterDate(post_start, end)
    pre_n = int(safe_getinfo(pre_col.size(), 0, label="flood_aoi_pre_count") or 0)
    post_n = int(safe_getinfo(post_col.size(), 0, label="flood_aoi_post_count") or 0)

    if pre_n == 0 or post_n == 0:
        return {
            "status": "insufficient_data",
            "flooded": False,
            "pre_window_days": pre_days,
            "post_window_days": post_days,
            "confidence": 0.0,
            "timestamp": None,
            "metrics": {"pre_scene_count": pre_n, "post_scene_count": post_n},
            "notes": ["No suitable Sentinel-1 scenes found in the requested windows."],
        }

    pre = pre_col.median().clip(aoi)
    post = post_col.median().clip(aoi)

    pre_vv = pre.select(SENTINEL1_GRD["vv"])
    post_vv = post.select(SENTINEL1_GRD["vv"])
    pre_vh = pre.select(SENTINEL1_GRD["vh"])
    post_vh = post.select(SENTINEL1_GRD["vh"])
    delta_vv = post_vv.subtract(pre_vv)
    delta_vh = post_vh.subtract(pre_vh)

    flood_mask = post_vv.lt(-17).And(post_vh.lt(-22)).And(delta_vv.lt(-1.5))
    flood_area_m2 = safe_reduce_sum(flood_mask.multiply(ee.Image.pixelArea()), aoi, scale=30, label="flood_aoi_area")
    flood_area_val = next(iter(flood_area_m2.values()), 0.0) if flood_area_m2 else 0.0

    area_m2 = safe_area_m2(aoi, 0.0, label="flood_aoi_total_area")
    flooded_pct = float(flood_area_val) / float(area_m2) if area_m2 else 0.0
    severity = clamp((flooded_pct * 3.0))
    flooded = severity >= 0.20

    return {
        "status": "ok",
        "score": round(float(severity), 3),
        "value": round(float(flood_area_val or 0.0) / 1e6, 4),
        "severity": round(float(severity), 3),
        "flooded": flooded,
        "flood_extent_km2": round(float(flood_area_val or 0.0) / 1e6, 4),
        "inundation_severity": round(float(severity), 3),
        "confidence": round(clamp(0.35 + min(0.45, post_n / 10.0) + min(0.2, pre_n / 10.0)), 2),
        "anomaly_score": round(clamp(severity), 3),
        "pre_window_days": pre_days,
        "post_window_days": post_days,
        "timestamp": safe_getinfo(post_col.first().date().format("YYYY-MM-dd"), None, label="flood_aoi_timestamp"),
        "metrics": {
            "pre_scene_count": pre_n,
            "post_scene_count": post_n,
            "pre_vv_db_mean": _reduce_mean(pre_vv, aoi, 30).get(SENTINEL1_GRD["vv"]),
            "post_vv_db_mean": _reduce_mean(post_vv, aoi, 30).get(SENTINEL1_GRD["vv"]),
            "pre_vh_db_mean": _reduce_mean(pre_vh, aoi, 30).get(SENTINEL1_GRD["vh"]),
            "post_vh_db_mean": _reduce_mean(post_vh, aoi, 30).get(SENTINEL1_GRD["vh"]),
            "delta_vv_db_mean": _reduce_mean(delta_vv, aoi, 30).get(SENTINEL1_GRD["vv"]),
            "delta_vh_db_mean": _reduce_mean(delta_vh, aoi, 30).get(SENTINEL1_GRD["vh"]),
        },
        "notes": [
            "Flood detection uses Sentinel-1 GRD backscatter change and low-backscatter thresholding.",
            "This is an EO indicator, not a hydraulic flood-depth model.",
        ],
    }


def _mask_s2_sr(img):
    return mask_sentinel2_sr_scl(img)


def _olci_ndci(img):
    return s3_olci_ndci(img)


def _terraclimate_pdsi(img):
    return ee.Image(img).select(TERRACLIMATE["pdsi"]).multiply(0.01).rename(TERRACLIMATE["pdsi"])


def _landsat_oli_ndsi(img):
    return landsat_oli_ndsi(ee.Image(img)).toFloat().copyProperties(img, ["system:time_start"])


def _landsat_tm_etm_ndsi(img):
    return landsat_tm_etm_ndsi(ee.Image(img)).toFloat().copyProperties(img, ["system:time_start"])


def turbidity_point_context(lat: float, lon: float, buffer_km: float = 5.0, days: int = 30) -> Dict[str, Any]:
    return turbidity_aoi_context(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000), days=days)


def turbidity_aoi_context(geom, buffer_km: float = 5.0, days: int = 30) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    start = end.advance(-days, "day")
    aoi = geom

    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .map(_mask_s2_sr)
    )

    scene_count = int(safe_getinfo(col.size(), 0, label="turbidity_scene_count") or 0)
    if scene_count == 0:
        return {
            "status": "insufficient_data",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "timestamp": None,
            "metrics": {"scene_count": 0},
            "notes": ["No suitable Sentinel-2 scenes found in the requested window."],
        }

    latest_ts = safe_latest_timestamp(col, None, label="turbidity_latest_timestamp")
    img = col.median().clip(aoi)
    ndwi = s2_ndwi(img)
    mndwi = s2_mndwi(img)
    ndti = s2_ndti(img)
    water = mndwi.gt(0.0).And(ndwi.gt(-0.05))
    plume_mask = water.And(ndti.gt(0.05))

    plume_area_m2 = safe_reduce_sum(plume_mask.multiply(ee.Image.pixelArea()), aoi, scale=20, label="turbidity_plume_area")
    plume_area_val = next(iter(plume_area_m2.values()), 0.0) if plume_area_m2 else 0.0

    stats = _reduce_mean(ndti.updateMask(water), aoi, scale=20)
    mean_ndti = stats.get("NDTI")
    turbidity_proxy = None if mean_ndti is None else clamp((float(mean_ndti) + 0.05) / 0.25)
    clarity = None if turbidity_proxy is None else round(1.0 - turbidity_proxy, 3)

    point_ndti = _pick_band_value(_reduce_first(ndti, aoi.centroid(), 20), "NDTI")
    anomaly = None if point_ndti is None else clamp((float(point_ndti) + 0.02) / 0.20)

    return {
        "status": "ok",
        "score": round(float(turbidity_proxy), 3) if turbidity_proxy is not None else None,
        "value": round(float(mean_ndti), 4) if mean_ndti is not None else None,
        "severity": round(float(turbidity_proxy), 3) if turbidity_proxy is not None else None,
        "turbidity_proxy": round(float(turbidity_proxy), 3) if turbidity_proxy is not None else None,
        "sediment_plume_area_km2": round(float(plume_area_val or 0.0) / 1e6, 4),
        "clarity_estimate": clarity,
        "spread_direction_deg": None,
        "anomaly_score": round(float(anomaly), 3) if anomaly is not None else None,
        "confidence": round(clamp(0.4 + min(0.45, scene_count / 10.0)), 2),
        "timestamp": latest_ts,
        "metrics": {
            "scene_count": scene_count,
            "mean_ndti": mean_ndti,
            "point_ndti": point_ndti,
            "mean_ndwi": _reduce_mean(ndwi.updateMask(water), aoi, scale=20).get("NDWI"),
            "mean_mndwi": _reduce_mean(mndwi.updateMask(water), aoi, scale=20).get("MNDWI"),
        },
        "notes": [
            "Turbidity is estimated as a proxy from Sentinel-2 reflectance ratios.",
            "Absolute NTU requires local calibration and in-situ validation.",
        ],
    }


def chlorophyll_point_context(lat: float, lon: float, buffer_km: float = 5.0, days: int = 15) -> Dict[str, Any]:
    return chlorophyll_aoi_context(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000), days=days)


def chlorophyll_aoi_context(geom, buffer_km: float = 5.0, days: int = 15) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    start = end.advance(-days, "day")
    aoi = geom

    try:
        col = ee.ImageCollection("COPERNICUS/S3/OLCI").filterBounds(aoi).filterDate(start, end)
    except Exception:
        return {"status": "unavailable", "reason": "Sentinel-3 OLCI collection unavailable"}

    scene_count = int(safe_getinfo(col.size(), 0, label="chlorophyll_scene_count") or 0)
    if scene_count == 0:
        return {
            "status": "insufficient_data",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "timestamp": None,
            "metrics": {"scene_count": 0},
            "notes": ["No suitable Sentinel-3 OLCI scenes found in the requested window."],
        }

    latest_ts = safe_latest_timestamp(col, None, label="chlorophyll_latest_timestamp")
    img = col.median().clip(aoi)
    band_names = safe_getinfo(img.bandNames(), [], label="chlorophyll_olci_bands") or []
    required_bands = {SENTINEL3_OLCI["red_edge"], SENTINEL3_OLCI["red"]}
    if not required_bands.issubset(set(band_names)):
        return {
            "status": "unavailable",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "timestamp": latest_ts,
            "metrics": {"scene_count": scene_count, "bands": band_names},
            "notes": [
                "Sentinel-3 OLCI imagery was found, but the expected radiance bands for NDCI were not available.",
                f"Expected Earth Engine bands: {SENTINEL3_OLCI['red_edge']} at 708.75 nm and {SENTINEL3_OLCI['red']} at 665 nm.",
            ],
        }

    ndci = _olci_ndci(img)

    point_ndci = _pick_band_value(_reduce_first(ndci, aoi.centroid(), scale=300), "NDCI")
    mean_ndci = _reduce_mean(ndci, aoi, scale=300).get("NDCI")
    ndci_value = mean_ndci if mean_ndci is not None else point_ndci
    bloom_intensity = None if ndci_value is None else clamp((float(ndci_value) + 0.02) / 0.15)
    if bloom_intensity is None:
        bloom_risk = "unknown"
    elif bloom_intensity >= 0.7:
        bloom_risk = "high"
    elif bloom_intensity >= 0.4:
        bloom_risk = "moderate"
    else:
        bloom_risk = "low"
    anomaly = None if point_ndci is None else clamp((float(point_ndci) + 0.01) / 0.10)
    if ndci_value is None:
        return {
            "status": "insufficient_data",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "timestamp": latest_ts,
            "metrics": {"scene_count": scene_count, "mean_ndci": mean_ndci, "point_ndci": point_ndci, "bands": [SENTINEL3_OLCI["red_edge"], SENTINEL3_OLCI["red"]]},
            "notes": [
                "Sentinel-3 OLCI scenes intersected the AOI, but no valid NDCI pixel reduced over this geometry.",
                "This often happens for small inland AOIs, cloud/quality masking, or coarse 300 m mixed pixels.",
            ],
        }

    return {
        "status": "ok",
        "score": round(float(bloom_intensity), 3) if bloom_intensity is not None else None,
        "value": round(float(ndci_value), 4) if ndci_value is not None else None,
        "severity": round(float(bloom_intensity), 3) if bloom_intensity is not None else None,
        "chlorophyll_proxy": round(float(ndci_value), 4) if ndci_value is not None else None,
        "bloom_intensity": round(float(bloom_intensity), 3) if bloom_intensity is not None else None,
        "bloom_risk": bloom_risk,
        "anomaly_score": round(float(anomaly), 3) if anomaly is not None else None,
        "confidence": round(clamp(0.35 + min(0.5, scene_count / 8.0)), 2),
        "timestamp": latest_ts,
        "metrics": {
            "scene_count": scene_count,
            "mean_ndci": mean_ndci,
            "point_ndci": point_ndci,
            "bands": [SENTINEL3_OLCI["red_edge"], SENTINEL3_OLCI["red"]],
        },
        "notes": [
            "Chlorophyll is represented as a proxy using Sentinel-3 OLCI band ratios.",
            "Absolute chlorophyll-a concentration requires regional calibration and validation.",
        ],
    }


def water_quality_from_proxies(turbidity: Dict[str, Any], chlorophyll: Dict[str, Any]) -> Dict[str, Any]:
    t = turbidity.get("turbidity_proxy") or turbidity.get("score")
    c = chlorophyll.get("bloom_intensity") or chlorophyll.get("severity")
    if t is None and c is None:
        return {
            "status": "insufficient_data",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "metrics": {},
            "notes": ["Not enough optical data to compute water-quality proxies."],
        }

    turbidity_score = float(t or 0.0)
    bloom_score = float(c or 0.0)
    degradation = clamp((0.6 * turbidity_score) + (0.4 * bloom_score))
    quality_proxy = round(1.0 - degradation, 3)
    confidence = clamp(0.45 + (0.25 if t is not None else 0.0) + (0.25 if c is not None else 0.0))

    return {
        "status": "ok",
        "score": quality_proxy,
        "value": quality_proxy,
        "severity": round(degradation, 3),
        "quality_proxy": quality_proxy,
        "degradation_indicator": round(degradation, 3),
        "confidence": round(confidence, 2),
        "spectral_anomaly_score": round(degradation, 3),
        "timestamp": turbidity.get("timestamp") or chlorophyll.get("timestamp"),
        "metrics": {
            "turbidity_proxy": turbidity.get("turbidity_proxy") or turbidity.get("score"),
            "chlorophyll_proxy": chlorophyll.get("chlorophyll_proxy") or chlorophyll.get("score"),
        },
        "notes": [
            "Water quality is a proxy product derived from turbidity and chlorophyll signals.",
            "This does not directly measure pollution or drinking-water safety.",
        ],
    }


def soil_moisture_point_context(lat: float, lon: float, buffer_km: float = 5.0, days: int = 2) -> Dict[str, Any]:
    return soil_moisture_aoi_context(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000), days=days)


def soil_moisture_aoi_context(geom, buffer_km: float = 5.0, days: int = 2) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    start = end.advance(-max(days, 14), "day")
    aoi = geom

    candidates = [
        "NASA/SMAP/SPL4SMGP/008",
        "NASA/SMAP/SPL4SMGP/007",
    ]

    col = None
    used_collection = None
    scene_count = 0
    for c in candidates:
        try:
            candidate = ee.ImageCollection(c).filterBounds(aoi).filterDate(start, end)
            candidate_count = int(safe_getinfo(candidate.size(), 0, label=f"soil_moisture_count_{c}") or 0)
            if candidate_count > 0:
                col = candidate
                used_collection = c
                scene_count = candidate_count
                break
        except Exception:
            continue

    if col is None or scene_count == 0:
        try:
            s1_start = end.advance(-30, "day")
            s1 = (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(aoi)
                .filterDate(s1_start, end)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            )
            s1_count = int(safe_getinfo(s1.size(), 0, label="soil_moisture_s1_count") or 0)
            if s1_count:
                sar = s1.median().clip(aoi)
                vv = safe_reduce_mean(sar.select(SENTINEL1_GRD["vv"]), aoi, scale=30, label="soil_moisture_s1_vv").get(SENTINEL1_GRD["vv"])
                vh = safe_reduce_mean(sar.select(SENTINEL1_GRD["vh"]), aoi, scale=30, label="soil_moisture_s1_vh").get(SENTINEL1_GRD["vh"])
                if vv is not None and vh is not None:
                    moisture_proxy = clamp((float(vh) - float(vv) + 8.0) / 12.0)
                    drought_stress = clamp(1.0 - moisture_proxy)
                    return {
                        "status": "ok",
                        "score": round(float(moisture_proxy), 3),
                        "value": round(float(drought_stress), 3),
                        "severity": round(float(drought_stress), 3),
                        "surface_soil_moisture": None,
                        "root_zone_moisture": None,
                        "drought_stress": round(float(drought_stress), 3),
                        "confidence": 0.45,
                        "timestamp": safe_latest_timestamp(s1, None, label="soil_moisture_s1_timestamp"),
                        "metrics": {"collection": "COPERNICUS/S1_GRD", "scene_count": s1_count, "mean_vv_db": vv, "mean_vh_db": vh, "sentinel1_moisture_proxy": round(float(moisture_proxy), 3)},
                        "notes": [
                            "SMAP root-zone soil moisture was unavailable; returned a Sentinel-1 SAR moisture proxy fallback.",
                            "SAR backscatter moisture proxies require land-cover and roughness calibration before operational use.",
                        ],
                    }
        except Exception as exc:
            logger.warning("Sentinel-1 soil moisture fallback failed: %s", exc, exc_info=True)

        return {
            "status": "insufficient_data",
            "score": None,
            "value": None,
            "severity": None,
            "confidence": 0.0,
            "timestamp": None,
            "metrics": {},
            "notes": ["SMAP soil moisture collection was not available and Sentinel-1 proxy fallback could not be computed."],
        }

    latest_ts = safe_latest_timestamp(col, None, label="soil_moisture_timestamp")
    img = col.median().clip(aoi)
    band_names = safe_getinfo(img.bandNames(), [], label="soil_moisture_bands") or []
    vals = _reduce_mean(img, aoi, scale=11000)
    # best-effort picking of surface / root-zone values
    surface = None
    root = None
    for b in band_names:
        bl = b.lower()
        if surface is None and any(k in bl for k in ["ssm", "surface", "sm_surface", "soil_moisture_surface"]):
            surface = vals.get(b)
        if root is None and any(k in bl for k in ["rsm", "root", "sm_root", "rootzone"]):
            root = vals.get(b)
    if surface is None and band_names:
        surface = vals.get(band_names[0])
    if root is None and len(band_names) > 1:
        root = vals.get(band_names[1])

    drought_stress = None
    if surface is not None or root is not None:
        ss = float(surface if surface is not None else root)
        rr = float(root if root is not None else surface)
        drought_stress = clamp(1.0 - ((ss + rr) / 2.0))

    return {
        "status": "ok",
        "score": round(float(1.0 - (drought_stress or 0.0)), 3) if drought_stress is not None else None,
        "value": round(float(drought_stress), 3) if drought_stress is not None else None,
        "severity": round(float(drought_stress), 3) if drought_stress is not None else None,
        "surface_soil_moisture": surface,
        "root_zone_moisture": root,
        "drought_stress": round(float(drought_stress), 3) if drought_stress is not None else None,
        "confidence": 0.8 if drought_stress is not None else 0.0,
        "timestamp": latest_ts,
        "metrics": {"collection": used_collection, "scene_count": scene_count, "bands": band_names, **vals},
        "notes": [
            "Soil moisture is derived from SMAP if available; otherwise this layer falls back to a proxy workflow.",
            "Local calibration is recommended for drought stress decisions.",
        ],
    }


def drought_indices_point_context(lat: float, lon: float, buffer_km: float = 5.0) -> Dict[str, Any]:
    return drought_indices_aoi_context(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000))


def drought_indices_aoi_context(geom, days: int = 90) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    start = end.advance(-days, "day")
    aoi = geom

    try:
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(aoi).filterDate(start, end)
        chirps_recent = chirps.select(CHIRPS["precipitation"]).sum()
        recent_pr = _reduce_mean(chirps_recent, aoi, scale=5000).get(CHIRPS["precipitation"])
    except Exception:
        recent_pr = None

    try:
        tc = ee.ImageCollection("IDAHO_EPSCOR/TERRACLIMATE").filterBounds(aoi).filterDate(start.advance(-3650, "day"), end)
        tc_latest = ee.Image(tc.sort("system:time_start", False).first())
        tc_vals = _reduce_mean(
            tc_latest.select(
                [
                    TERRACLIMATE["precipitation"],
                    TERRACLIMATE["deficit"],
                    TERRACLIMATE["aet"],
                    TERRACLIMATE["soil"],
                ]
            ).addBands(_terraclimate_pdsi(tc_latest)),
            aoi,
            scale=4000,
        )
    except Exception:
        tc_latest = None
        tc_vals = {}

    pdsi = tc_vals.get(TERRACLIMATE["pdsi"])
    water_deficit = tc_vals.get(TERRACLIMATE["deficit"])
    soil = tc_vals.get(TERRACLIMATE["soil"])
    pr_mean = tc_vals.get(TERRACLIMATE["precipitation"])

    spi_proxy = None
    if recent_pr is not None and pr_mean is not None:
        spi_proxy = clamp(0.5 + ((float(recent_pr) - float(pr_mean)) / max(abs(float(pr_mean)), 1.0)) / 4.0)
    elif recent_pr is not None:
        spi_proxy = clamp(float(recent_pr) / 200.0)

    spei_proxy = None
    if recent_pr is not None and water_deficit is not None:
        spei_proxy = clamp(0.5 + ((float(recent_pr) - float(water_deficit)) / max(abs(float(recent_pr)) + abs(float(water_deficit)), 1.0)) / 4.0)
    elif pdsi is not None:
        spei_proxy = clamp(0.5 - float(pdsi) / 10.0)

    drought_stress = None
    if pdsi is not None:
        drought_stress = clamp(0.5 - float(pdsi) / 10.0)
    elif spi_proxy is not None:
        drought_stress = clamp(1.0 - spi_proxy)

    return {
        "status": "ok" if spi_proxy is not None or spei_proxy is not None or pdsi is not None else "insufficient_data",
        "score": round(float(drought_stress), 3) if drought_stress is not None else None,
        "value": round(float(drought_stress), 3) if drought_stress is not None else None,
        "severity": round(float(drought_stress), 3) if drought_stress is not None else None,
        "drought_stress": round(float(drought_stress), 3) if drought_stress is not None else None,
        "spi_proxy": round(float(spi_proxy), 3) if spi_proxy is not None else None,
        "spei_proxy": round(float(spei_proxy), 3) if spei_proxy is not None else None,
        "pdsi": pdsi,
        "water_deficit": water_deficit,
        "precip_recent_mm": recent_pr,
        "soil_moisture_background": soil,
        "confidence": 0.75 if drought_stress is not None else 0.0,
        "timestamp": safe_getinfo(tc_latest.date().format("YYYY-MM-dd"), None, label="drought_timestamp") if tc_latest is not None else None,
        "metrics": {"terra_climate": tc_vals, "recent_precip_mm": recent_pr},
        "notes": [
            "SPI and SPEI are proxy indices here derived from CHIRPS and TerraClimate.",
            "For formal drought reporting, compute full climatological percentiles per basin and month.",
        ],
    }


def glacier_retreat_point_context(lat: float, lon: float, buffer_km: float = 5.0) -> Dict[str, Any]:
    return glacier_retreat_aoi_context(ee_buffer_bounds(_pt(lat, lon), buffer_km * 1000))


def glacier_retreat_aoi_context(geom, recent_days: int = 365, historic_start_year: int = 1984) -> Dict[str, Any]:
    if not initialize_earth_engine():
        return {"status": "permission_denied", "reason": _EE_ERROR}

    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())
    recent_start = end.advance(-recent_days, "day")
    historic_start = ee.Date.fromYMD(historic_start_year, 1, 1)
    recent_aoi = geom

    # Recent sentinel-2 snow/ice proxy
    try:
        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(recent_aoi)
            .filterDate(recent_start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
            .map(_mask_s2_sr)
        )
        s2_count = int(safe_getinfo(s2.size(), 0, label="glacier_s2_count") or 0)
    except Exception:
        s2 = None
        s2_count = 0

    recent_fraction = None
    if s2 is not None and s2_count > 0:
        img = s2.median().clip(recent_aoi)
        ndsi = s2_ndsi(img)
        snow_mask = ndsi.gt(0.4)
        snow_area_m2 = safe_reduce_sum(snow_mask.multiply(ee.Image.pixelArea()), recent_aoi, scale=20, label="glacier_recent_snow_area")
        snow_area = next(iter(snow_area_m2.values()), 0.0) if snow_area_m2 else 0.0
        recent_area_m2 = safe_area_m2(recent_aoi, 0.0, label="glacier_recent_aoi_area")
        recent_fraction = float(snow_area) / float(recent_area_m2) if recent_area_m2 else None

    # Historical Landsat proxy using NDSI
    hist_fraction = None
    hist_count = 0
    try:
        l8_ndsi = (
            ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
            .filterBounds(recent_aoi)
            .filterDate(historic_start, ee.Date.fromYMD(2018, 12, 31))
            .filter(ee.Filter.lt("CLOUD_COVER", 70))
            .map(_landsat_oli_ndsi)
        )
        l7_ndsi = (
            ee.ImageCollection("LANDSAT/LE07/C02/T1_L2")
            .filterBounds(recent_aoi)
            .filterDate(historic_start, end)
            .filter(ee.Filter.lt("CLOUD_COVER", 70))
            .map(_landsat_tm_etm_ndsi)
        )
        l5_ndsi = (
            ee.ImageCollection("LANDSAT/LT05/C02/T1_L2")
            .filterBounds(recent_aoi)
            .filterDate(historic_start, ee.Date.fromYMD(2012, 12, 31))
            .filter(ee.Filter.lt("CLOUD_COVER", 70))
            .map(_landsat_tm_etm_ndsi)
        )
        hist = (
            l8_ndsi.merge(l7_ndsi).merge(l5_ndsi)
            .select(["NDSI"])
            .map(lambda img: ee.Image(img).toFloat().copyProperties(img, ["system:time_start"]))
        )
        hist_count = int(safe_getinfo(hist.size(), 0, label="glacier_landsat_count") or 0)
        if hist_count > 0:
            hist_baseline = hist.sort("system:time_start").limit(48)
            ndsi_h = hist_baseline.median().clip(recent_aoi)
            snow_mask_h = ndsi_h.gt(0.4)
            snow_area_h_m2 = safe_reduce_sum(snow_mask_h.multiply(ee.Image.pixelArea()), recent_aoi, scale=60, label="glacier_historic_snow_area")
            snow_area_h = next(iter(snow_area_h_m2.values()), 0.0) if snow_area_h_m2 else 0.0
            hist_area_m2 = safe_area_m2(recent_aoi, 0.0, label="glacier_historic_aoi_area")
            hist_fraction = float(snow_area_h) / float(hist_area_m2) if hist_area_m2 else None
    except Exception:
        pass

    retreat_pct = None
    if hist_fraction is not None and recent_fraction is not None:
        retreat_pct = clamp((hist_fraction - recent_fraction) / max(hist_fraction, 1e-6), 0.0, 1.0)

    return {
        "status": "ok" if recent_fraction is not None or hist_fraction is not None else "insufficient_data",
        "score": round(float(1.0 - (retreat_pct or 0.0)), 3) if retreat_pct is not None else None,
        "value": round(float(retreat_pct), 3) if retreat_pct is not None else None,
        "severity": round(float(retreat_pct), 3) if retreat_pct is not None else None,
        "glacier_area_recent_fraction": recent_fraction,
        "glacier_area_historical_fraction": hist_fraction,
        "retreat_fraction": retreat_pct,
        "confidence": 0.7 if retreat_pct is not None else 0.0,
        "timestamp": safe_getinfo(end.format("YYYY-MM-dd"), None, label="glacier_timestamp"),
        "metrics": {
            "recent_scene_count": s2_count,
            "historic_scene_count": hist_count,
            "historic_baseline_scene_limit": 48,
            "historic_note": "Landsat/Sentinel-2 snow/ice proxy using NDSI",
        },
        "notes": [
            "Glacier retreat is only meaningful in glaciated or snow-covered areas.",
            "This layer is a proxy and should be validated for each glacier basin.",
        ],
    }


def export_geotiff_point(lat: float, lon: float, layer: str, buffer_km: float = 5.0, scale_m: int = 30) -> str:
    if not initialize_earth_engine():
        raise RuntimeError(f"Earth Engine unavailable: {_EE_ERROR}")
    ensure_dir(_SETTINGS.cache_dir)
    aoi_bbox = bbox_around_point(lat, lon, buffer_km)
    aoi = ee.Geometry.Rectangle(aoi_bbox)
    return _export_geotiff_common(aoi, layer, lat=lat, lon=lon, buffer_km=buffer_km, scale_m=scale_m)


def export_geotiff_aoi(geometry: dict, layer: str, buffer_km: float = 5.0, scale_m: int = 30, label: str = None) -> str:
    if not initialize_earth_engine():
        raise RuntimeError(f"Earth Engine unavailable: {_EE_ERROR}")
    ensure_dir(_SETTINGS.cache_dir)
    _, clean_geometry = normalize_aoi_geometry(geometry)
    aoi = ee.Geometry(clean_geometry)
    return _export_geotiff_common(aoi, layer, label=label, scale_m=scale_m)


def _export_geotiff_common(aoi, layer: str, lat: float = None, lon: float = None, buffer_km: float = 5.0, scale_m: int = 30, label: str = None) -> str:
    import datetime as dt
    end = ee.Date(dt.datetime.utcnow().isoformat())

    if layer == "flood":
        ctx = flood_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("inundation_severity") or 0.0)).rename("flood_severity")
    elif layer == "turbidity":
        ctx = turbidity_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("turbidity_proxy") or ctx.get("score") or 0.0)).rename("turbidity_proxy")
    elif layer == "chlorophyll":
        ctx = chlorophyll_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("bloom_intensity") or ctx.get("severity") or 0.0)).rename("chlorophyll_proxy")
    elif layer == "water_quality":
        t = turbidity_aoi_context(aoi)
        c = chlorophyll_aoi_context(aoi)
        q = water_quality_from_proxies(t, c)
        image = ee.Image.constant(float(q.get("quality_proxy") or q.get("score") or 0.0)).rename("water_quality_proxy")
    elif layer == "soil_moisture":
        ctx = soil_moisture_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("drought_stress") or ctx.get("severity") or 0.0)).rename("soil_moisture_proxy")
    elif layer == "drought":
        ctx = drought_indices_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("drought_stress") or ctx.get("severity") or 0.0)).rename("drought_stress")
    elif layer == "glacier":
        ctx = glacier_retreat_aoi_context(aoi)
        image = ee.Image.constant(float(ctx.get("retreat_fraction") or ctx.get("severity") or 0.0)).rename("glacier_retreat")
    else:
        image = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select(JRC_GSW[0])

    image = image.clip(aoi)
    try:
        url = image.getDownloadURL({
            "scale": scale_m,
            "crs": "EPSG:4326",
            "region": aoi,
            "format": "GEO_TIFF",
        })
    except Exception as exc:
        logger.exception("Earth Engine GeoTIFF URL generation failed for layer=%s", layer)
        raise RuntimeError(f"Raster export unavailable for {layer}: {exc}") from exc

    if lat is not None and lon is not None:
        name = f"{layer}_{round(lat,4)}_{round(lon,4)}.tif"
    else:
        safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in (label or "aoi"))
        name = f"{layer}_{safe_label}.tif"

    out_path = Path(_SETTINGS.cache_dir) / name
    try:
        r = requests.get(url, timeout=_SETTINGS.request_timeout_seconds, stream=True)
        r.raise_for_status()
    except Exception as exc:
        logger.exception("Raster download failed for layer=%s", layer)
        raise RuntimeError(f"Raster download failed for {layer}: {exc}") from exc
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return str(out_path)
