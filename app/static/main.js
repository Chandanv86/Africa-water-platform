const API_BASE = window.API_BASE || "";

const MODULES = {
  soil_moisture: {
    title: "Soil Moisture",
    description: "Root-zone moisture and drought stress",
    source: "SMAP L4 or Sentinel-1 derived",
    color: "#67e8a6",
  },
  drought: {
    title: "Drought Indices",
    description: "SPI / SPEI / PDSI",
    source: "CHIRPS + TerraClimate",
    color: "#ffd166",
  },
  glacier: {
    title: "Glacial Retreat",
    description: "Glacier/snow area loss",
    source: "Sentinel-2 + Landsat",
    color: "#9bdcff",
  },
  flood: {
    title: "Flood Extent Mapping",
    description: "SAR flood detection",
    source: "Sentinel-1",
    color: "#5fb3ff",
  },
  turbidity: {
    title: "Sediment Plumes / Turbidity",
    description: "Suspended sediment and water clarity",
    source: "Sentinel-2",
    color: "#f5a742",
  },
  chlorophyll: {
    title: "Algal Blooms / Chlorophyll-a",
    description: "Harmful bloom detection",
    source: "Sentinel-3 OLCI",
    color: "#a3e635",
  },
  water_quality: {
    title: "Water Quality Proxy",
    description: "Combined turbidity and chlorophyll pressure",
    source: "Sentinel-2 + Sentinel-3 OLCI",
    color: "#c084fc",
  },
};

const MODULE_TREND_CAPTIONS = {
  soil_moisture: "Yearly moisture proxy: 0 dry/stressed, 1 wetter. Missing bars mean observations were unavailable.",
  drought: "Yearly drought stress proxy from CHIRPS and TerraClimate: 0 low stress, 1 high stress.",
  glacier: "Yearly snow/ice fraction from optical NDSI. Meaningful only in glaciated or persistent snow regions.",
  flood: "Yearly Sentinel-1 SAR wet/inundation signal: 0 low signal, 1 high signal.",
  turbidity: "Yearly Sentinel-2 turbidity proxy from red/green water pixels: 0 low, 1 high.",
  chlorophyll: "Yearly Sentinel-3 OLCI chlorophyll proxy from NDCI: 0 low, 1 high bloom pressure.",
  water_quality: "Yearly combined water-quality proxy: 0 degraded proxy signal, 1 better proxy condition.",
};

const map = L.map("map", {
  zoomControl: true,
  worldCopyJump: false,
  maxBounds: [[-40, -25], [40, 60]],
  maxBoundsViscosity: 1.0,
}).setView([8, 20], 3);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 19,
}).addTo(map);

const drawnItems = new L.FeatureGroup();
const intelligenceLayers = L.layerGroup().addTo(map);
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
  draw: {
    polyline: false,
    circle: false,
    circlemarker: false,
    marker: false,
    rectangle: true,
    polygon: true,
  },
  edit: {
    featureGroup: drawnItems,
    remove: true,
  },
});
map.addControl(drawControl);

let clickMarker = null;
let chart = null;
let moduleCharts = [];
let currentAoiGeoJson = null;
let mode = "point";
let lastPoint = null;
let lastAnalysis = null;

const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const areaStatsEl = document.getElementById("areaStats");
const timelineEl = document.getElementById("timeline");
const flagsEl = document.getElementById("flags");
const sourcesEl = document.getElementById("sources");
const methodologyEl = document.getElementById("methodology");
const moduleGridEl = document.getElementById("moduleGrid");
const layerTogglesEl = document.getElementById("layerToggles");

const pointModeBtn = document.getElementById("pointModeBtn");
const aoiModeBtn = document.getElementById("aoiModeBtn");
const clearBtn = document.getElementById("clearBtn");
const downloadButtons = document.querySelectorAll(".btn.download");

function api(path) {
  return `${API_BASE}${path}`;
}

function setMode(newMode) {
  mode = newMode;
  pointModeBtn.classList.toggle("active", mode === "point");
  aoiModeBtn.classList.toggle("active", mode === "aoi");
  statusEl.innerHTML = mode === "point"
    ? "Point mode enabled. Click anywhere on the map."
    : "AOI mode enabled. Draw a rectangle or polygon.";
}

function setLoading(message) {
  statusEl.innerHTML = message;
}

function formatValue(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
  if (typeof value === "number") return Number(value).toFixed(Math.abs(value) < 10 ? 3 : 2);
  return value;
}

function kpi(title, value) {
  return `<div class="tile"><div class="label">${title}</div><div class="value">${formatValue(value)}</div></div>`;
}

function moduleMetrics(key, layer) {
  const metrics = layer?.metrics || {};
  const table = {
    soil_moisture: [
      ["Moisture", layer?.score],
      ["Stress", layer?.severity],
      ["Confidence", layer?.confidence],
      ["Source", metrics.collection || "S1/SMAP"],
    ],
    drought: [
      ["SPI proxy", layer?.metrics?.spi_proxy],
      ["SPEI proxy", layer?.metrics?.spei_proxy],
      ["PDSI", layer?.metrics?.pdsi ?? layer?.value],
      ["Confidence", layer?.confidence],
    ],
    glacier: [
      ["Recent snow", layer?.metrics?.recent_scene_count],
      ["Retreat", layer?.severity],
      ["Confidence", layer?.confidence],
      ["Status", layer?.status],
    ],
    flood: [
      ["Severity", layer?.severity],
      ["Extent km2", metrics.flood_extent_km2 ?? layer?.value],
      ["Confidence", layer?.confidence],
      ["Scenes", metrics.post_scene_count],
    ],
    turbidity: [
      ["NDTI", layer?.value],
      ["Proxy", layer?.score],
      ["Confidence", layer?.confidence],
      ["Scenes", metrics.scene_count],
    ],
    chlorophyll: [
      ["NDCI", layer?.value],
      ["Bloom", layer?.severity],
      ["Confidence", layer?.confidence],
      ["Scenes", metrics.scene_count],
    ],
    water_quality: [
      ["Quality", layer?.score],
      ["Degradation", layer?.severity],
      ["Confidence", layer?.confidence],
      ["Status", layer?.status],
    ],
  };
  return table[key] || [["Score", layer?.score], ["Severity", layer?.severity], ["Confidence", layer?.confidence], ["Value", layer?.value]];
}

function layerTone(layer) {
  const severity = Number(layer?.severity ?? layer?.score ?? 0);
  if (!Number.isFinite(severity)) return "neutral";
  if (severity >= 0.7) return "high";
  if (severity >= 0.4) return "moderate";
  return "low";
}

function renderModuleCard(key, layer) {
  const meta = MODULES[key];
  const tone = layerTone(layer);
  const metrics = moduleMetrics(key, layer);
  return `
    <article class="module-card ${tone}">
      <div class="module-head">
        <span class="module-dot" style="background:${meta.color}"></span>
        <h3>${meta.title}</h3>
        <span class="status-pill">${layer?.status || "unknown"}</span>
      </div>
      <div class="module-copy">${meta.description}</div>
      <div class="module-source">${meta.source}</div>
      <div class="kpi compact">
        ${metrics.map(([label, value]) => kpi(label, value)).join("")}
      </div>
      <div class="module-chart-wrap"><canvas id="moduleChart-${key}"></canvas></div>
      <div class="small-note">${MODULE_TREND_CAPTIONS[key] || "Yearly EO trend. Missing values are not plotted."}</div>
      ${layer?.notes?.length ? `<div class="small-note">${layer.notes.slice(0, 2).map(n => `- ${n}`).join("<br/>")}</div>` : ""}
    </article>
  `;
}

function renderModules(data) {
  const keys = ["soil_moisture", "drought", "glacier", "flood", "turbidity", "chlorophyll", "water_quality"];
  moduleGridEl.innerHTML = keys.map(key => renderModuleCard(key, data[key])).join("");
  renderModuleCharts(data, keys);
}

function trendRowsForModule(key, data) {
  const trends = data.trend_summary || {};
  const mapping = {
    soil_moisture: ["soil_moisture_yearly_trends", "soil_moisture_trends", ["moisture_proxy", "soil_moisture_proxy"]],
    drought: ["drought_yearly_trends", "drought_trends", ["drought_stress"]],
    glacier: ["glacier_yearly_trends", "glacier_trends", ["snow_ice_fraction"]],
    flood: ["flood_yearly_trends", "flood_history", ["flood_signal"]],
    turbidity: ["turbidity_yearly_trends", "turbidity_trends", ["turbidity_proxy"]],
    chlorophyll: ["chlorophyll_yearly_trends", "chlorophyll_trends", ["chlorophyll_proxy"]],
    water_quality: ["water_quality_yearly_trends", "water_quality_trends", ["quality_proxy", "water_quality_proxy"]],
  };
  const [yearlyKey, fallbackKey, valueKeys = []] = mapping[key] || [];
  const yearlyRows = (trends[yearlyKey] || []).filter(row => row?.year);
  const fallbackRows = (trends[fallbackKey] || []).filter(row => row?.year || row?.month || row?.timestamp);
  const monthlyRows = (trends.monthly || []).filter(row => row?.month || row?.timestamp);
  const waterHistoryRows = (trends.yearly || data.historical_timeline || []).filter(row => row?.year);
  const rows = yearlyRows.length ? yearlyRows : (fallbackRows.length ? fallbackRows : (monthlyRows.length ? monthlyRows : waterHistoryRows));
  return rows.map(row => ({
    label: row.year || row.month || String(row.timestamp || "").slice(0, 10),
    value: valueKeys.map(valueKey => row[valueKey]).find(value => value !== null && value !== undefined) ?? row.value ?? row.water_class ?? row.severity ?? null,
  }));
}

function renderModuleCharts(data, keys) {
  moduleCharts.forEach(item => item.destroy());
  moduleCharts = [];
  keys.forEach(key => {
    const canvas = document.getElementById(`moduleChart-${key}`);
    if (!canvas) return;
    const rows = trendRowsForModule(key, data);
    const validRows = rows.filter(row => row.value !== null && row.value !== undefined && Number.isFinite(Number(row.value)));
    if (!validRows.length) {
      canvas.parentElement.innerHTML = `<div class="small-note">${MODULES[key].title} trend unavailable for this AOI.</div>`;
      return;
    }
    const maxValue = Math.max(...validRows.map(row => Number(row.value)));
    moduleCharts.push(new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: validRows.map(row => row.label),
        datasets: [{
          label: MODULES[key].title,
          data: validRows.map(row => Number(row.value)),
          backgroundColor: MODULES[key].color,
          borderColor: MODULES[key].color,
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { ticks: { color: "#aeb8b2", maxRotation: 60, minRotation: 0 } },
          y: { min: 0, max: maxValue > 1 ? 3 : 1, ticks: { color: "#aeb8b2" } },
        },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: true },
        },
      },
    }));
  });
}

function renderSummary(data) {
  const near = data.nearest_water_body || {};
  summaryEl.innerHTML = `
    <h3>Summary</h3>
    <div class="kpi">
      ${kpi("Status", data.summary_card)}
      ${kpi("Nearest distance", near.distance_km !== null && near.distance_km !== undefined ? `${near.distance_km} km` : "N/A")}
    </div>
    <p>
      <b>${data.summary_card || "Water intelligence"}</b><br/>
      Nearest feature: ${near.name || "Unknown"} (${near.type || "Unknown"})
    </p>
    <div class="meta">Source dataset: ${data.source_dataset || "N/A"}</div>
    <div class="meta">Data timestamp: ${data.data_timestamp ?? "N/A"}</div>
  `;
}

function renderAreaStats(data) {
  const s = data.stats || null;
  if (!s) {
    areaStatsEl.innerHTML = "";
    return;
  }
  areaStatsEl.innerHTML = `
    <h3>AOI Statistics</h3>
    <div class="kpi">
      ${kpi("Area km2", s.area_km2)}
      ${kpi("Perimeter km", s.perimeter_km)}
      ${kpi("Centroid lat", s.centroid?.lat)}
      ${kpi("Centroid lon", s.centroid?.lon)}
    </div>
    <p class="meta">BBox: ${s.bbox ? s.bbox.map(v => Number(v).toFixed(4)).join(", ") : "N/A"}</p>
  `;
}

function renderFlags(data) {
  const flags = data.flags || [];
  flagsEl.innerHTML = `
    <h3>Alerts</h3>
    <div>${flags.length ? flags.map(f => `<span class="badge">${f}</span>`).join("") : "<span class='meta'>No active alert indicators</span>"}</div>
  `;
}

function renderSources(data) {
  const sources = data.sources || [];
  sourcesEl.innerHTML = `
    <h3>Sources and STAC Context</h3>
    ${sources.map(s => `
      <div class="source-row">
        <div><b>${s.name}</b></div>
        <div class="meta">${s.collection || ""} ${s.timestamp ? "- " + s.timestamp : ""}</div>
        <div class="meta">${s.notes || ""}</div>
      </div>
    `).join("")}
  `;
}

function renderMethodology(data) {
  const methods = data.methodology || [];
  methodologyEl.innerHTML = `
    <h3>Methodology</h3>
    ${methods.map(m => `
      <div class="source-row">
        <div><b>${m.title}</b></div>
        <div class="meta">${m.description}</div>
      </div>
    `).join("")}
  `;
}

function renderTimeline(data) {
  const trends = data.trend_summary || {};
  const monthly = trends.monthly || [];
  const annual = trends.anomaly_yearly_trends || [];
  const timeline = data.historical_timeline || [];
  const labels = annual.length ? annual.map(x => x.year) : (monthly.length ? monthly.map(x => x.month) : timeline.map(x => x.year));
  const valueForYear = (rows, year, keys) => {
    const row = (rows || []).find(item => item.year === year);
    if (!row) return null;
    return keys.map(key => row[key]).find(value => value !== null && value !== undefined) ?? row.value ?? row.severity ?? null;
  };
  const datasets = annual.length
    ? [
        { label: "Flood", data: labels.map(year => valueForYear(trends.flood_yearly_trends, year, ["flood_signal"])), borderColor: MODULES.flood.color },
        { label: "Turbidity", data: labels.map(year => valueForYear(trends.turbidity_yearly_trends, year, ["turbidity_proxy"])), borderColor: MODULES.turbidity.color },
        { label: "Chlorophyll", data: labels.map(year => valueForYear(trends.chlorophyll_yearly_trends, year, ["chlorophyll_proxy"])), borderColor: MODULES.chlorophyll.color },
        { label: "Drought", data: labels.map(year => valueForYear(trends.drought_yearly_trends, year, ["drought_stress"])), borderColor: MODULES.drought.color },
        { label: "Anomaly", data: annual.map(x => x.value ?? null), borderColor: "#ff6b6b" },
      ]
    : monthly.length
    ? [
        { label: "Flood", data: monthly.map(x => x.flood_signal ?? null), borderColor: MODULES.flood.color },
        { label: "Turbidity", data: monthly.map(x => x.turbidity_proxy ?? null), borderColor: MODULES.turbidity.color },
        { label: "Chlorophyll", data: monthly.map(x => x.chlorophyll_proxy ?? null), borderColor: MODULES.chlorophyll.color },
        { label: "Drought", data: monthly.map(x => x.drought_stress ?? null), borderColor: MODULES.drought.color },
        { label: "Anomaly", data: monthly.map(x => x.anomaly ?? null), borderColor: "#ff6b6b" },
      ]
    : [{ label: "Water class / value", data: timeline.map(x => x.water_class ?? x.value ?? null), borderColor: MODULES.flood.color }];

  if (!labels.length) {
    timelineEl.innerHTML = `
      <h3>Timeline</h3>
      <p class="meta">Timeline is unavailable for this request. Check Earth Engine authentication and dataset access.</p>
    `;
    return;
  }

  timelineEl.innerHTML = `
    <h3>Timeline</h3>
    <div class="chart-wrap"><canvas id="timelineChart"></canvas></div>
    <div class="small-note">${annual.length ? "Yearly EO proxy trends for available datasets." : (monthly.length ? "Monthly EO proxy trends for available datasets." : "JRC water class: 0 no data, 1 not water, 2 seasonal, 3 permanent.")}</div>
  `;

  const ctx = document.getElementById("timelineChart").getContext("2d");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: annual.length || monthly.length ? "line" : "bar",
    data: {
      labels,
      datasets: datasets.map(ds => ({
        ...ds,
        backgroundColor: ds.borderColor,
        tension: 0.25,
        spanGaps: false,
        pointRadius: 2,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          min: 0,
          max: annual.length || monthly.length ? 1 : 3,
          title: { display: true, text: annual.length || monthly.length ? "Proxy value (0-1)" : "JRC class" },
        },
      },
      plugins: { legend: { labels: { color: "#dbe8ff" } } },
    },
  });
}

function renderLayerToggles(data) {
  layerTogglesEl.innerHTML = Object.entries(MODULES).map(([key, meta]) => `
    <label class="toggle-row">
      <input type="checkbox" data-layer="${key}" ${["flood", "turbidity", "chlorophyll"].includes(key) ? "checked" : ""}/>
      <span class="module-dot" style="background:${meta.color}"></span>
      <span>${meta.title}</span>
    </label>
  `).join("");
  layerTogglesEl.querySelectorAll("input").forEach(input => {
    input.addEventListener("change", () => renderMapOverlays(data));
  });
  renderMapOverlays(data);
}

function overlayBounds() {
  if (currentAoiGeoJson && ["Polygon", "MultiPolygon"].includes(currentAoiGeoJson.type)) {
    return L.geoJSON(currentAoiGeoJson).getBounds();
  }
  if (lastPoint) {
    const { lat, lon } = lastPoint;
    return L.latLngBounds([[lat - 0.2, lon - 0.2], [lat + 0.2, lon + 0.2]]);
  }
  return null;
}

function renderMapOverlays(data) {
  intelligenceLayers.clearLayers();
  const bounds = overlayBounds();
  if (!bounds) return;

  layerTogglesEl.querySelectorAll("input:checked").forEach(input => {
    const key = input.dataset.layer;
    const meta = MODULES[key];
    const layer = data[key] || {};
    const severity = Math.max(0.12, Number(layer.severity ?? layer.score ?? 0.2));
    const rectangle = L.rectangle(bounds, {
      color: meta.color,
      weight: 1,
      fillColor: meta.color,
      fillOpacity: Math.min(0.42, 0.12 + severity * 0.3),
      interactive: true,
    });
    rectangle.bindPopup(`
      <b>${meta.title}</b><br/>
      ${meta.description}<br/>
      Source: ${meta.source}<br/>
      Status: ${layer.status || "unknown"}<br/>
      Severity: ${formatValue(layer.severity)}
    `);
    intelligenceLayers.addLayer(rectangle);
  });
}

function clearCards() {
  moduleCharts.forEach(item => item.destroy());
  moduleCharts = [];
  [summaryEl, areaStatsEl, moduleGridEl, timelineEl, flagsEl, sourcesEl, methodologyEl, layerTogglesEl]
    .forEach(el => el.innerHTML = "");
  intelligenceLayers.clearLayers();
}

function renderAll(data) {
  lastAnalysis = data;
  renderSummary(data);
  renderAreaStats(data);
  renderModules(data);
  renderTimeline(data);
  renderFlags(data);
  renderSources(data);
  renderMethodology(data);
  renderLayerToggles(data);
}

async function loadPointAnalysis(lat, lon) {
  lastPoint = { lat, lon };
  currentAoiGeoJson = null;
  setLoading(`Loading point analysis for <b>${lat.toFixed(4)}, ${lon.toFixed(4)}</b>...`);
  clearCards();

  const res = await fetch(api(`/water/inspect?lat=${lat}&lon=${lon}&buffer_km=5`));
  const data = await res.json();

  if (!res.ok) {
    statusEl.innerHTML = `<span class="error">${data.detail || "Request failed"}</span>`;
    return;
  }

  statusEl.innerHTML = `Point analysis complete for <b>${lat.toFixed(4)}, ${lon.toFixed(4)}</b>`;
  renderAll(data);
}

async function loadAoiAnalysis(geojson, label) {
  lastPoint = null;
  setLoading(`Running AOI analysis for <b>${label || "selected area"}</b>...`);
  clearCards();
  const res = await fetch(api("/aoi/analyze"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      geometry: geojson,
      label: label || "AOI",
      buffer_km: 5,
    }),
  });
  const data = await res.json();

  if (!res.ok) {
    statusEl.innerHTML = `<span class="error">${data.detail || "AOI request failed"}</span>`;
    return;
  }

  statusEl.innerHTML = `AOI analysis complete for <b>${label || "selected area"}</b>`;
  currentAoiGeoJson = { type: data.geometry.type, coordinates: data.geometry.coordinates };
  renderAll(data);
}

pointModeBtn.addEventListener("click", () => setMode("point"));
aoiModeBtn.addEventListener("click", () => setMode("aoi"));

clearBtn.addEventListener("click", () => {
  drawnItems.clearLayers();
  intelligenceLayers.clearLayers();
  if (clickMarker) {
    map.removeLayer(clickMarker);
    clickMarker = null;
  }
  currentAoiGeoJson = null;
  lastPoint = null;
  lastAnalysis = null;
  clearCards();
  statusEl.innerHTML = "Cleared. Click a point or draw an AOI.";
});

map.on("click", async (e) => {
  if (mode !== "point") return;
  const { lat, lng } = e.latlng;
  if (clickMarker) map.removeLayer(clickMarker);
  clickMarker = L.marker([lat, lng]).addTo(map);
  await loadPointAnalysis(lat, lng);
});

map.on(L.Draw.Event.CREATED, async (e) => {
  if (mode !== "aoi") return;
  drawnItems.clearLayers();
  const layer = e.layer;
  drawnItems.addLayer(layer);

  const geojson = layer.toGeoJSON().geometry;
  currentAoiGeoJson = geojson;
  const label = e.layerType === "rectangle" ? "Rectangle AOI" : "Polygon AOI";
  await loadAoiAnalysis(geojson, label);
});

downloadButtons.forEach(btn => {
  btn.addEventListener("click", async () => {
    const layer = btn.getAttribute("data-layer");
    if (!currentAoiGeoJson && !lastPoint) {
      alert("First click a point or draw an AOI.");
      return;
    }

    try {
      btn.disabled = true;
      btn.textContent = "Preparing...";
      let response;
      if (currentAoiGeoJson && ["Polygon", "MultiPolygon"].includes(currentAoiGeoJson.type)) {
        response = await fetch(api("/aoi/tif"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            geometry: currentAoiGeoJson,
            layer,
            label: "AOI",
            buffer_km: 5,
            scale_m: 30,
          }),
        });
      } else if (lastPoint) {
        const { lat, lon } = lastPoint;
        response = await fetch(api(`/tif/export?lat=${lat}&lon=${lon}&layer=${layer}&buffer_km=5&scale_m=30`));
      }

      if (!response.ok) {
        const body = await response.text();
        alert(body);
        return;
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${layer}.tif`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Download failed: ${err}`);
    } finally {
      btn.disabled = false;
      btn.textContent = layer.replace("_", " ");
    }
  });
});

setMode("point");
