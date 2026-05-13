# Agriculture / Food Security Yearly AOI Analytics Module

Add a complete agriculture and food-security analytics pipeline to the Africa Water Intelligence Platform. This module computes yearly metrics per AOI for cropland extent, cropland conversion, crop phenology, and food-security risk — with proper chart types, no-data handling, and source metadata.

---

## User Review Required

> [!IMPORTANT]
> **Earth Engine dependency**: Dynamic World and Sentinel-2 phenology extractors require a working Earth Engine service account. If EE is unavailable, these layers will return `status: "unavailable"` with explicit notes — same pattern as existing water layers.

> [!IMPORTANT]
> **FEWS NET**: FEWS NET provides a public GeoJSON API at `https://fdw.fews.net/api/ipcphase/`. This does not require authentication. If the API is down or the AOI has no coverage, the layer returns `status: "unavailable"`.

> [!WARNING]
> **Year range**: Dynamic World is available from **2015–present**. Sentinel-2 phenology composites start from **2017**. Years outside these ranges will be marked as `unavailable`, not forced.

## Open Questions

> [!IMPORTANT]
> **Year range to compute**: The plan defaults to 2018–2025 (last 8 years). Should a different range be used, or should this be user-configurable from the frontend?

> [!IMPORTANT]
> **FEWS NET region mapping**: FEWS NET uses admin-level boundaries. The implementation will query by the AOI centroid's lat/lon. If the AOI spans multiple FEWS NET zones, the nearest zone's classification is used. Is area-weighted multi-zone aggregation needed for v1?

---

## Proposed Changes

### Pydantic Models — Agriculture Schemas

#### [NEW] [agriculture_models.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/models/agriculture_models.py)

New Pydantic v2 models:

- **`YearlyRecord`** — The canonical output for every data type per year:
  ```python
  class YearlyRecord(BaseModel):
      datatype: str          # e.g. "cropland_extent"
      description: str
      source: str
      year: int
      value: Optional[float]
      unit: str
      confidence: float      # 0.0–1.0
      coverage: float        # 0.0–1.0 (fraction of AOI with valid data)
      status: str            # "ok" | "partial" | "unavailable" | "insufficient_data"
      method: str
      timestamp: str
      notes: List[str]
  ```

- **`YearOverYearChange`** — Change from previous year:
  ```python
  class YearOverYearChange(BaseModel):
      datatype: str
      year: int
      previous_year: int
      value_current: Optional[float]
      value_previous: Optional[float]
      absolute_change: Optional[float]
      percent_change: Optional[float]
      status: str
  ```

- **`CroplandTransition`** — Detailed gain/loss for conversion:
  ```python
  class CroplandTransition(BaseModel):
      year: int
      gain_km2: Optional[float]
      loss_km2: Optional[float]
      net_change_km2: Optional[float]
      confidence: float
      status: str
  ```

- **`PhenologyRecord`** — Seasonal phenology per year:
  ```python
  class PhenologyRecord(BaseModel):
      year: int
      monthly_ndvi: List[Optional[float]]  # 12 values, Jan–Dec
      monthly_evi: List[Optional[float]]
      season_start_month: Optional[int]
      peak_month: Optional[int]
      season_end_month: Optional[int]
      peak_ndvi: Optional[float]
      amplitude: Optional[float]
      valid_months: int
      status: str
  ```

- **`FoodSecurityRecord`** — Categorical IPC-like classification:
  ```python
  class FoodSecurityRecord(BaseModel):
      year: int
      phase: Optional[int]         # 1–5 IPC scale, None if unavailable
      phase_label: Optional[str]   # "Minimal", "Stressed", etc.
      confidence: float
      source_region: Optional[str]
      status: str
  ```

- **`AgricultureAnalysisResponse`** — Top-level response:
  ```python
  class AgricultureAnalysisResponse(BaseModel):
      geometry: GeometryInput
      stats: AreaStats
      centroid: GeoPoint
      label: Optional[str]
      year_range: List[int]
      cropland_extent: List[YearlyRecord]
      cropland_conversion: List[CroplandTransition]
      phenology: List[PhenologyRecord]
      food_security: List[FoodSecurityRecord]
      yearly_changes: List[YearOverYearChange]
      summary: Dict[str, Any]
      methodology: List[MethodologyItem]
      sources: List[SourceRef]
      status: str
      notes: List[str]
  ```

---

### Backend Services — Data Extractors

#### [NEW] [dynamic_world_service.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/services/dynamic_world_service.py)

Two functions using GEE's `GOOGLE/DYNAMICWORLD/V1` collection:

1. **`cropland_extent_yearly(geom_ee, year) → YearlyRecord`**
   - Filter DW collection for the given year
   - Select the `crops` probability band
   - Compute annual median, threshold > 0.4
   - Sum crop pixel area using `ee.Image.pixelArea()`
   - Return area in km², confidence from scene count, coverage from valid pixel fraction

2. **`cropland_conversion_yearly(geom_ee, year_current, year_previous) → CroplandTransition`**
   - Compute majority class for both years
   - Create transition matrix: non-crop→crop = gain, crop→non-crop = loss
   - Return gain_km2, loss_km2, net_change_km2

Both functions:
- Use `safe_reduce_sum`, `safe_getinfo` from existing `gee_service.py`
- Return `status: "unavailable"` if EE is not initialized
- Return `status: "insufficient_data"` if scene count is 0

---

#### [NEW] [sentinel2_phenology_service.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/services/sentinel2_phenology_service.py)

**`phenology_yearly(geom_ee, year) → PhenologyRecord`**
- Filter `COPERNICUS/S2_SR_HARMONIZED` for the year
- Cloud-mask using the existing `_mask_s2_sr` function from `gee_service.py`
- Compute NDVI = `(B8 - B4) / (B8 + B4)` and EVI
- Build 12 monthly median composites
- Reduce each month's composite over the AOI to get mean NDVI and EVI
- Detect season_start (first month NDVI > 0.3), peak (max NDVI month), season_end (last month NDVI > 0.3)
- Compute amplitude = peak - min
- If < 3 valid months, return `status: "insufficient_data"`

---

#### [NEW] [fewsnet_service.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/services/fewsnet_service.py)

**`food_security_yearly(lat, lon, year) → FoodSecurityRecord`**
- Call the FEWS NET API: `https://fdw.fews.net/api/ipcphase/?format=json&year={year}&fields=simple`
- Find the region containing or nearest to `(lat, lon)`
- Extract IPC phase (1–5)
- Map phase to label: `{1: "Minimal", 2: "Stressed", 3: "Crisis", 4: "Emergency", 5: "Famine"}`
- Cache by year + rounded lat/lon
- If API fails or no coverage, return `status: "unavailable"` with explicit note

---

#### [NEW] [agriculture_service.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/services/agriculture_service.py)

The orchestrator service:

**`analyze_agriculture_aoi(geometry, label, year_start, year_end) → AgricultureAnalysisResponse`**
1. Validate and normalize geometry (reuse `normalize_aoi_geometry`)
2. Check EE availability
3. For each year in range:
   - Run `cropland_extent_yearly` (via `asyncio.to_thread`)
   - Run `cropland_conversion_yearly` (if previous year exists)
   - Run `phenology_yearly`
   - Run `food_security_yearly`
4. Compute year-over-year changes for cropland extent
5. Build summary card:
   - Latest cropland area
   - Trend direction (increasing/decreasing/stable)
   - Latest food security phase
6. Return `AgricultureAnalysisResponse`
7. Cache per AOI hash + year range

Error handling:
- Each extractor is wrapped in try/except → returns its own `status: "unavailable"` on failure
- The orchestrator never crashes even if all extractors fail

---

### API Route

#### [NEW] [agriculture.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/api/agriculture.py)

```python
@router.post("/agriculture/analyze", response_model=AgricultureAnalysisResponse)
async def analyze_agriculture(request: AOIRequest):
    ...
```

- Accepts the same `AOIRequest` model
- Optional query params: `year_start` (default 2018), `year_end` (default current year)
- Returns `AgricultureAnalysisResponse`

---

### App Registration

#### [MODIFY] [main.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/main.py)

- Import and register the new `agriculture` router
- Add to both root and `api_v1` prefix

---

### Frontend Changes

#### [MODIFY] [index.html](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/static/index.html)

- Add a new `<section id="agriculturePanel" class="card"></section>` container in the sidebar

#### [MODIFY] [main.js](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/static/main.js)

After the existing AOI analysis completes, fire a second request to `/aoi/agriculture/analyze` and render the agriculture panel with:

1. **Summary card** — Latest cropland area, trend, food security phase
2. **Cropland Extent chart** — Line chart (years on X, km² on Y)
3. **Cropland Conversion chart** — Stacked bars (gain/loss per year)
4. **Phenology chart** — Monthly line chart for the most recent year (Jan–Dec, NDVI/EVI)
5. **Food Security timeline** — Categorical color-coded bars per year (IPC phase colors)

Each chart section includes:
- Data type label
- Description
- Source attribution
- Confidence badge
- Coverage badge
- Status pill
- "Unavailable" placeholder when `status !== "ok"`

Chart types mapped correctly:
| Data Type | Chart |
|---|---|
| Cropland extent | Line chart |
| Cropland conversion | Gain/Loss bar chart |
| Phenology | Monthly seasonal line |
| Food security | Categorical risk blocks |

#### [MODIFY] [style.css](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/static/style.css)

- Add styles for the agriculture panel cards
- Add IPC phase color classes: `.ipc-1` through `.ipc-5`
- Add confidence/coverage badge styles
- Add phenology chart markers styling

---

### Cache Update

#### [MODIFY] [cache_service.py](file:///c:/Users/chand/OneDrive/Desktop/SkyQuest/my%20vault/africa-water-platform-aoi-fixed-v2/africa-water-platform-aoi/app/services/cache_service.py)

- Add `_AGRICULTURE_CACHE = TTLCache(maxsize=128, ttl=3600)`
- Add `"agriculture"` to `_cache_by_name`

---

## Verification Plan

### Automated Tests
1. Start the server: `uvicorn app.main:app --reload`
2. Open browser and draw an AOI polygon over a known agricultural region in Africa (e.g., Nigeria, Togo, Ghana)
3. Verify the agriculture panel loads with yearly data
4. Verify the API returns valid JSON: `curl -X POST "http://127.0.0.1:8000/aoi/agriculture/analyze" -H "Content-Type: application/json" -d '{"geometry":{"type":"Polygon","coordinates":[[[1.2,6.0],[1.8,6.0],[1.8,6.5],[1.2,6.5],[1.2,6.0]]]}}'`
5. Verify each yearly record has all required fields (datatype, description, source, year, value, unit, confidence, coverage, status, method, timestamp)
6. Verify `status: "unavailable"` is returned when EE is not configured (not a crash)
7. Verify FEWS NET returns categorical IPC phase, not a fake numeric score
8. Verify charts render correctly in the browser (line for extent, bars for conversion, seasonal line for phenology, color blocks for food security)

### Manual Verification
- Confirm that missing years show "unavailable" rather than zeros
- Confirm that the phenology chart only plots cloud-masked composites
- Confirm that food security is displayed as categorical, not continuous
