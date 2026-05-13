"""Agriculture / food-security orchestrator.

Coordinates the four extractors (cropland extent, cropland conversion,
phenology, food security) across a year range for a single AOI.

The orchestrator **never crashes** — if all extractors fail for all years
it still returns a valid ``AgricultureAnalysisResponse`` with every record
carrying ``status: "unavailable"``.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.models.agriculture_models import (
    AgricultureAnalysisResponse,
    CroplandTransition,
    FoodSecurityRecord,
    PhenologyRecord,
    YearlyRecord,
    YearOverYearChange,
)
from app.models.water_models import (
    AreaStats,
    GeoPoint,
    GeometryInput,
    MethodologyItem,
    SourceRef,
)
from app.services.cache_service import get as cache_get, set as cache_set, stable_cache_key
from app.services.dynamic_world_service import (
    cropland_conversion_yearly,
    cropland_extent_yearly,
    cropland_series_yearly,
)
from app.services.fewsnet_service import food_security_yearly
from app.services.gee_service import auth_status, initialize_earth_engine
from app.services.sentinel2_phenology_service import phenology_series_yearly, phenology_yearly
from app.services.utils import (
    is_valid_aoi_for_africa,
    normalize_aoi_geometry,
    polygon_area_km2,
    polygon_perimeter_km,
)

_SETTINGS = get_settings()
logger = logging.getLogger(__name__)

_CACHE_VERSION = "agri-v2-batched"
_TASK_TIMEOUT = 120  # seconds per extractor call


# ── Public API ───────────────────────────────────────────────────────────


async def analyze_agriculture_aoi(
    geometry: dict,
    label: Optional[str] = None,
    year_start: int = 2022,
    year_end: Optional[int] = None,
) -> AgricultureAnalysisResponse:
    """Run all agriculture extractors for *geometry* across the year range."""

    if year_end is None:
        year_end = datetime.datetime.now().year

    # ── Geometry validation ──────────────────────────────────────────
    poly, clean_geometry = normalize_aoi_geometry(geometry)
    if not is_valid_aoi_for_africa(poly):
        raise ValueError("AOI is outside the Africa analysis extent")

    centroid = poly.centroid
    c_lat, c_lon = float(centroid.y), float(centroid.x)
    aoi_area = polygon_area_km2(poly)
    per_km = polygon_perimeter_km(poly)

    # ── Cache ────────────────────────────────────────────────────────
    ee_ready = bool(auth_status().get("ready"))
    cache_key = stable_cache_key("agriculture", {
        "version": _CACHE_VERSION,
        "geometry": clean_geometry,
        "year_start": year_start,
        "year_end": year_end,
        "ee_ready": ee_ready,
    })
    cached = cache_get("agriculture", cache_key)
    if cached is not None:
        return cached

    # ── EE geometry ──────────────────────────────────────────────────
    geom_ee = None
    if ee_ready:
        try:
            from app.services.gee_service import ee  # type: ignore
            geom_ee = ee.Geometry(clean_geometry)
        except Exception as exc:
            logger.warning("Failed to build EE geometry: %s", exc)

    years = list(range(year_start, year_end + 1))

    if geom_ee is not None:
        # Run EE-heavy aggregations sequentially to avoid Earth Engine 429 /
        # "Too many concurrent aggregations" failures under local dashboard use.
        dw_result = await _safe_run(
            "dynamic_world",
            asyncio.to_thread(cropland_series_yearly, geom_ee, years),
        )
        if isinstance(dw_result, tuple) and len(dw_result) == 2:
            extent_results, conversion_results = dw_result
        else:
            extent_results = [_make_unavailable_extent(year) for year in years]
            conversion_results = [_make_unavailable_conversion(year, year - 1) for year in years[1:]]

        pheno_result = await _safe_run(
            "phenology",
            asyncio.to_thread(phenology_series_yearly, geom_ee, years),
        )
        phenology_results = pheno_result if isinstance(pheno_result, list) else [_make_unavailable_phenology(year) for year in years]
    else:
        extent_results = [_make_unavailable_extent(year) for year in years]
        conversion_results = [_make_unavailable_conversion(year, year - 1) for year in years[1:]]
        phenology_results = [_make_unavailable_phenology(year) for year in years]

    gathered_food = await _safe_gather({
        f"food_{year}": asyncio.to_thread(food_security_yearly, c_lat, c_lon, year)
        for year in years
    })
    food_results = [
        gathered_food.get(f"food_{year}") or _make_unavailable_food(year)
        for year in years
    ]
    summary = _build_summary(extent_results, food_results)
    yearly_changes = _yearly_changes(extent_results)

    all_statuses = (
        [r.status for r in extent_results]
        + [r.status for r in phenology_results]
        + [r.status for r in food_results]
    )
    if any(s == "ok" for s in all_statuses):
        overall_status = "ok" if all(s == "ok" for s in all_statuses) else "partial"
    else:
        overall_status = "unavailable"

    notes: List[str] = []
    if not ee_ready:
        notes.append("Earth Engine is not available. Cropland, conversion, and phenology layers are unavailable.")

    response = AgricultureAnalysisResponse(
        geometry=GeometryInput(
            type=clean_geometry.get("type"),
            coordinates=clean_geometry.get("coordinates"),
        ),
        stats=AreaStats(
            area_km2=round(float(aoi_area), 4),
            perimeter_km=round(float(per_km), 4),
            centroid=GeoPoint(lat=c_lat, lon=c_lon),
            bbox=[poly.bounds[0], poly.bounds[1], poly.bounds[2], poly.bounds[3]],
        ),
        centroid=GeoPoint(lat=c_lat, lon=c_lon),
        label=label,
        year_range=years,
        cropland_extent=extent_results,
        cropland_conversion=conversion_results,
        phenology=phenology_results,
        food_security=food_results,
        yearly_changes=yearly_changes,
        summary=summary,
        methodology=_methodology(),
        sources=_sources(ee_ready),
        status=overall_status,
        notes=notes,
    )

    cache_set("agriculture", cache_key, response)
    return response


# ── Internal helpers ─────────────────────────────────────────────────────


async def _safe_gather(tasks: Dict[str, Any]) -> Dict[str, Any]:
    """Run tasks concurrently; failures return None instead of crashing."""
    names = list(tasks.keys())
    coros = []
    for name, task in tasks.items():
        coros.append(_safe_run(name, task))
    results = await asyncio.gather(*coros)
    return dict(zip(names, results))


async def _safe_run(name: str, task: Any) -> Any:
    try:
        return await asyncio.wait_for(task, timeout=_TASK_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Agriculture task %s timed out after %ds", name, _TASK_TIMEOUT)
        return None
    except Exception as exc:
        logger.warning("Agriculture task %s failed: %s", name, exc, exc_info=True)
        return None


def _make_unavailable_extent(year: int) -> YearlyRecord:
    return YearlyRecord(
        datatype="cropland_extent",
        description="Total cropland area within AOI",
        source="Dynamic World",
        year=year,
        value=None,
        unit="km2",
        confidence=0.0,
        coverage=0.0,
        status="unavailable",
        method="",
        timestamp=f"{year}-12-31",
        notes=[
            "Earth Engine unavailable for this analysis. ",
            "Cropland extent could not be computed. Please try again or check system status.",
        ],
    )


def _make_unavailable_conversion(year: int, prev: int) -> CroplandTransition:
    return CroplandTransition(
        year=year,
        previous_year=prev,
        gain_km2=None,
        loss_km2=None,
        net_change_km2=None,
        confidence=0.0,
        coverage=0.0,
        status="unavailable",
        notes=[
            "Earth Engine unavailable for this analysis. ",
            f"Cropland conversion between {prev} and {year} could not be computed.",
        ],
    )


def _make_unavailable_phenology(year: int) -> PhenologyRecord:
    return PhenologyRecord(
        year=year,
        monthly_ndvi=[None] * 12,
        monthly_evi=[None] * 12,
        valid_month_count=0,
        scene_count=0,
        status="unavailable",
        notes=[
            "Earth Engine unavailable for this analysis. ",
            f"Crop phenology (NDVI/EVI) could not be extracted for {year}.",
        ],
    )


def _make_unavailable_food(year: int) -> FoodSecurityRecord:
    return FoodSecurityRecord(
        year=year,
        phase=None,
        phase_label=None,
        confidence=0.0,
        source_region=None,
        status="unavailable",
        notes=[
            f"Food security data could not be retrieved for {year}. ",
            "This may indicate lack of FEWS NET coverage for the region, missing historical data, ",
            "or API connectivity issues. Visit https://fews.net for more information.",
        ],
    )


def _yearly_changes(extent: List[YearlyRecord]) -> List[YearOverYearChange]:
    changes: List[YearOverYearChange] = []
    ordered = sorted(extent, key=lambda record: record.year)
    for prev, curr in zip(ordered, ordered[1:]):
        if prev.value is None or curr.value is None:
            changes.append(YearOverYearChange(
                datatype="cropland_extent",
                year=curr.year,
                previous_year=prev.year,
                value_current=curr.value,
                value_previous=prev.value,
                unit="km2",
                status="unavailable",
                notes=["Cropland change unavailable because one or both years lack valid cropland extent."],
            ))
            continue

        absolute = round(float(curr.value) - float(prev.value), 4)
        percent_change = None
        if float(prev.value) != 0.0:
            percent_change = round((absolute / float(prev.value)) * 100.0, 3)
        changes.append(YearOverYearChange(
            datatype="cropland_extent",
            year=curr.year,
            previous_year=prev.year,
            value_current=curr.value,
            value_previous=prev.value,
            absolute_change=absolute,
            percent_change=percent_change,
            unit="km2",
            status="ok",
            notes=[] if percent_change is not None else ["Percent change is undefined because the previous year value is 0 km2."],
        ))
    return changes


def _build_summary(
    extent: List[YearlyRecord],
    food: List[FoodSecurityRecord],
) -> Dict[str, Any]:
    """Build a summary dict from the latest available values."""
    summary: Dict[str, Any] = {}

    # Latest cropland area
    ok_extents = [r for r in extent if r.status == "ok" and r.value is not None]
    if ok_extents:
        latest = ok_extents[-1]
        summary["latest_cropland_km2"] = latest.value
        summary["latest_cropland_year"] = latest.year
        summary["latest_cropland_confidence"] = latest.confidence
        summary["latest_cropland_coverage"] = latest.coverage
        
        if len(ok_extents) >= 2:
            prev = ok_extents[-2]
            if prev.value is not None and latest.value is not None:
                diff = latest.value - prev.value
                if abs(diff) < 1.0:
                    summary["cropland_trend"] = "stable"
                elif diff > 0:
                    summary["cropland_trend"] = "increasing"
                else:
                    summary["cropland_trend"] = "decreasing"
            else:
                summary["cropland_trend"] = "unknown"
        else:
            summary["cropland_trend"] = "unknown"
        
        # Average cropland across all years
        avg_value = sum(r.value for r in ok_extents if r.value is not None) / len(ok_extents)
        summary["average_cropland_km2"] = round(avg_value, 4)
        
        # Data quality metrics
        avg_confidence = sum(r.confidence for r in ok_extents) / len(ok_extents)
        avg_coverage = sum(r.coverage for r in ok_extents) / len(ok_extents)
        summary["average_confidence"] = round(avg_confidence, 2)
        summary["average_coverage"] = round(avg_coverage, 2)
        
        # Detect if cropland is very low (< 0.1 km²)
        if latest.value < 0.1:
            summary["cropland_interpretation"] = (
                "Very low to minimal cropland detected. The AOI may be primarily non-agricultural, "
                "or crops may be below the detection threshold (0.4 probability). "
                "This may be typical for certain land types (grassland, urban, water bodies)."
            )
    else:
        summary["latest_cropland_km2"] = None
        summary["latest_cropland_year"] = None
        summary["latest_cropland_confidence"] = None
        summary["latest_cropland_coverage"] = None
        summary["cropland_trend"] = "unknown"
        summary["average_cropland_km2"] = None
        summary["average_confidence"] = None
        summary["average_coverage"] = None
        summary["cropland_interpretation"] = (
            "No valid cropland data available. Earth Engine may be unavailable, "
            "or the region may lack Dynamic World coverage."
        )

    # Latest food security phase
    ok_food = [r for r in food if r.status == "ok" and r.phase is not None]
    if ok_food:
        latest_f = ok_food[-1]
        summary["latest_food_phase"] = latest_f.phase
        summary["latest_food_label"] = latest_f.phase_label
        summary["latest_food_year"] = latest_f.year
        summary["latest_food_confidence"] = latest_f.confidence
    else:
        summary["latest_food_phase"] = None
        summary["latest_food_label"] = None
        summary["latest_food_year"] = None
        summary["latest_food_confidence"] = None
        summary["food_security_note"] = (
            "FEWS NET food security data is not currently available for this AOI. "
            "This may indicate insufficient data or that the region is outside FEWS NET coverage."
        )

    # Count of available years
    summary["years_with_data"] = sum(1 for r in extent if r.status in ("ok", "partial"))
    summary["total_years"] = len(extent)
    summary["data_completeness"] = round(summary["years_with_data"] / max(summary["total_years"], 1), 2)

    return summary


def _methodology() -> List[MethodologyItem]:
    return [
        MethodologyItem(
            title="Cropland extent",
            description=(
                "Dynamic World annual median crop probability thresholded at 0.4 (40% probability). "
                "Pixel-area summation over AOI gives total cropland in km². "
                "A threshold of 0.4 balances crop detection sensitivity with false-positive reduction. "
                "Values near zero may indicate sparse agriculture, grassland, or non-agricultural dominance."
            ),
        ),
        MethodologyItem(
            title="Cropland conversion",
            description=(
                "Dynamic World majority-class comparison between consecutive years. "
                "Gain = non-crop→crop (agricultural expansion), Loss = crop→non-crop (abandonment), "
                "Net = gain − loss. Low values may reflect marginal agriculture or misclassification."
            ),
        ),
        MethodologyItem(
            title="Crop phenology",
            description=(
                "Sentinel-2 SR Harmonized cloud-masked monthly median composites. "
                "NDVI (Normalized Difference Vegetation Index) and EVI (Enhanced Vegetation Index) "
                "reduced to monthly AOI means. Season milestones from NDVI > 0.3 threshold. "
                "Missing months indicate persistent cloud cover or insufficient valid observations."
            ),
        ),
        MethodologyItem(
            title="Food security classification",
            description=(
                "FEWS NET IPC-phase lookup by AOI centroid. This is a centroid-based approximation "
                "and does NOT represent full AOI-level food-security analysis. "
                "IPC phase is categorical (1=Minimal, 2=Stressed, 3=Crisis, 4=Emergency, 5=Famine) "
                "and is NEVER converted to a continuous score. 'Unavailable' may indicate lack of "
                "FEWS NET coverage for the region or year."
            ),
        ),
        MethodologyItem(
            title="Data quality indicators",
            description=(
                "Confidence reflects data scene count and AOI coverage (0–1 scale). "
                "Coverage reflects the fraction of the AOI with valid satellite observations (0–1 scale). "
                "Low confidence/coverage may indicate cloud cover, satellite gaps, or regional data scarcity."
            ),
        ),
    ]


def _sources(ee_ready: bool) -> List[SourceRef]:
    sources = [
        SourceRef(
            name="Dynamic World",
            collection="GOOGLE/DYNAMICWORLD/V1",
            kind="land_cover",
            notes="Near-real-time land use / land cover at 10 m resolution.",
        ),
        SourceRef(
            name="Sentinel-2 SR Harmonized",
            collection="COPERNICUS/S2_SR_HARMONIZED",
            kind="optical",
            notes="Surface reflectance with SCL cloud masking for NDVI/EVI phenology.",
        ),
        SourceRef(
            name="FEWS NET",
            url="https://fews.net/",
            kind="food_security",
            notes="Famine Early Warning Systems Network IPC-phase classifications.",
        ),
    ]
    if not ee_ready:
        sources[0].notes = "Unavailable — Earth Engine not configured."
        sources[1].notes = "Unavailable — Earth Engine not configured."
    return sources
