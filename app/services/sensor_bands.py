"""Sensor-specific band names and spectral index helpers.

This module is the single source of truth for Earth Engine band names used by
the analysis services. Keep sensor families separate; Sentinel-2 names are not
valid for Landsat or Sentinel-3 OLCI products.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple


SENTINEL2_SR: Dict[str, str] = {
    "coastal": "B1",
    "blue": "B2",
    "green": "B3",
    "red": "B4",
    "red_edge_1": "B5",
    "red_edge_2": "B6",
    "red_edge_3": "B7",
    "nir": "B8",
    "nir_narrow": "B8A",
    "water_vapor": "B9",
    "swir1": "B11",
    "swir2": "B12",
    "scl": "SCL",
}

SENTINEL1_GRD: Dict[str, str] = {
    "vv": "VV",
    "vh": "VH",
}

SENTINEL3_OLCI: Dict[str, str] = {
    "red": "Oa08_radiance",
    "red_edge": "Oa11_radiance",
}

LANDSAT_OLI: Dict[str, str] = {
    "green": "SR_B3",
    "swir1": "SR_B6",
}

LANDSAT_TM_ETM: Dict[str, str] = {
    "green": "SR_B2",
    "swir1": "SR_B5",
}

CHIRPS: Dict[str, str] = {
    "precipitation": "precipitation",
}

TERRACLIMATE: Dict[str, str] = {
    "precipitation": "pr",
    "pdsi": "pdsi",
    "deficit": "def",
    "aet": "aet",
    "soil": "soil",
}

JRC_GSW: Tuple[str, ...] = (
    "occurrence",
    "change_abs",
    "change_norm",
    "seasonality",
    "recurrence",
    "transition",
    "max_extent",
)


def required_bands(sensor: str, *keys: str) -> Iterable[str]:
    """Return the Earth Engine band names for a sensor and logical keys."""

    mappings = {
        "sentinel2_sr": SENTINEL2_SR,
        "sentinel1_grd": SENTINEL1_GRD,
        "sentinel3_olci": SENTINEL3_OLCI,
        "landsat_oli": LANDSAT_OLI,
        "landsat_tm_etm": LANDSAT_TM_ETM,
        "chirps": CHIRPS,
        "terraclimate": TERRACLIMATE,
    }
    if sensor not in mappings:
        raise KeyError(f"Unsupported sensor mapping: {sensor}")
    mapping = mappings[sensor]
    return [mapping[key] for key in keys]


def mask_sentinel2_sr_scl(img):
    """Mask clouds, cloud shadow, cirrus, and snow using Sentinel-2 SR SCL."""

    scl = img.select(SENTINEL2_SR["scl"])
    valid = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return img.updateMask(valid)


def s2_ndwi(img):
    return img.normalizedDifference(
        [SENTINEL2_SR["green"], SENTINEL2_SR["nir"]]
    ).rename("NDWI")


def s2_mndwi(img):
    return img.normalizedDifference(
        [SENTINEL2_SR["green"], SENTINEL2_SR["swir1"]]
    ).rename("MNDWI")


def s2_ndti(img):
    return img.normalizedDifference(
        [SENTINEL2_SR["red"], SENTINEL2_SR["green"]]
    ).rename("NDTI")


def s2_ndsi(img):
    return img.normalizedDifference(
        [SENTINEL2_SR["green"], SENTINEL2_SR["swir1"]]
    ).rename("NDSI")


def s3_olci_ndci(img):
    return img.normalizedDifference(
        [SENTINEL3_OLCI["red_edge"], SENTINEL3_OLCI["red"]]
    ).rename("NDCI")


def landsat_oli_ndsi(img):
    return img.normalizedDifference(
        [LANDSAT_OLI["green"], LANDSAT_OLI["swir1"]]
    ).rename("NDSI")


def landsat_tm_etm_ndsi(img):
    return img.normalizedDifference(
        [LANDSAT_TM_ETM["green"], LANDSAT_TM_ETM["swir1"]]
    ).rename("NDSI")
