from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from app.config import get_settings
from app.models.water_models import (
    AOIAnalysisResponse,
    AOIRequest,
    AOITiffRequest,
    AreaStats,
    GeoPoint,
    GeometryInput,
    LayerAnalysis,
    MethodologyItem,
    PointAnalysisResponse,
    SourceRef,
    TimelinePoint,
    WaterFeatureContext,
    WaterTimelineResponse,
)
from app.services.gee_service import (
    auth_status,
    chlorophyll_aoi_context,
    chlorophyll_point_context,
    drought_indices_aoi_context,
    drought_indices_point_context,
    export_geotiff_aoi,
    export_geotiff_point,
    flood_aoi_context,
    flood_point_context,
    glacier_retreat_aoi_context,
    glacier_retreat_point_context,
    jrc_aoi_context,
    jrc_point_context,
    soil_moisture_aoi_context,
    soil_moisture_point_context,
    timeline_context_aoi,
    timeline_context_point,
    turbidity_aoi_context,
    turbidity_point_context,
    water_quality_from_proxies,
)
from app.services.cache_service import get as cache_get, set as cache_set, stable_cache_key
from app.services.stac_service import get_recent_context
from app.services.utils import (
    africa_bbox_check,
    is_valid_aoi_for_africa,
    normalize_aoi_geometry,
    polygon_area_km2,
    polygon_perimeter_km,
)
from app.services.vector_service import find_nearest_water_body

_SETTINGS = get_settings()
logger = logging.getLogger(__name__)
ANALYSIS_TASK_TIMEOUT_SECONDS = 90
LARGE_AOI_KM2 = 100000.0
ANALYSIS_CACHE_VERSION = "eo-v9-layer-yearly-trends"


def _methodology() -> List[MethodologyItem]:
    return [
        MethodologyItem(
            title="Flood analysis",
            description="Sentinel-1 GRD backscatter change detection and low-backscatter thresholding for temporary inundation.",
        ),
        MethodologyItem(
            title="Turbidity analysis",
            description="Sentinel-2 harmonized surface reflectance with cloud masking, NDWI/MNDWI, and NDTI water-proxy ratios.",
        ),
        MethodologyItem(
            title="Algal bloom analysis",
            description="Sentinel-3 OLCI NDCI proxy using red and red-edge bands around 665 nm and 708.75 nm.",
        ),
        MethodologyItem(
            title="Water quality proxy",
            description="Weighted combination of turbidity and chlorophyll proxies. Requires local calibration for regulatory use.",
        ),
        MethodologyItem(
            title="Soil moisture and drought",
            description="SMAP-based moisture and CHIRPS/TerraClimate proxy drought indices.",
        ),
        MethodologyItem(
            title="Glacier retreat",
            description="Sentinel-2 and Landsat snow/ice proxy based on NDSI for glaciated areas.",
        ),
        MethodologyItem(
            title="Historical water context",
            description="JRC Global Surface Water v1.4 provides long-term occurrence, seasonality, and yearly history.",
        ),
    ]


def _sources(recent: Dict[str, Any]) -> List[SourceRef]:
    out = [
        SourceRef(
            name="JRC Global Surface Water v1.4",
            collection="JRC/GSW1_4/GlobalSurfaceWater",
            kind="historical_water",
            timestamp="1984-2021",
            notes="Occurrence, seasonality, transition, and max extent.",
        ),
        SourceRef(
            name="Natural Earth",
            collection="ne_10m_lakes.geojson / ne_10m_rivers.geojson / coastline",
            kind="vector_context",
            notes="Nearest named water body context.",
        ),
    ]
    for key, label, kind in [
        ("sentinel1", "Sentinel-1 via STAC", "sar_recent_context"),
        ("sentinel2", "Sentinel-2 via STAC", "optical_recent_context"),
        ("sentinel3", "Sentinel-3 via STAC", "ocean_color_recent_context"),
        ("landsat", "Landsat via STAC", "landsat_recent_context"),
    ]:
        item = recent.get(key)
        if item:
            out.append(SourceRef(
                name=label,
                collection=getattr(item, "collection", None),
                kind=kind,
                timestamp=getattr(item, "datetime", None),
                notes="Recent scene metadata for contextual analysis.",
            ))
    return out


def _summary(flood: LayerAnalysis, turbidity: LayerAnalysis, chlorophyll: LayerAnalysis, water_quality: LayerAnalysis, soil_moisture: LayerAnalysis, drought: LayerAnalysis, glacier: LayerAnalysis, nearest: dict) -> str:
    if flood.status == "ok" and flood.severity is not None and flood.severity >= 0.25:
        return "Flood signal detected"
    if chlorophyll.status == "ok" and chlorophyll.severity is not None and chlorophyll.severity >= 0.7:
        return "High bloom risk detected"
    if turbidity.status == "ok" and turbidity.severity is not None and turbidity.severity >= 0.5:
        return "High turbidity / sediment signal"
    if water_quality.status == "ok" and water_quality.severity is not None and water_quality.severity >= 0.6:
        return "Water-quality degradation signal"
    if soil_moisture.status == "ok" and soil_moisture.value is not None and soil_moisture.value >= 0.5:
        return "Dry soil / drought stress signal"
    if drought.status == "ok" and drought.value is not None and drought.value >= 0.5:
        return "Drought stress signal"
    if glacier.status == "ok" and glacier.value is not None and glacier.value >= 0.2:
        return "Glacier retreat signal"
    if nearest.get("distance_km") is not None and nearest["distance_km"] <= 2:
        return "Near mapped water feature"
    return "Stable or low-risk water context"


def _flags(flood: LayerAnalysis, turbidity: LayerAnalysis, chlorophyll: LayerAnalysis, water_quality: LayerAnalysis, soil_moisture: LayerAnalysis, drought: LayerAnalysis, glacier: LayerAnalysis, nearest: dict) -> List[str]:
    flags = []
    if flood.status == "ok" and flood.severity is not None and flood.severity >= 0.25:
        flags.append("flood_signal_detected")
    if turbidity.status == "ok" and turbidity.severity is not None and turbidity.severity >= 0.5:
        flags.append("high_turbidity")
    if chlorophyll.status == "ok" and chlorophyll.severity is not None and chlorophyll.severity >= 0.4:
        flags.append("bloom_watch")
    if water_quality.status == "ok" and water_quality.severity is not None and water_quality.severity >= 0.6:
        flags.append("water_quality_degraded")
    if soil_moisture.status == "ok" and soil_moisture.value is not None and soil_moisture.value >= 0.5:
        flags.append("soil_moisture_stress")
    if drought.status == "ok" and drought.value is not None and drought.value >= 0.5:
        flags.append("drought_watch")
    if glacier.status == "ok" and glacier.value is not None and glacier.value >= 0.2:
        flags.append("glacier_retreat_watch")
    if nearest.get("distance_km") is not None and nearest["distance_km"] <= 1:
        flags.append("near_mapped_water_body")
    return flags


def _to_layer(obj: Dict[str, Any]) -> LayerAnalysis:
    metrics = dict(obj.get("metrics") or {})
    for key in [
        "flood_extent_km2",
        "inundation_severity",
        "turbidity_proxy",
        "sediment_plume_area_km2",
        "chlorophyll_proxy",
        "bloom_intensity",
        "bloom_risk",
        "quality_proxy",
        "surface_soil_moisture",
        "root_zone_moisture",
        "drought_stress",
        "spi_proxy",
        "spei_proxy",
        "pdsi",
        "glacier_area_recent_fraction",
        "glacier_area_historical_fraction",
        "retreat_fraction",
    ]:
        if key in obj and key not in metrics:
            metrics[key] = obj.get(key)
    return LayerAnalysis(
        status=obj.get("status", "unknown"),
        score=obj.get("score"),
        value=obj.get("value"),
        severity=obj.get("severity"),
        confidence=float(obj.get("confidence") or 0.0),
        timestamp=obj.get("timestamp"),
        metrics=metrics,
        notes=obj.get("notes") or ([] if obj.get("status") else []),
    )


def _timeline_points(raw: List[Dict[str, Any]]) -> List[TimelinePoint]:
    if isinstance(raw, dict):
        raw = raw.get("yearly", [])
    out = []
    for item in raw:
        out.append(TimelinePoint(
            year=item.get("year"),
            water_class=item.get("water_class"),
            permanent=item.get("permanent", False),
            seasonal=item.get("seasonal", False),
            occurrence_pct=item.get("occurrence_pct"),
            value=item.get("value"),
            note=item.get("note"),
        ))
    return out


def _trend_summary(hist: List[Dict[str, Any]], flood: Dict[str, Any], turbidity: Dict[str, Any], chlorophyll: Dict[str, Any], drought: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(hist, dict):
        base = {
            "status": hist.get("status", "ok"),
            "reason": hist.get("reason"),
            "yearly": hist.get("yearly", []),
            "monthly": hist.get("monthly", []),
            "flood_history": hist.get("flood_history", []),
            "flood_yearly_trends": hist.get("flood_yearly_trends", []),
            "turbidity_trends": hist.get("turbidity_trends", []),
            "turbidity_yearly_trends": hist.get("turbidity_yearly_trends", []),
            "chlorophyll_trends": hist.get("chlorophyll_trends", []),
            "chlorophyll_yearly_trends": hist.get("chlorophyll_yearly_trends", []),
            "soil_moisture_trends": hist.get("soil_moisture_trends", []),
            "soil_moisture_yearly_trends": hist.get("soil_moisture_yearly_trends", []),
            "drought_trends": hist.get("drought_trends", []),
            "drought_yearly_trends": hist.get("drought_yearly_trends", []),
            "glacier_trends": hist.get("glacier_trends", []),
            "glacier_yearly_trends": hist.get("glacier_yearly_trends", []),
            "water_quality_trends": hist.get("water_quality_trends", []),
            "water_quality_yearly_trends": hist.get("water_quality_yearly_trends", []),
            "anomaly_trends": hist.get("anomaly_trends", []),
            "anomaly_yearly_trends": hist.get("anomaly_yearly_trends", []),
            "scale_note": hist.get("scale_note"),
        }
        if base["anomaly_trends"] or base["anomaly_yearly_trends"]:
            return base
        hist = hist.get("yearly", [])
    else:
        base = {}
    yearly = [
        {"year": item.get("year"), "water_class": item.get("water_class"), "value": item.get("value")}
        for item in hist
        if item.get("year") is not None
    ]
    anomaly_value = max(
        float(flood.get("anomaly_score") or flood.get("severity") or 0.0),
        float(turbidity.get("anomaly_score") or turbidity.get("severity") or 0.0),
        float(chlorophyll.get("anomaly_score") or chlorophyll.get("severity") or 0.0),
        float(drought.get("severity") or 0.0),
    )
    fallback = {
        "status": base.get("status", "ok"),
        "reason": base.get("reason"),
        "yearly": yearly,
        "monthly": [],
        "flood_history": [{"timestamp": flood.get("timestamp"), "severity": flood.get("severity"), "extent_km2": flood.get("flood_extent_km2")}] if flood.get("timestamp") else [],
        "flood_yearly_trends": [],
        "turbidity_trends": [{"timestamp": turbidity.get("timestamp"), "value": turbidity.get("value"), "severity": turbidity.get("severity")}] if turbidity.get("timestamp") else [],
        "turbidity_yearly_trends": [],
        "chlorophyll_trends": [{"timestamp": chlorophyll.get("timestamp"), "value": chlorophyll.get("value"), "severity": chlorophyll.get("severity")}] if chlorophyll.get("timestamp") else [],
        "chlorophyll_yearly_trends": [],
        "soil_moisture_trends": [],
        "soil_moisture_yearly_trends": [],
        "drought_trends": [{"timestamp": drought.get("timestamp"), "value": drought.get("value"), "pdsi": drought.get("pdsi")}] if drought.get("timestamp") else [],
        "drought_yearly_trends": [],
        "glacier_trends": [],
        "glacier_yearly_trends": [],
        "water_quality_trends": [],
        "water_quality_yearly_trends": [],
        "anomaly_trends": [{"timestamp": _pick_timestamp(flood, turbidity, chlorophyll, drought), "value": round(anomaly_value, 3)}],
        "anomaly_yearly_trends": [],
    }
    for key, value in base.items():
        if key in {"status", "reason"} or value:
            fallback[key] = value
    return fallback


def _fallback_layer(reason: str, source: str = "earth_engine") -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "score": None,
        "value": None,
        "severity": None,
        "confidence": 0.0,
        "metrics": {"source": source, "fallback": True},
        "notes": [reason],
    }


def _with_stac_fallback(layer: Dict[str, Any], recent: Dict[str, Any], key: str, label: str) -> Dict[str, Any]:
    if layer.get("status") not in {"permission_denied", "unavailable", "insufficient_data"}:
        return layer
    item = recent.get(key)
    if not item:
        return layer
    out = dict(layer)
    metrics = dict(out.get("metrics") or {})
    metrics["stac_fallback"] = {
        "collection": getattr(item, "collection", None),
        "item_id": getattr(item, "item_id", None),
        "datetime": getattr(item, "datetime", None),
        "bbox": getattr(item, "bbox", None),
        "assets": [asset.model_dump() for asset in getattr(item, "assets", [])[:8]],
    }
    notes = list(out.get("notes") or [])
    notes.append(f"Earth Engine product unavailable; {label} scene metadata and COG assets are available through STAC fallback.")
    out["metrics"] = metrics
    out["notes"] = notes
    return out


async def _run_with_timeout(name: str, awaitable: Any, timeout_seconds: int = ANALYSIS_TASK_TIMEOUT_SECONDS) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("Analysis task timed out: %s after %ss", name, timeout_seconds)
        return _fallback_layer(f"{name} analysis timed out after {timeout_seconds}s. Try a smaller AOI or run this layer as an asynchronous export.", source="timeout")
    except Exception as exc:
        logger.error("Analysis task failed: %s", name, exc_info=(type(exc), exc, exc.__traceback__))
        return _fallback_layer(f"{name} analysis failed gracefully: {exc}")


async def _gather_named(tasks: Dict[str, Any], timeout_seconds: int = ANALYSIS_TASK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    names = list(tasks)
    wrapped = [_run_with_timeout(name, task, timeout_seconds=timeout_seconds) for name, task in tasks.items()]
    results = await asyncio.gather(*wrapped, return_exceptions=False)
    out: Dict[str, Any] = {}
    for name, result in zip(names, results):
        out[name] = result
    return out


async def inspect_point(lat: float, lon: float, buffer_km: float = None) -> PointAnalysisResponse:
    if not africa_bbox_check(lat, lon):
        raise ValueError("Location is outside the Africa analysis extent")

    buffer_km = float(buffer_km or _SETTINGS.default_buffer_km)
    ee_status = auth_status()
    cache_key = stable_cache_key("point", {"version": ANALYSIS_CACHE_VERSION, "lat": round(float(lat), 5), "lon": round(float(lon), 5), "buffer_km": buffer_km, "ee_ready": bool(ee_status.get("ready"))})
    cached_response = cache_get("aoi", cache_key)
    if cached_response is not None:
        return cached_response

    gathered = await _gather_named({
        "nearest": asyncio.to_thread(find_nearest_water_body, lat, lon),
        "recent": asyncio.to_thread(get_recent_context, lat, lon),
        "jrc": asyncio.to_thread(jrc_point_context, lat, lon),
        "flood": asyncio.to_thread(flood_point_context, lat, lon, buffer_km),
        "turbidity": asyncio.to_thread(turbidity_point_context, lat, lon, buffer_km),
        "chlorophyll": asyncio.to_thread(chlorophyll_point_context, lat, lon, buffer_km),
        "soil": asyncio.to_thread(soil_moisture_point_context, lat, lon, buffer_km),
        "drought": asyncio.to_thread(drought_indices_point_context, lat, lon, buffer_km),
        "glacier": asyncio.to_thread(glacier_retreat_point_context, lat, lon, buffer_km),
        "timeline": asyncio.to_thread(timeline_context_point, lat, lon, buffer_km),
    })

    nearest = gathered.get("nearest") or {}
    recent = gathered.get("recent") or {}
    jrc = gathered.get("jrc") or {}
    flood = _with_stac_fallback(gathered.get("flood") or {}, recent, "sentinel1", "Sentinel-1")
    turbidity = _with_stac_fallback(gathered.get("turbidity") or {}, recent, "sentinel2", "Sentinel-2")
    chlorophyll = _with_stac_fallback(gathered.get("chlorophyll") or {}, recent, "sentinel3", "Sentinel-3 OLCI")
    soil = _with_stac_fallback(gathered.get("soil") or {}, recent, "sentinel1", "Sentinel-1 soil-moisture proxy")
    drought = gathered.get("drought") or {}
    glacier = _with_stac_fallback(gathered.get("glacier") or {}, recent, "landsat", "Landsat")
    hist = gathered.get("timeline") or []

    water_quality = water_quality_from_proxies(turbidity, chlorophyll)

    flood_l = _to_layer(flood)
    turb_l = _to_layer(turbidity)
    chl_l = _to_layer(chlorophyll)
    soil_l = _to_layer(soil)
    drought_l = _to_layer(drought)
    glacier_l = _to_layer(glacier)
    quality_l = _to_layer(water_quality)

    response = PointAnalysisResponse(
        location=GeoPoint(lat=lat, lon=lon),
        summary_card=_summary(flood_l, turb_l, chl_l, quality_l, soil_l, drought_l, glacier_l, nearest),
        nearest_water_body=WaterFeatureContext(**nearest) if nearest else None,
        source_dataset="JRC + Natural Earth + Sentinel-1/2/3 + SMAP + CHIRPS/TerraClimate",
        data_timestamp=_pick_timestamp(flood, turbidity, chlorophyll, soil, drought, glacier, jrc),
        flood=flood_l,
        turbidity=turb_l,
        chlorophyll=chl_l,
        water_quality=quality_l,
        soil_moisture=soil_l,
        drought=drought_l,
        glacier=glacier_l,
        historical_timeline=_timeline_points(hist),
        trend_summary=_trend_summary(hist, flood, turbidity, chlorophyll, drought),
        flags=_flags(flood_l, turb_l, chl_l, quality_l, soil_l, drought_l, glacier_l, nearest),
        sources=_sources(recent),
        methodology=_methodology(),
        nearby_context={
            "buffer_km": buffer_km,
            "jrc": jrc,
            "recent_scene_context": {
                "sentinel1": recent.get("sentinel1").model_dump() if recent.get("sentinel1") else None,
                "sentinel2": recent.get("sentinel2").model_dump() if recent.get("sentinel2") else None,
                "sentinel3": recent.get("sentinel3").model_dump() if recent.get("sentinel3") else None,
                "landsat": recent.get("landsat").model_dump() if recent.get("landsat") else None,
            },
        },
        uncertainty={
            "notes": [
                "Flood detection is an EO indicator, not a hydraulic flood-depth model.",
                "Turbidity and chlorophyll are proxy estimates and require local calibration for regulatory use.",
                "Clouds, adjacency effects, and mixed pixels can bias optical water products.",
                "Soil moisture and drought outputs are proxies if full SMAP or climate product access is limited.",
                "Glacier retreat is only meaningful where snow/ice actually exists.",
            ],
        },
        download_links={
            "flood_tif": f"/tif/export?lat={lat}&lon={lon}&layer=flood&buffer_km={buffer_km}",
            "turbidity_tif": f"/tif/export?lat={lat}&lon={lon}&layer=turbidity&buffer_km={buffer_km}",
            "chlorophyll_tif": f"/tif/export?lat={lat}&lon={lon}&layer=chlorophyll&buffer_km={buffer_km}",
            "water_quality_tif": f"/tif/export?lat={lat}&lon={lon}&layer=water_quality&buffer_km={buffer_km}",
            "soil_moisture_tif": f"/tif/export?lat={lat}&lon={lon}&layer=soil_moisture&buffer_km={buffer_km}",
            "drought_tif": f"/tif/export?lat={lat}&lon={lon}&layer=drought&buffer_km={buffer_km}",
            "glacier_tif": f"/tif/export?lat={lat}&lon={lon}&layer=glacier&buffer_km={buffer_km}",
        },
    )
    cache_set("aoi", cache_key, response)
    return response


def _pick_timestamp(*objs) -> Any:
    for obj in objs:
        ts = obj.get("timestamp") if isinstance(obj, dict) else None
        if ts:
            return ts
        metrics = obj.get("metrics") if isinstance(obj, dict) else None
        if isinstance(metrics, dict):
            if metrics.get("timestamp"):
                return metrics.get("timestamp")
    return None


async def analyze_aoi(geometry: dict, label: str = None, buffer_km: float = None) -> AOIAnalysisResponse:
    poly, clean_geometry = normalize_aoi_geometry(geometry)
    if not is_valid_aoi_for_africa(poly):
        raise ValueError("AOI is outside the Africa analysis extent")

    buffer_km = float(buffer_km or _SETTINGS.default_buffer_km)
    centroid = poly.centroid
    c_lat, c_lon = float(centroid.y), float(centroid.x)
    aoi_area = polygon_area_km2(poly)
    per_km = polygon_perimeter_km(poly)
    is_large_aoi = aoi_area > LARGE_AOI_KM2

    ee_status = auth_status()
    ee_ready = bool(ee_status.get("ready"))
    cache_key = stable_cache_key("aoi_analysis", {"version": ANALYSIS_CACHE_VERSION, "geometry": clean_geometry, "label": label, "buffer_km": buffer_km, "ee_ready": ee_ready})
    cached_response = cache_get("aoi", cache_key)
    if cached_response is not None:
        return cached_response

    from app.services.gee_service import ee  # type: ignore
    geom_ee = None
    if ee is not None and ee_ready:
        try:
            geom_ee = ee.Geometry(clean_geometry)
        except Exception as exc:
            ee_ready = False
            ee_status = {"ready": False, "error": str(exc)}
            logger.exception("Failed to construct Earth Engine AOI geometry")

    if geom_ee is None:
        reason = ee_status.get("error") or "Earth Engine client library not initialized"
        logger.warning("Earth Engine unavailable for AOI analysis; returning vector/STAC fallback: %s", reason)
        gathered = await _gather_named({
            "nearest": asyncio.to_thread(find_nearest_water_body, c_lat, c_lon),
            "recent": asyncio.to_thread(get_recent_context, c_lat, c_lon),
        })
        unavailable = _fallback_layer(f"Earth Engine unavailable: {reason}", source="earth_engine")
        gathered.update({
            "jrc": {"status": "permission_denied", "reason": reason, "notes": [f"Earth Engine unavailable: {reason}"]},
            "flood": dict(unavailable),
            "turbidity": dict(unavailable),
            "chlorophyll": dict(unavailable),
            "soil": dict(unavailable),
            "drought": dict(unavailable),
            "glacier": dict(unavailable),
            "timeline": {
                "status": "permission_denied",
                "reason": reason,
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
            },
        })
    else:
        tasks = {
            "nearest": asyncio.to_thread(find_nearest_water_body, c_lat, c_lon),
            "recent": asyncio.to_thread(get_recent_context, c_lat, c_lon),
            "jrc": asyncio.to_thread(jrc_aoi_context, geom_ee),
            "flood": asyncio.to_thread(flood_aoi_context, geom_ee, buffer_km),
            "turbidity": asyncio.to_thread(turbidity_aoi_context, geom_ee, buffer_km),
            "chlorophyll": asyncio.to_thread(chlorophyll_aoi_context, geom_ee, buffer_km),
            "soil": asyncio.to_thread(soil_moisture_aoi_context, geom_ee, buffer_km),
            "drought": asyncio.to_thread(drought_indices_aoi_context, geom_ee),
            "glacier": asyncio.to_thread(glacier_retreat_aoi_context, geom_ee),
        }
        tasks["timeline"] = asyncio.to_thread(timeline_context_aoi, geom_ee, None, 0, aoi_area)
        gathered = await _gather_named(tasks, timeout_seconds=ANALYSIS_TASK_TIMEOUT_SECONDS)

    nearest = gathered.get("nearest") or {}
    recent = gathered.get("recent") or {}
    jrc = gathered.get("jrc") or {}
    flood = _with_stac_fallback(gathered.get("flood") or {}, recent, "sentinel1", "Sentinel-1")
    turbidity = _with_stac_fallback(gathered.get("turbidity") or {}, recent, "sentinel2", "Sentinel-2")
    chlorophyll = _with_stac_fallback(gathered.get("chlorophyll") or {}, recent, "sentinel3", "Sentinel-3 OLCI")
    soil = _with_stac_fallback(gathered.get("soil") or {}, recent, "sentinel1", "Sentinel-1 soil-moisture proxy")
    drought = gathered.get("drought") or {}
    glacier = _with_stac_fallback(gathered.get("glacier") or {}, recent, "landsat", "Landsat")
    hist = gathered.get("timeline") or []

    water_quality = water_quality_from_proxies(turbidity, chlorophyll)

    flood_l = _to_layer(flood)
    turb_l = _to_layer(turbidity)
    chl_l = _to_layer(chlorophyll)
    soil_l = _to_layer(soil)
    drought_l = _to_layer(drought)
    glacier_l = _to_layer(glacier)
    quality_l = _to_layer(water_quality)

    response = AOIAnalysisResponse(
        geometry=GeometryInput(type=clean_geometry.get("type"), coordinates=clean_geometry.get("coordinates")),
        label=label,
        stats=AreaStats(
            area_km2=round(float(aoi_area), 4),
            perimeter_km=round(float(per_km), 4),
            centroid=GeoPoint(lat=c_lat, lon=c_lon),
            bbox=[poly.bounds[0], poly.bounds[1], poly.bounds[2], poly.bounds[3]],
        ),
        centroid=GeoPoint(lat=c_lat, lon=c_lon),
        summary_card=_summary(flood_l, turb_l, chl_l, quality_l, soil_l, drought_l, glacier_l, nearest),
        nearest_water_body=WaterFeatureContext(**nearest) if nearest else None,
        source_dataset="JRC + Natural Earth + Sentinel-1/2/3 + SMAP + CHIRPS/TerraClimate",
        data_timestamp=_pick_timestamp(flood, turbidity, chlorophyll, soil, drought, glacier, jrc),
        flood=flood_l,
        turbidity=turb_l,
        chlorophyll=chl_l,
        water_quality=quality_l,
        soil_moisture=soil_l,
        drought=drought_l,
        glacier=glacier_l,
        historical_timeline=_timeline_points(hist),
        trend_summary=_trend_summary(hist, flood, turbidity, chlorophyll, drought),
        flags=_flags(flood_l, turb_l, chl_l, quality_l, soil_l, drought_l, glacier_l, nearest),
        sources=_sources(recent),
        methodology=_methodology(),
        nearby_context={
            "buffer_km": buffer_km,
            "aoi_area_km2": round(float(aoi_area), 4),
            "polygon_perimeter_km": round(float(per_km), 4),
            "jrc": jrc,
            "recent_scene_context": {
                "sentinel1": recent.get("sentinel1").model_dump() if recent.get("sentinel1") else None,
                "sentinel2": recent.get("sentinel2").model_dump() if recent.get("sentinel2") else None,
                "sentinel3": recent.get("sentinel3").model_dump() if recent.get("sentinel3") else None,
                "landsat": recent.get("landsat").model_dump() if recent.get("landsat") else None,
            },
        },
        uncertainty={
            "notes": [
                "Flood detection is an EO indicator, not a hydraulic flood-depth model.",
                "Turbidity and chlorophyll are proxy estimates and require local calibration for regulatory use.",
                "Clouds, adjacency effects, and mixed pixels can bias optical water products.",
                "Soil moisture and drought outputs are proxies if full SMAP or climate product access is limited.",
                "Glacier retreat is only meaningful where snow/ice actually exists.",
            ],
        },
        download_links={
            "flood_tif": "/aoi/tif",
            "turbidity_tif": "/aoi/tif",
            "chlorophyll_tif": "/aoi/tif",
            "water_quality_tif": "/aoi/tif",
            "soil_moisture_tif": "/aoi/tif",
            "drought_tif": "/aoi/tif",
            "glacier_tif": "/aoi/tif",
        },
    )
    cache_set("aoi", cache_key, response)
    return response


async def export_point_tif(lat: float, lon: float, layer: str, buffer_km: float = 5.0, scale_m: int = 30) -> str:
    cache_key = stable_cache_key("point_tif", {"lat": round(float(lat), 5), "lon": round(float(lon), 5), "layer": layer, "buffer_km": buffer_km, "scale_m": scale_m})
    cached_path = cache_get("raster", cache_key)
    if cached_path:
        return cached_path
    path = await asyncio.to_thread(export_geotiff_point, lat, lon, layer, buffer_km, scale_m)
    return cache_set("raster", cache_key, path)


async def export_aoi_tif(geometry: dict, layer: str, buffer_km: float = 5.0, scale_m: int = 30, label: str = None) -> str:
    _, clean_geometry = normalize_aoi_geometry(geometry)
    cache_key = stable_cache_key("aoi_tif", {"geometry": clean_geometry, "layer": layer, "buffer_km": buffer_km, "scale_m": scale_m, "label": label})
    cached_path = cache_get("raster", cache_key)
    if cached_path:
        return cached_path
    path = await asyncio.to_thread(export_geotiff_aoi, clean_geometry, layer, buffer_km, scale_m, label)
    return cache_set("raster", cache_key, path)
