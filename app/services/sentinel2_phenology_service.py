"""Sentinel-2 crop phenology — cloud-masked monthly NDVI / EVI composites.

Builds a 12-month profile per year from ``COPERNICUS/S2_SR_HARMONIZED``,
detecting season start, peak, and end from the monthly NDVI curve.

Only cloud-masked composites are used — raw cloudy scenes are never plotted.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from app.models.agriculture_models import PhenologyRecord
from app.services.gee_service import (
    initialize_earth_engine,
    safe_feature_rows,
    safe_getinfo,
    safe_reduce_mean,
)
from app.services.sensor_bands import SENTINEL2_SR, mask_sentinel2_sr_scl

logger = logging.getLogger(__name__)

_S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
_MAX_CLOUD_PCT = 60
_PROCESSING_VERSION = "s2-phenology-v1"
_NDVI_SOS_THRESHOLD = 0.3  # season-start/end threshold


def _ee():
    from app.services.gee_service import ee  # type: ignore
    return ee


def phenology_series_yearly(geom_ee: Any, years: List[int]) -> List[PhenologyRecord]:
    """Batch Sentinel-2 monthly NDVI/EVI phenology for several years."""
    if not initialize_earth_engine():
        return [_unavailable_phenology(year, "Earth Engine not initialized") for year in years]

    ee = _ee()
    if ee is None:
        return [_unavailable_phenology(year, "Earth Engine library unavailable") for year in years]

    nir = SENTINEL2_SR["nir"]
    red = SENTINEL2_SR["red"]
    blue = SENTINEL2_SR["blue"]

    def _year_feature(year_value):
        year = ee.Number(year_value).toInt()
        base_col = (
            ee.ImageCollection(_S2_COLLECTION)
            .filterBounds(geom_ee)
            .filterDate(ee.Date.fromYMD(year, 1, 1), ee.Date.fromYMD(year, 12, 31))
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", _MAX_CLOUD_PCT))
            .map(mask_sentinel2_sr_scl)
        )
        total_scenes = base_col.size()
        empty = ee.Image.constant([0, 0, 0]).rename([nir, red, blue]).updateMask(ee.Image.constant(0))

        def _month_value(month_value):
            month = ee.Number(month_value).toInt()
            start = ee.Date.fromYMD(year, month, 1)
            end = start.advance(1, "month")
            m_col = base_col.filterDate(start, end)
            count = m_col.size()
            composite = ee.Image(ee.Algorithms.If(count.gt(0), m_col.median(), empty)).clip(geom_ee)
            ndvi = composite.normalizedDifference([nir, red]).rename("NDVI")
            ndvi_raw = ndvi.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=geom_ee,
                scale=20,
                bestEffort=True,
                maxPixels=1e8,
            ).get("NDVI")
            nir_band = composite.select(nir)
            red_band = composite.select(red)
            blue_band = composite.select(blue)
            evi = (
                nir_band.subtract(red_band)
                .multiply(2.5)
                .divide(nir_band.add(red_band.multiply(6)).subtract(blue_band.multiply(7.5)).add(1))
                .rename("EVI")
            )
            evi_raw = evi.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=geom_ee,
                scale=20,
                bestEffort=True,
                maxPixels=1e8,
            ).get("EVI")
            return ee.Dictionary({
                "ndvi": ee.Algorithms.If(ee.Algorithms.IsEqual(ndvi_raw, None), None, ndvi_raw),
                "evi": ee.Algorithms.If(ee.Algorithms.IsEqual(evi_raw, None), None, evi_raw),
            })

        month_dicts = ee.List.sequence(1, 12).map(_month_value)
        ndvi_values = month_dicts.map(lambda item: ee.Dictionary(item).get("ndvi"))
        evi_values = month_dicts.map(lambda item: ee.Dictionary(item).get("evi"))
        valid_months = ndvi_values.removeAll([None]).length()
        status = ee.Algorithms.If(
            total_scenes.eq(0),
            "unavailable",
            ee.Algorithms.If(valid_months.lt(3), "insufficient_data", ee.Algorithms.If(valid_months.lt(6), "partial", "ok")),
        )
        return ee.Feature(None, {
            "year": year,
            "monthly_ndvi": ndvi_values,
            "monthly_evi": evi_values,
            "valid_month_count": valid_months,
            "scene_count": total_scenes,
            "status": status,
        })

    try:
        rows = safe_feature_rows(
            ee.FeatureCollection(ee.List(years).map(_year_feature)),
            [],
            label="phenology_batched",
        )
    except Exception as exc:
        logger.warning("Batched phenology failed: %s", exc, exc_info=True)
        return [_unavailable_phenology(year, f"Processing error: {exc}") for year in years]

    by_year = {int(row.get("year")): row for row in rows if row.get("year") is not None}
    records: List[PhenologyRecord] = []
    for year in years:
        row = by_year.get(int(year))
        if not row:
            records.append(_unavailable_phenology(year, "No Sentinel-2 phenology yearly row returned."))
            continue
        monthly_ndvi = [
            round(float(value), 4) if value is not None else None
            for value in (row.get("monthly_ndvi") or [None] * 12)
        ]
        monthly_evi = [
            round(float(value), 4) if value is not None else None
            for value in (row.get("monthly_evi") or [None] * 12)
        ]
        valid_months = int(row.get("valid_month_count") or 0)
        total_scenes = int(row.get("scene_count") or 0)
        status = row.get("status") or "unavailable"
        notes: List[str]
        if total_scenes == 0:
            notes = [f"No cloud-filtered Sentinel-2 scenes for {year}"]
        elif valid_months < 3:
            notes = [f"Only {valid_months} month(s) with valid data. Minimum 3 required."]
        elif valid_months < 6:
            notes = [f"{valid_months} months with data. Full profile requires >= 6."]
        else:
            notes = []
        notes.append(f"Total Sentinel-2 scenes used: {total_scenes}. Valid months: {valid_months}.")

        season_start = None
        peak_month = None
        season_end = None
        peak_ndvi = None
        amplitude = None
        valid_values = [(i, v) for i, v in enumerate(monthly_ndvi) if v is not None]
        if valid_values:
            peak_idx, peak_val = max(valid_values, key=lambda x: x[1])
            peak_month = peak_idx + 1
            peak_ndvi = peak_val
            for idx, val in valid_values:
                if val > _NDVI_SOS_THRESHOLD:
                    season_start = idx + 1
                    break
            for idx, val in reversed(valid_values):
                if val > _NDVI_SOS_THRESHOLD:
                    season_end = idx + 1
                    break
            amplitude = round(peak_val - min(v for _, v in valid_values), 4)

        records.append(PhenologyRecord(
            year=year,
            monthly_ndvi=monthly_ndvi,
            monthly_evi=monthly_evi,
            season_start_month=season_start,
            peak_month=peak_month,
            season_end_month=season_end,
            peak_ndvi=peak_ndvi,
            amplitude=amplitude,
            valid_month_count=valid_months,
            scene_count=total_scenes,
            status=status,
            method=f"cloud-masked S2 monthly median composites, NDVI/EVI ({_PROCESSING_VERSION})",
            notes=notes,
        ))
    return records


def phenology_yearly(geom_ee: Any, year: int) -> PhenologyRecord:
    """Build a 12-month NDVI/EVI profile for the given year.

    Scientific recipe
    -----------------
    1. Filter S2 SR Harmonized for the year, bounded by AOI.
    2. Remove scenes with > 60 % cloud.
    3. Apply SCL cloud mask (``mask_sentinel2_sr_scl``).
    4. Compute NDVI = (B8 − B4) / (B8 + B4) and EVI per scene.
    5. For each month 1–12, take monthly **median** composite and
       ``reduceRegion(mean)`` over the AOI → mean NDVI and EVI.
    6. If < 3 months have data → ``insufficient_data``.
    7. If 3–5 months → ``partial``.
    8. Detect season milestones from the monthly NDVI array.
    """
    if not initialize_earth_engine():
        return _unavailable_phenology(year, "Earth Engine not initialized")

    ee = _ee()
    if ee is None:
        return _unavailable_phenology(year, "Earth Engine library unavailable")

    try:
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"

        base_col = (
            ee.ImageCollection(_S2_COLLECTION)
            .filterBounds(geom_ee)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", _MAX_CLOUD_PCT))
            .map(mask_sentinel2_sr_scl)
        )

        total_scenes = int(safe_getinfo(base_col.size(), 0, label=f"pheno_{year}_count") or 0)
        if total_scenes == 0:
            return _unavailable_phenology(year, f"No cloud-filtered Sentinel-2 scenes for {year}")

        nir = SENTINEL2_SR["nir"]    # B8
        red = SENTINEL2_SR["red"]    # B4
        blue = SENTINEL2_SR["blue"]  # B2

        monthly_ndvi: List[Optional[float]] = [None] * 12
        monthly_evi: List[Optional[float]] = [None] * 12
        valid_months = 0

        for month_idx in range(12):
            m = month_idx + 1
            m_start = f"{year}-{m:02d}-01"
            if m == 12:
                m_end = f"{year + 1}-01-01"
            else:
                m_end = f"{year}-{m + 1:02d}-01"

            m_col = base_col.filterDate(m_start, m_end)
            m_count = int(safe_getinfo(m_col.size(), 0, label=f"pheno_{year}_m{m}_count") or 0)
            if m_count == 0:
                continue

            composite = m_col.median().clip(geom_ee)

            # NDVI
            ndvi_img = composite.normalizedDifference([nir, red]).rename("NDVI")
            ndvi_vals = safe_reduce_mean(ndvi_img, geom_ee, scale=20, label=f"pheno_{year}_m{m}_ndvi")
            ndvi_val = ndvi_vals.get("NDVI")
            if ndvi_val is not None:
                monthly_ndvi[month_idx] = round(float(ndvi_val), 4)
                valid_months += 1

            # EVI = 2.5 * (NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1)
            try:
                nir_band = composite.select(nir)
                red_band = composite.select(red)
                blue_band = composite.select(blue)
                evi_img = (
                    nir_band.subtract(red_band)
                    .multiply(2.5)
                    .divide(
                        nir_band.add(red_band.multiply(6))
                        .subtract(blue_band.multiply(7.5))
                        .add(1)
                    )
                    .rename("EVI")
                )
                evi_vals = safe_reduce_mean(evi_img, geom_ee, scale=20, label=f"pheno_{year}_m{m}_evi")
                evi_val = evi_vals.get("EVI")
                if evi_val is not None:
                    monthly_evi[month_idx] = round(float(evi_val), 4)
            except Exception:
                pass  # EVI is best-effort; NDVI is primary

        # Determine status
        if valid_months < 3:
            status = "insufficient_data"
            notes = [f"Only {valid_months} month(s) with valid data. Minimum 3 required."]
        elif valid_months < 6:
            status = "partial"
            notes = [f"{valid_months} months with data. Full profile requires ≥ 6."]
        else:
            status = "ok"
            notes = []

        notes.append(f"Total Sentinel-2 scenes used: {total_scenes}. Valid months: {valid_months}.")

        # Phenology milestones
        season_start = None
        peak_month = None
        season_end = None
        peak_ndvi = None
        amplitude = None

        valid_ndvi_values = [(i, v) for i, v in enumerate(monthly_ndvi) if v is not None]
        if valid_ndvi_values:
            # Peak
            peak_idx, peak_val = max(valid_ndvi_values, key=lambda x: x[1])
            peak_month = peak_idx + 1
            peak_ndvi = peak_val

            # Season start: first month where NDVI > threshold
            for idx, val in valid_ndvi_values:
                if val > _NDVI_SOS_THRESHOLD:
                    season_start = idx + 1
                    break

            # Season end: last month where NDVI > threshold
            for idx, val in reversed(valid_ndvi_values):
                if val > _NDVI_SOS_THRESHOLD:
                    season_end = idx + 1
                    break

            # Amplitude
            min_val = min(v for _, v in valid_ndvi_values)
            amplitude = round(peak_val - min_val, 4)

        return PhenologyRecord(
            year=year,
            monthly_ndvi=monthly_ndvi,
            monthly_evi=monthly_evi,
            season_start_month=season_start,
            peak_month=peak_month,
            season_end_month=season_end,
            peak_ndvi=peak_ndvi,
            amplitude=amplitude,
            valid_month_count=valid_months,
            scene_count=total_scenes,
            status=status,
            method=f"cloud-masked S2 monthly median composites, NDVI/EVI ({_PROCESSING_VERSION})",
            notes=notes,
        )

    except Exception as exc:
        logger.warning("Phenology failed for %d: %s", year, exc, exc_info=True)
        return _unavailable_phenology(year, f"Processing error: {exc}")


def _unavailable_phenology(year: int, reason: str) -> PhenologyRecord:
    return PhenologyRecord(
        year=year,
        monthly_ndvi=[None] * 12,
        monthly_evi=[None] * 12,
        valid_month_count=0,
        scene_count=0,
        status="unavailable",
        method=_PROCESSING_VERSION,
        notes=[reason],
    )
