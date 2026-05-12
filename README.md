# Africa Water Intelligence Platform

A production-shaped Python/FastAPI + Leaflet system for Africa-scale EO water intelligence.

It supports:
- Point click analysis
- Polygon / quadrilateral AOI analysis
- Flood extent from Sentinel-1 SAR
- Turbidity and sediment plume proxies from Sentinel-2
- Chlorophyll / algal bloom proxies from Sentinel-3 OLCI
- Water-quality proxies
- Historical water extent from JRC Global Surface Water
- Soil moisture and drought proxies
- Glacier retreat / snow-ice change proxies
- GeoTIFF export

---

## Setting up on another device

Follow these instructions to clone and set up the project on a new machine.

### 1) Prerequisites

Ensure you have the following installed:
- **Python 3.11 or 3.12**: Download from [python.org](https://www.python.org/downloads/)
- **Git**: Download from [git-scm.com](https://git-scm.com/downloads)

### 2) Clone the Repository

Open your terminal or command prompt and run:
```bash
git clone https://github.com/Chandanv86/Africa-water-platform.git
cd Africa-water-platform
```

### 3) Create a Virtual Environment

It is highly recommended to isolate dependencies in a virtual environment.

**Windows PowerShell:**
```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4) Environment Variables

You need an `.env` file at the root of the project to store your configurations.

1. Copy the example file (if available) or create a new `.env` file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in a text editor and configure the following variables:
   ```env
   APP_NAME="Africa Water Intelligence Platform"
   APP_VERSION="3.0.0"
   ENVIRONMENT="development"
   DEBUG=true
   CORS_ORIGINS=*

   # Google Earth Engine service account auth
   GEE_SERVICE_ACCOUNT_EMAIL="your-service-account@your-project-id.iam.gserviceaccount.com"
   GEE_SERVICE_ACCOUNT_KEY_PATH="/absolute/path/to/your/service-account-key.json"
   GEE_PROJECT_ID="your-project-id"

   # STAC / cloud data
   STAC_URL="https://planetarycomputer.microsoft.com/api/stac/v1"
   STAC_DAYS_BACK=30
   STAC_COLLECTION_S1="sentinel-1-grd"
   STAC_COLLECTION_S2="sentinel-2-l2a"
   STAC_COLLECTION_S3="sentinel-3-olci"
   STAC_COLLECTION_LANDSAT="landsat-c2-l2"
   ```

### 5) Earth Engine Authentication Setup

To use Google Earth Engine functionality:
1. Create a Google Cloud project and enable the **Earth Engine API**.
2. Go to IAM & Admin > Service Accounts, and create a new service account.
3. Generate and download a JSON key for this service account.
4. Save the JSON key securely on your machine.
5. Update your `.env` file to point `GEE_SERVICE_ACCOUNT_KEY_PATH` to the absolute path of this JSON file, and fill in the corresponding `GEE_SERVICE_ACCOUNT_EMAIL` and `GEE_PROJECT_ID`.

*(Note: If Earth Engine is not configured, the app will still run, but Earth Engine-backed layers will return `permission_denied` or `unavailable`.)*

### 6) Add Natural Earth Vector Data

To support offline vector fallback functionality, create the following directory structure and add the geojson files:

```bash
mkdir -p data/natural_earth
```

You need to place these two files in the `data/natural_earth/` directory:
- `ne_10m_lakes.geojson`
- `ne_10m_rivers.geojson`

You can download them from [Natural Earth Data](https://www.naturalearthdata.com/) (1:10m Physical Vectors) and convert the shapefiles to `.geojson`, or source them if you have them from a previous backup.

### 7) Run the Application

Start the FastAPI server using Uvicorn:

```bash
uvicorn app.main:app --reload
```

Once the server is running, you can access:
- **Web App**: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- **Swagger API Docs**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Health Check**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

---

## Using the UI

- Navigate around the map interface.
- **Point Analysis**: Click any point on the map to get immediate water intelligence.
- **AOI Analysis**: Use the drawing tool to draw a rectangle or polygon area.
- The right side panel will populate with real-time analytics including flood extents, turbidity, chlorophyll levels, water quality, soil moisture, and historical timelines.
- You can export GeoTIFFs from the AOI panel for further GIS processing.

## Project Structure

```text
app/
├── main.py        # FastAPI entrypoint
├── api/           # API routes
├── services/      # Earth Engine, STAC, and GIS logic
├── models/        # Pydantic response schemas
└── static/        # Leaflet frontend (HTML, CSS, JS)
```

## Recommended Production Upgrade

For a real-world deployment, consider adding:
- **Redis**: For caching costly Earth Engine and STAC queries.
- **PostGIS**: For advanced spatial indexing.
- **Celery/RQ**: Background workers for handling large AOI processing.
- **Cloud Object Storage**: Saving GeoTIFF exports to AWS S3 / GCP Storage.
- **TiTiler/rio-tiler**: A proper dynamic tile server.
- **Logging/Monitoring**: Integrating Datadog, Sentry, or Grafana.
