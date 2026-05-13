"""Dynamic World cropland analytics — extent and year-over-year conversion.

Uses ``GOOGLE/DYNAMICWORLD/V1`` (available 2015-present, best from 2018).
All functions are designed to run inside ``asyncio.to_thread`` and are
individually wrapped so a failure in one year never blocks others.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.models.agriculture_models import CroplandTransition, YearlyRecord
from app.services.gee_service import (
    initialize_earth_engine,
    safe_feature_rows,
    safe_getinfo,
    safe_reduce_mean,
    safe_reduce_sum,
    safe_area_m2,
)
from app.services.utils import clamp

logger = logging.getLogger(__name__)

_DW_COLLECTION = "GOOGLE/DYNAMICWORLD/V1"
_CROP_CLASS_INDEX = 4  # "crops" in Dynamic World class list
_CROP_PROB_THRESHOLD = 0.4  # pixels above this probability are classified as crop
_DW_BANDS = ["water", "trees", "grass", "flooded_vegetation", "crops",
             "shrub_and_scrub", "built", "bare", "snow_and_ice"]

# Processing version — bump when changing thresholds or logic
_PROCESSING_VERSION = "dw-crop-v1"


def _ee():
    """Lazy import of the ee module — avoids import-time errors."""
    from app.services.gee_service import ee  # type: ignore
    return ee


def cropland_series_yearly(geom_ee: Any, years: List[int]) -> Tuple[List[YearlyRecord], List[CroplandTransition]]:
    """Batch Dynamic World cropland extent and conversion for many years."""
    if not initialize_earth_engine():
        return (
            [_unavailable_extent(year, "Earth Engine not initialized") for year in years],
            [_unavailable_transition(year, year - 1, "Earth Engine not initialized") for year in years[1:]],
        )

    ee = _ee()
    if ee is None:
        return (
            [_unavailable_extent(year, "Earth Engine library unavailable") for year in years],
            [_unavailable_transition(year, year - 1, "Earth Engine library unavailable") for year in years[1:]],
        )

    def _annual_crop_image(year_value):
        year = ee.Number(year_value).toInt()
        col = (
            ee.ImageCollection(_DW_COLLECTION)
            .filterBounds(geom_ee)
            .filterDate(ee.Date.fromYMD(year, 1, 1), ee.Date.fromYMD(year, 12, 31))
            .select("crops")
        )
        count = col.size()
        empty = ee.Image.constant(0).rename("crops").updateMask(ee.Image.constant(0))
        crop_prob = ee.Image(ee.Algorithms.If(count.gt(0), col.median(), empty)).clip(geom_ee)
        return crop_prob.gt(_CROP_PROB_THRESHOLD).rename("crop").set("scene_count", count)

    def _year_feature(year_value):
        year = ee.Number(year_value).toInt()
        crop = _annual_crop_image(year)
        count = ee.Number(crop.get("scene_count"))
        area = crop.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("crop")
        valid_area = crop.mask().rename("valid").multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("valid")
        total_area = ee.Number(geom_ee.area(maxError=1)).max(1)
        area_m2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(area, None), 0, area))
        valid_m2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(valid_area, None), 0, valid_area))
        coverage = valid_m2.divide(total_area).max(0).min(1)
        confidence = ee.Number(0.3).add(count.divide(50).min(0.5)).add(coverage.min(0.2)).max(0).min(1)
        status = ee.Algorithms.If(count.eq(0), "unavailable", ee.Algorithms.If(coverage.lt(0.3), "partial", "ok"))
        return ee.Feature(None, {
            "year": year,
            "value": area_m2.divide(1e6),
            "coverage": coverage,
            "confidence": confidence,
            "scene_count": count,
            "status": status,
        })

    def _transition_feature(year_value):
        year = ee.Number(year_value).toInt()
        prev = year.subtract(1).toInt()
        prev_crop = _annual_crop_image(prev)
        curr_crop = _annual_crop_image(year)
        count_prev = ee.Number(prev_crop.get("scene_count"))
        count_curr = ee.Number(curr_crop.get("scene_count"))
        gain = prev_crop.Not().And(curr_crop).rename("gain")
        loss = prev_crop.And(curr_crop.Not()).rename("loss")
        gain_area = gain.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("gain")
        loss_area = loss.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("loss")
        prev_valid = prev_crop.mask().rename("valid").multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("valid")
        curr_valid = curr_crop.mask().rename("valid").multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=10,
            bestEffort=True,
            maxPixels=1e8,
        ).get("valid")
        total_area = ee.Number(geom_ee.area(maxError=1)).max(1)
        gain_km2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(gain_area, None), 0, gain_area)).divide(1e6)
        loss_km2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(loss_area, None), 0, loss_area)).divide(1e6)
        prev_valid_m2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(prev_valid, None), 0, prev_valid))
        curr_valid_m2 = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(curr_valid, None), 0, curr_valid))
        coverage = prev_valid_m2.min(curr_valid_m2).divide(total_area).max(0).min(1)
        min_scenes = count_prev.min(count_curr)
        confidence = ee.Number(0.3).add(min_scenes.divide(50).min(0.5)).add(coverage.min(0.2)).max(0).min(1)
        missing = count_prev.eq(0).Or(count_curr.eq(0))
        status = ee.Algorithms.If(missing, "unavailable", ee.Algorithms.If(min_scenes.lt(10), "low_confidence", "ok"))
        return ee.Feature(None, {
            "year": year,
            "previous_year": prev,
            "gain_km2": gain_km2,
            "loss_km2": loss_km2,
            "net_change_km2": gain_km2.subtract(loss_km2),
            "coverage": coverage,
            "confidence": confidence,
            "status": status,
            "count_prev": count_prev,
            "count_curr": count_curr,
        })

    try:
        extent_rows = safe_feature_rows(
            ee.FeatureCollection(ee.List(years).map(_year_feature)),
            [],
            label="dw_extent_batched",
        )
        transition_years = years[1:]
        transition_rows = safe_feature_rows(
            ee.FeatureCollection(ee.List(transition_years).map(_transition_feature)),
            [],
            label="dw_conversion_batched",
        ) if transition_years else []
    except Exception as exc:
        logger.warning("Batched Dynamic World extraction failed: %s", exc, exc_info=True)
        return (
            [_unavailable_extent(year, f"Processing error: {exc}") for year in years],
            [_unavailable_transition(year, year - 1, f"Processing error: {exc}") for year in years[1:]],
        )

    extent_by_year = {int(row.get("year")): row for row in extent_rows if row.get("year") is not None}
    extent_records: List[YearlyRecord] = []
    for year in years:
        row = extent_by_year.get(int(year))
        if not row:
            extent_records.append(_unavailable_extent(year, "No Dynamic World yearly row returned."))
            continue
        status = row.get("status") or "unavailable"
        value = row.get("value") if status in {"ok", "partial"} else None
        coverage = float(row.get("coverage") or 0.0)
        confidence = float(row.get("confidence") or 0.0)
        notes = [f"Dynamic World scenes: {int(row.get('scene_count') or 0)}. Crop probability threshold: {_CROP_PROB_THRESHOLD}."]
        if status == "partial":
            notes.insert(0, f"Low AOI coverage ({coverage:.0%}). Cropland area may be underestimated.")
        if status == "unavailable":
            notes.insert(0, f"No Dynamic World scenes for {year}.")
        extent_records.append(YearlyRecord(
            datatype="cropland_extent",
            description="Total cropland area within AOI",
            source="Dynamic World",
            year=year,
            value=round(float(value), 4) if value is not None else None,
            unit="km2",
            confidence=round(confidence, 2),
            coverage=round(coverage, 2),
            status=status,
            method=f"annual median crop probability > {_CROP_PROB_THRESHOLD}, pixel-area sum ({_PROCESSING_VERSION})",
            timestamp=f"{year}-12-31",
            notes=notes,
        ))

    conversion_records: List[CroplandTransition] = []
    for row in transition_rows:
        year = int(row.get("year"))
        prev = int(row.get("previous_year"))
        status = row.get("status") or "unavailable"
        notes = [f"Scenes: {prev}={int(row.get('count_prev') or 0)}, {year}={int(row.get('count_curr') or 0)}."]
        if status == "low_confidence":
            notes.append("Low scene count in one or both years; transition confidence is reduced.")
        if status == "unavailable":
            notes.append("Dynamic World scenes were missing for one or both years.")
        conversion_records.append(CroplandTransition(
            year=year,
            previous_year=prev,
            gain_km2=round(float(row["gain_km2"]), 4) if row.get("gain_km2") is not None and status != "unavailable" else None,
            loss_km2=round(float(row["loss_km2"]), 4) if row.get("loss_km2") is not None and status != "unavailable" else None,
            net_change_km2=round(float(row["net_change_km2"]), 4) if row.get("net_change_km2") is not None and status != "unavailable" else None,
            confidence=round(float(row.get("confidence") or 0.0), 2),
            coverage=round(float(row.get("coverage") or 0.0), 2),
            status=status,
            method=f"annual crop-probability transition ({_PROCESSING_VERSION})",
            notes=notes,
        ))

    return extent_records, conversion_records


# ── Cropland extent ──────────────────────────────────────────────────────


def cropland_extent_yearly(geom_ee: Any, year: int) -> YearlyRecord:
    """Compute total cropland area within *geom_ee* for a calendar year.

    Scientific recipe
    -----------------
    1. Filter Dynamic World to the calendar year.
    2. Select the ``crops`` probability band.
    3. Compute annual **median** probability per pixel.
    4. Threshold > 0.4 → binary crop mask.
    5. Multiply by ``pixelArea`` and ``reduceRegion(sum)`` → area m².
    6. Convert to km².
    7. Compute coverage = valid pixels / total AOI pixels.
    """
    if not initialize_earth_engine():
        return _unavailable_extent(year, "Earth Engine not initialized")

    ee = _ee()
    if ee is None:
        return _unavailable_extent(year, "Earth Engine library unavailable")

    try:
        start = f"{year}-01-01"
        end = f"{year}-12-31"

        col = (
            ee.ImageCollection(_DW_COLLECTION)
            .filterBounds(geom_ee)
            .filterDate(start, end)
            .select("crops")
        )

        scene_count = int(safe_getinfo(col.size(), 0, label=f"dw_extent_{year}_count") or 0)
        if scene_count == 0:
            return _unavailable_extent(year, f"No Dynamic World scenes for {year}")

        # Annual median crop probability
        median_crop = col.median().clip(geom_ee)

        # Binary crop mask
        crop_mask = median_crop.gt(_CROP_PROB_THRESHOLD).rename("crop")

        # Area calculation
        crop_area_img = crop_mask.multiply(ee.Image.pixelArea())
        area_result = safe_reduce_sum(
            crop_area_img, geom_ee, scale=10, label=f"dw_extent_{year}_area"
        )
        crop_area_m2 = float(next(iter(area_result.values()), 0.0)) if area_result else 0.0
        crop_area_km2 = crop_area_m2 / 1e6

        # Coverage: fraction of AOI with valid DW pixels
        valid_mask = median_crop.mask()
        valid_area_result = safe_reduce_sum(
            valid_mask.multiply(ee.Image.pixelArea()), geom_ee, scale=10,
            label=f"dw_extent_{year}_valid",
        )
        valid_area_m2 = float(next(iter(valid_area_result.values()), 0.0)) if valid_area_result else 0.0
        total_area_m2 = safe_area_m2(geom_ee, 0.0, label=f"dw_extent_{year}_total") or 1.0
        coverage = clamp(valid_area_m2 / max(total_area_m2, 1.0))

        # Confidence from scene count and coverage
        confidence = clamp(
            0.3 + min(0.5, scene_count / 50.0) + min(0.2, coverage)
        )

        # Status
        if coverage < 0.3:
            status = "partial"
            notes = [f"Low AOI coverage ({coverage:.0%}). Cropland area may be underestimated."]
        else:
            status = "ok"
            notes = []

        notes.append(f"Dynamic World scenes: {scene_count}. Crop probability threshold: {_CROP_PROB_THRESHOLD}.")

        return YearlyRecord(
            datatype="cropland_extent",
            description="Total cropland area within AOI",
            source="Dynamic World",
            year=year,
            value=round(crop_area_km2, 4),
            unit="km2",
            confidence=round(confidence, 2),
            coverage=round(coverage, 2),
            status=status,
            method=f"annual median crop probability > {_CROP_PROB_THRESHOLD}, pixel-area sum ({_PROCESSING_VERSION})",
            timestamp=f"{year}-12-31",
            notes=notes,
        )

    except Exception as exc:
        logger.warning("Cropland extent failed for %d: %s", year, exc, exc_info=True)
        return _unavailable_extent(year, f"Processing error: {exc}")


def _unavailable_extent(year: int, reason: str) -> YearlyRecord:
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
        method=_PROCESSING_VERSION,
        timestamp=f"{year}-12-31",
        notes=[reason],
    )


# ── Cropland conversion ─────────────────────────────────────────────────


def cropland_conversion_yearly(
    geom_ee: Any,
    year_current: int,
    year_previous: int,
) -> CroplandTransition:
    """Year-over-year cropland gain, loss, and net change.

    Scientific recipe
    -----------------
    1. For each year: select all 9 DW class-probability bands,
       compute annual median, then ``arrayArgmax`` → majority class.
    2. Binary crop mask: majority == 4 (crops index).
    3. Gain = NOT(crop_prev) AND crop_curr → area.
    4. Loss = crop_prev AND NOT(crop_curr) → area.
    5. Net = gain − loss.
    """
    if not initialize_earth_engine():
        return _unavailable_transition(year_current, year_previous, "Earth Engine not initialized")

    ee = _ee()
    if ee is None:
        return _unavailable_transition(year_current, year_previous, "Earth Engine library unavailable")

    try:
        def _majority_crop_mask(year: int):
            col = (
                ee.ImageCollection(_DW_COLLECTION)
                .filterBounds(geom_ee)
                .filterDate(f"{year}-01-01", f"{year}-12-31")
                .select(_DW_BANDS)
            )
            count = int(safe_getinfo(col.size(), 0, label=f"dw_conv_{year}_count") or 0)
            if count == 0:
                return None, 0

            median = col.median().clip(geom_ee)
            # Stack bands into array and find argmax
            stacked = median.toArray()
            majority_idx = stacked.arrayArgmax().arrayGet(0).rename("majority")
            crop_mask = majority_idx.eq(_CROP_CLASS_INDEX).rename("crop")
            return crop_mask, count

        crop_prev, count_prev = _majority_crop_mask(year_previous)
        crop_curr, count_curr = _majority_crop_mask(year_current)

        if crop_prev is None or crop_curr is None:
            missing = []
            if crop_prev is None:
                missing.append(str(year_previous))
            if crop_curr is None:
                missing.append(str(year_current))
            return _unavailable_transition(
                year_current, year_previous,
                f"No Dynamic World scenes for year(s): {', '.join(missing)}",
            )

        # Gain: non-crop → crop
        gain_mask = crop_prev.Not().And(crop_curr)
        gain_result = safe_reduce_sum(
            gain_mask.multiply(ee.Image.pixelArea()), geom_ee, scale=10,
            label=f"dw_conv_{year_current}_gain",
        )
        gain_m2 = float(next(iter(gain_result.values()), 0.0)) if gain_result else 0.0

        # Loss: crop → non-crop
        loss_mask = crop_prev.And(crop_curr.Not())
        loss_result = safe_reduce_sum(
            loss_mask.multiply(ee.Image.pixelArea()), geom_ee, scale=10,
            label=f"dw_conv_{year_current}_loss",
        )
        loss_m2 = float(next(iter(loss_result.values()), 0.0)) if loss_result else 0.0

        gain_km2 = gain_m2 / 1e6
        loss_km2 = loss_m2 / 1e6
        net_km2 = gain_km2 - loss_km2

        # Confidence — lower when scene counts are low
        min_scenes = min(count_prev, count_curr)
        confidence = clamp(0.3 + min(0.5, min_scenes / 50.0))
        status = "ok" if min_scenes >= 10 else "low_confidence"

        notes = [
            f"Scenes: {year_previous}={count_prev}, {year_current}={count_curr}.",
        ]
        if status == "low_confidence":
            notes.append("Low scene count in one or both years; transition confidence is reduced.")

        return CroplandTransition(
            year=year_current,
            previous_year=year_previous,
            gain_km2=round(gain_km2, 4),
            loss_km2=round(loss_km2, 4),
            net_change_km2=round(net_km2, 4),
            confidence=round(confidence, 2),
            coverage=1.0,  # DW covers all land by default
            status=status,
            method=f"majority-class transition ({_PROCESSING_VERSION})",
            notes=notes,
        )

    except Exception as exc:
        logger.warning(
            "Cropland conversion failed for %d→%d: %s",
            year_previous, year_current, exc, exc_info=True,
        )
        return _unavailable_transition(year_current, year_previous, f"Processing error: {exc}")


def _unavailable_transition(year: int, prev_year: int, reason: str) -> CroplandTransition:
    return CroplandTransition(
        year=year,
        previous_year=prev_year,
        gain_km2=None,
        loss_km2=None,
        net_change_km2=None,
        confidence=0.0,
        coverage=0.0,
        status="unavailable",
        method=_PROCESSING_VERSION,
        notes=[reason],
    )
