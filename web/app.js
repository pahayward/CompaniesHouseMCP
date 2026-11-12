"use strict";

// Basemap. Any MapLibre style URL works here (MapTiler, a self-hosted PMTiles style, ...).
const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";
const GLYPH_FONT = ["Noto Sans Bold"];          // must exist in the style's glyph set
const DEFAULT_VIEW = { center: [-2.5, 54.3], zoom: 5.4 };
const PAGE = 50;
const LEVEL_NAMES = ["Postcode area", "Postcode district", "Postcode sector", "Postcode"];
const FACETS = [
  { key: "size", param: "size", short: "s", title: "Company size" },
  { key: "type", param: "type", short: "t", title: "Legal type" },
  { key: "sic", param: "sic", short: "i", title: "Industry (first SIC code)", collapsed: true },
];
const EMPTY = { type: "FeatureCollection", features: [] };
const BLANK_STYLE = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#e8edf3" } }] };

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n).toLocaleString("en-GB");
const compact = (n) => n < 1000 ? String(n) : n < 10000 ? (n / 1000).toFixed(1) + "k"
  : n < 1e6 ? Math.round(n / 1000) + "k" : (n / 1e6).toFixed(1) + "M";
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

const state = {
  colourBy: "size",
  sel: { size: new Set(), type: new Set(), sic: new Set() },
  active: false,
  hideCrowded: false,
};
let legend = null;      // { size: [...], type: [...], sic: [...] }
let levelStarts = [7, 10, 13.5];
let map = null;
let pointsAbort = null;
let place = null;       // { code, offset, total, loading } for the open postcode
let popup = null;
let styleReady = false;
let basemapDown = false;
let handlersBound = false;

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return r.json();
}

// ------------------------------------------------------------------ URL hash state
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const out = { sel: {} };
  const ll = (p.get("ll") || "").split(",").map(Number);
  if (ll.length === 2 && ll.every(Number.isFinite)) out.center = [ll[1], ll[0]];
  if (p.has("z") && Number.isFinite(Number(p.get("z")))) out.zoom = Number(p.get("z"));
  if (["size", "type", "sic"].includes(p.get("by"))) out.by = p.get("by");
  for (const f of FACETS) {
    if (p.has(f.short)) {
      const v = p.get(f.short);
      out.sel[f.key] = v === "none" ? [] : v.split(",").map(Number).filter(Number.isInteger);
    }
  }
  out.active = p.get("a") === "1";
  out.hide = p.get("h") === "1";
  return out;
}

function writeHash() {
  if (!map) return;
  const c = map.getCenter();
  const p = new URLSearchParams({ ll: `${c.lat.toFixed(4)},${c.lng.toFixed(4)}`, z: map.getZoom().toFixed(2), by: state.colourBy });
  for (const f of FACETS) {
    const s = state.sel[f.key];
    if (s.size < legend[f.key].length) p.set(f.short, s.size ? [...s].sort((a, b) => a - b).join(",") : "none");
  }
  if (state.active) p.set("a", "1");
  if (state.hideCrowded) p.set("h", "1");
  history.replaceState(null, "", "#" + p.toString());
}

// ---------------------------------------------------------------------- filters UI
function buildFilters() {
  const host = $("#facet-groups");
  host.innerHTML = "";
  for (const f of FACETS) {
    const items = legend[f.key];
    const wrap = document.createElement(f.collapsed ? "details" : "div");
    wrap.className = "group";
    wrap.dataset.facet = f.key;
    if (f.collapsed && state.sel[f.key].size < items.length) wrap.open = true;
    const head = `<div class="head"><h2>${esc(f.title)}</h2>
        <span class="links"><button type="button" data-all="1">All</button><button type="button" data-all="0">None</button></span></div>`;
    wrap.innerHTML = (f.collapsed ? `<summary>${head}</summary>` : head) + items.map((it) => `
      <label class="opt" title="${esc(it.hint)}">
        <input type="checkbox" value="${it.id}" ${state.sel[f.key].has(it.id) ? "checked" : ""}>
        <span class="sw" style="--c:${it.colour}"></span><span class="lbl">${esc(it.label)}</span>
      </label>`).join("");
    wrap.addEventListener("change", (e) => {
      if (e.target.type !== "checkbox") return;
      const id = Number(e.target.value);
      e.target.checked ? state.sel[f.key].add(id) : state.sel[f.key].delete(id);
      filtersChanged();
    });
    wrap.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-all]");
      if (!b) return;
      e.preventDefault();
      state.sel[f.key] = new Set(b.dataset.all === "1" ? items.map((i) => i.id) : []);
      wrap.querySelectorAll("input[type=checkbox]").forEach((cb) => { cb.checked = state.sel[f.key].has(Number(cb.value)); });
      filtersChanged();
    });
    host.append(wrap);
  }
  markColourBy();
}

function markColourBy() {
  document.querySelectorAll(".group[data-facet]").forEach((g) => g.classList.toggle("by", g.dataset.facet === state.colourBy));
  document.querySelector(`input[name=by][value=${state.colourBy}]`).checked = true;
}

function facetParams(p, { includeStatus = true } = {}) {
  for (const f of FACETS) {
    const s = state.sel[f.key];
    if (s.size < legend[f.key].length) p.set(f.param, s.size ? [...s].sort((a, b) => a - b).join(",") : "none");
  }
  if (includeStatus && state.active) p.set("active", "true");
  return p;
}

function filtersChanged() {
  writeHash();
  refresh();
  if (place) loadPlace(place.code, 0);   // re-list the open postcode under the new layers
}

// -------------------------------------------------------------------------- map
function createMap(hash) {
  map = new maplibregl.Map({
    container: "map",
    style: STYLE_URL,
    center: hash.center || DEFAULT_VIEW.center,
    zoom: hash.zoom ?? DEFAULT_VIEW.zoom,
    minZoom: 4, maxZoom: 19,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12 });

  // "style.load" rather than "load": add our layers as soon as the style is parsed, not after every
  // basemap tile has downloaded, so pins appear even while the basemap is slow.
  map.on("style.load", () => {
    styleReady = true;
    if (map.getSource("places")) return;
    map.addSource("places", { type: "geojson", data: EMPTY });
    map.addSource("hl", { type: "geojson", data: EMPTY });
    map.addLayer({
      id: "places", type: "circle", source: "places",
      layout: { "circle-sort-key": ["-", ["get", "n"]] },   // small bubbles on top
      paint: {
        "circle-color": ["get", "colour"],
        "circle-opacity": 0.82,
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 1.5,
        // radius grows with ln(count): 1 -> 5px, 100 -> 13px, 10k -> 27px, 1M -> 46px
        "circle-radius": ["interpolate", ["linear"], ["ln", ["+", ["get", "n"], 1]],
          0.69, 5, 2.4, 8, 4.6, 13, 6.9, 19, 9.2, 27, 11.5, 36, 14, 46],
      },
    });
    if (map.getStyle().glyphs) {   // count labels need the style's font server
      map.addLayer({
        id: "places-n", type: "symbol", source: "places",
        filter: ["any", ["<", ["get", "level"], 3], [">=", ["get", "n"], 2]],
        layout: { "text-field": ["get", "label"], "text-font": GLYPH_FONT, "text-size": 11,
                  "text-allow-overlap": true, "text-ignore-placement": true },
        paint: { "text-color": "#ffffff", "text-halo-color": "rgba(15,23,42,.55)", "text-halo-width": 1 },
      });
    }
    map.addLayer({
      id: "hl", type: "circle", source: "hl",
      paint: { "circle-radius": 18, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#0f172a", "circle-stroke-width": 3 },
    });

    if (!handlersBound) {   // layer handlers survive a style swap, so bind them once
      handlersBound = true;
      map.on("mousemove", "places", onHover);
      map.on("mouseleave", "places", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
      map.on("click", "places", onPlaceClick);
    }
    refresh();
  });
  map.on("moveend", debounce(() => { writeHash(); refresh(); }, 150));
  map.on("error", (e) => console.warn("map error:", e.error?.message || e));

  // If the basemap style never arrives (offline, blocked), fall back to a plain background so the data still works.
  setTimeout(() => {
    if (styleReady) return;
    console.warn("basemap style did not load; showing a plain background");
    basemapDown = true;
    map.setStyle(BLANK_STYLE);
  }, 15000);
}

function onHover(e) {
  const f = e.features?.[0];
  if (!f) return;
  map.getCanvas().style.cursor = "pointer";
  const p = f.properties;
  popup.setLngLat(f.geometry.coordinates)
    .setHTML(`<b>${esc(p.code)}</b><br>${num(p.n)} ${p.n === 1 ? "company" : "companies"}<br>
              <span class="sub">${LEVEL_NAMES[p.level]}${p.level < 3 ? " · click to zoom in" : ""}</span>`)
    .addTo(map);
}

function onPlaceClick(e) {
  const f = e.features?.[0];
  if (!f) return;
  const p = f.properties, c = f.geometry.coordinates;
  popup.remove();
  if (p.level < 3) {
    map.easeTo({ center: c, zoom: levelStarts[p.level] + 0.3, duration: 600 });
  } else {
    setHighlight(c);
    map.easeTo({ center: c, padding: panelPadding(), duration: 500 });
    openPlace(p.code, c);
  }
}

// Keep the map centre in the part of the map the details panel does not cover.
const NO_PADDING = { top: 0, right: 0, bottom: 0, left: 0 };
function panelPadding() {
  const el = $("#map");
  return matchMedia("(max-width: 800px)").matches
    ? { ...NO_PADDING, bottom: Math.round(el.clientHeight * 0.58) }
    : { ...NO_PADDING, right: Math.min(390, Math.max(0, el.clientWidth - 40)) };
}

function setHighlight(lngLat) {
  map?.getSource("hl")?.setData(lngLat
    ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: lngLat }, properties: {} }] }
    : EMPTY);
}

function setStatus(text, warn = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("warn", warn);
}

async function refresh() {
  if (!map || !legend || !map.getSource("places")) return;
  if (FACETS.some((f) => state.sel[f.key].size === 0)) {
    map.getSource("places").setData(EMPTY);
    setStatus("Nothing selected: switch a layer on to see companies");
    return;
  }
  const b = map.getBounds();
  const p = facetParams(new URLSearchParams({
    bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(4)).join(","),
    zoom: map.getZoom().toFixed(2),
    colour: state.colourBy,
  }));
  if (state.hideCrowded) p.set("hide_crowded", "true");

  pointsAbort?.abort();
  pointsAbort = new AbortController();
  setStatus("Loading…");
  try {
    const gj = await getJSON("/api/map/points?" + p, { signal: pointsAbort.signal });
    const colours = legend[state.colourBy].map((x) => x.colour);
    for (const f of gj.features) {
      f.properties.colour = colours[f.properties.dom] || "#64748b";
      f.properties.label = compact(f.properties.n);
    }
    map.getSource("places").setData(gj);
    if (gj.truncated) {
      setStatus(`Zoom in: showing only the busiest ${num(gj.features.length)} places in view`, true);
    } else {
      setStatus(`${num(gj.features.length)} ${LEVEL_NAMES[gj.level].toLowerCase()}s · ${num(gj.total)} companies in view`
        + (basemapDown ? " · basemap unavailable" : ""), basemapDown);
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    setStatus("Could not load companies: " + err.message, true);
  }
}

// ------------------------------------------------------------------- detail panel
function showPanel(view) {
  $("#detail").hidden = false;
  $("#place-view").hidden = view !== "place";
  $("#company-view").hidden = view !== "company";
}

function closePanel() {
  $("#detail").hidden = true;
  setHighlight(null);
  map.easeTo({ padding: NO_PADDING, duration: 300 });
  place = null;
}

async function openPlace(code, lngLat) {
  place = { code, lngLat, offset: 0, total: 0 };
  showPanel("place");
  $("#place-view").innerHTML = `<h3>${esc(code)}</h3><p class="sub">Loading…</p>`;
  await loadPlace(code, 0);
}

async function loadPlace(code, offset) {
  const p = facetParams(new URLSearchParams({ limit: PAGE, offset }));
  let r;
  try {
    r = await getJSON(`/api/map/place/${encodeURIComponent(code)}/companies?` + p);
  } catch (err) {
    $("#place-view").innerHTML = `<h3>${esc(code)}</h3><p class="sub">${esc(err.message)}</p>`;
    return;
  }
  if (!place || place.code !== code) return;   // user moved on
  place.offset = offset + r.companies.length;
  place.total = r.total;
  const view = $("#place-view");
  if (offset === 0) {
    const filtered = r.place_total != null && r.total !== r.place_total;
    view.innerHTML = `<h3>${esc(r.postcode)}</h3>
      <p class="sub">${num(r.total)} ${r.total === 1 ? "company matches" : "companies match"} your layers${
        filtered ? ` (${num(r.place_total)} registered here)` : ""}</p><div id="place-list"></div><div id="place-more"></div>`;
  }
  const colours = legend.size.map((x) => x.colour);
  $("#place-list").insertAdjacentHTML("beforeend", r.companies.map((c) => `
    <button type="button" class="co" data-number="${esc(c.number)}">
      <div class="name">${esc(c.name)}</div>
      <div class="sub"><span class="chip" style="--c:${colours[c.size_id]}">${esc(c.size)}</span>
        ${esc(c.status)} · ${esc(c.number)}${c.industry && c.industry !== "None Supplied" ? "<br>" + esc(c.industry) : ""}</div>
    </button>`).join(""));
  $("#place-more").innerHTML = place.offset < r.total
    ? `<button type="button" class="icon-btn more">Show more (${num(r.total - place.offset)} left)</button>` : "";
}

async function openCompany(number, { backTo = null, fly = false } = {}) {
  showPanel("company");
  const view = $("#company-view");
  view.innerHTML = `<p class="sub">Loading…</p>`;
  let c;
  try {
    c = await getJSON(`/api/company/${encodeURIComponent(number)}`);
  } catch (err) {
    view.innerHTML = `<p class="sub">${esc(err.message)}</p>`;
    return;
  }
  const loc = c.location ? [c.location[1], c.location[0]] : null;
  if (loc) setHighlight(loc);
  if (loc && fly) map.flyTo({ center: loc, zoom: Math.max(map.getZoom(), 16), padding: panelPadding(), duration: 900 });

  const status = c.status || "";
  const cls = status.startsWith("Active") ? "ok" : /dissol|liquid|strike|receiv|administr/i.test(status) ? "bad" : "";
  const addr = [c.address1, c.address2, c.post_town, c.county, c.postcode].filter(Boolean).map(esc).join("<br>");
  const row = (k, v) => v ? `<dt>${k}</dt><dd>${v}</dd>` : "";
  view.innerHTML = `
    ${backTo ? `<button type="button" class="back" data-back="${esc(backTo)}">&larr; ${esc(backTo)}</button>` : ""}
    <h3>${esc(c.name)}</h3>
    <div class="sub">Company ${esc(c.number)} · <span class="badge ${cls}">${esc(status)}</span></div>
    <dl>
      ${row("Type", esc(c.category))}
      ${row("Size", `${esc(c.size)}<div class="sub">Accounts: ${esc(c.accounts_category || "none")}</div>`)}
      ${row("Incorporated", esc(c.incorporated))}
      ${row("Dissolved", esc(c.dissolved))}
      ${row("Origin", c.country_of_origin && c.country_of_origin !== "United Kingdom" ? esc(c.country_of_origin) : "")}
      ${row("Industry", c.sic?.length ? `<ul>${c.sic.map((s) => `<li>${esc(s.code)}${s.description ? " &ndash; " + esc(s.description) : ""}</li>`).join("")}</ul>` : "")}
      ${row("Registered office", addr)}
      ${row("Previous names", c.previous_names?.length ? `<ul>${c.previous_names.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "")}
    </dl>
    <div class="actions">
      <a href="${esc(c.ch_url)}" target="_blank" rel="noopener noreferrer">Companies House &nearr;</a>
      ${loc ? `<button type="button" class="secondary" data-fly="${loc[0]},${loc[1]}">Zoom to location</button>` : ""}
    </div>`;
}

// ---------------------------------------------------------------------------- search
function initSearch() {
  const input = $("#search"), list = $("#search-results");
  let abort = null, results = [], active = -1;

  const close = () => { list.hidden = true; active = -1; };
  const choose = (i) => {
    const r = results[i];
    if (!r) return;
    close();
    input.value = r.name;
    if (r.lat != null) map.flyTo({ center: [r.lng, r.lat], zoom: 16, padding: panelPadding(), duration: 900 });
    openCompany(r.number);
  };
  const paint = () => {
    list.innerHTML = results.length ? results.map((r, i) => `
      <li role="option" data-i="${i}" aria-selected="${i === active}">
        <div>${esc(r.name)}</div>
        <div class="sub">${esc(r.number)} · ${esc(r.status)}${r.postcode ? " · " + esc(r.postcode) : ""}${r.lat == null ? " · not on map" : ""}</div>
      </li>`).join("") : `<li class="empty">No matches</li>`;
    list.hidden = false;
  };

  const run = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) return close();
    abort?.abort();
    abort = new AbortController();
    try {
      results = await getJSON("/api/search?q=" + encodeURIComponent(q), { signal: abort.signal });
      active = results.length ? 0 : -1;
      paint();
    } catch (err) {
      if (err.name !== "AbortError") { results = []; list.innerHTML = `<li class="empty">${esc(err.message)}</li>`; list.hidden = false; }
    }
  }, 250);

  input.addEventListener("input", run);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "Enter") { e.preventDefault(); choose(Math.max(active, 0)); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!results.length) return;
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      paint();
    }
  });
  list.addEventListener("mousedown", (e) => {            // mousedown: fires before the input blurs
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); choose(Number(li.dataset.i)); }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
}

// ----------------------------------------------------------------------------- init
function renderFooter(meta) {
  const mapped = Number(meta.companies_mapped), total = Number(meta.companies_total);
  const lost = Number(meta.companies_no_postcode) + Number(meta.companies_no_coordinates);
  $("#foot").innerHTML = `<details>
    <summary>${num(mapped)} of ${num(total)} companies are on the map · data sources and licences</summary>
    <p>${num(lost)} companies have no usable or recognised postcode, so they cannot be placed.
    Company data: Companies House, Open Government Licence v3.0 (source file ${esc(meta.source_companies || "")}).
    Postcode locations: ONS Postcode Directory (${esc(meta.source_onspd || "")}); contains OS data &copy; Crown copyright
    and database right, Royal Mail data &copy; Royal Mail copyright and database right, source: Office for National
    Statistics, OGL v3.0. Map &copy; OpenStreetMap contributors, OpenFreeMap.</p></details>`;
}

function showFatal(msg) {
  const el = $("#fatal");
  el.hidden = false;
  el.innerHTML = `<div><h3>The map data isn't ready</h3><p>${esc(msg)}</p>
    <p class="sub">Build it once with <code>python build_geo.py path\\to\\ONSPD.zip</code>, then reload.</p></div>`;
}

async function init() {
  const hash = readHash();
  let res;
  try {
    res = await getJSON("/api/map/legend");
  } catch (err) {
    showFatal(err.message);
    return;
  }
  legend = res.legend;
  levelStarts = res.levels;
  renderFooter(res.meta);

  state.colourBy = hash.by || "size";
  state.active = hash.active;
  state.hideCrowded = hash.hide;
  for (const f of FACETS) {
    const all = legend[f.key].map((x) => x.id);
    state.sel[f.key] = new Set(hash.sel[f.key] ? hash.sel[f.key].filter((i) => all.includes(i)) : all);
  }
  buildFilters();
  $("#f-active").checked = state.active;
  $("#f-crowded").checked = state.hideCrowded;

  document.querySelectorAll("input[name=by]").forEach((r) => r.addEventListener("change", () => {
    state.colourBy = r.value;
    markColourBy();
    filtersChanged();
  }));
  $("#f-active").addEventListener("change", (e) => { state.active = e.target.checked; filtersChanged(); });
  $("#f-crowded").addEventListener("change", (e) => { state.hideCrowded = e.target.checked; filtersChanged(); });

  $("#detail-close").addEventListener("click", closePanel);
  $("#place-view").addEventListener("click", (e) => {
    const co = e.target.closest(".co");
    if (co) return openCompany(co.dataset.number, { backTo: place?.code });
    if (e.target.closest(".more") && place) loadPlace(place.code, place.offset);
  });
  $("#company-view").addEventListener("click", (e) => {
    const back = e.target.closest("[data-back]");
    if (back) {
      showPanel("place");
      if (place?.lngLat) setHighlight(place.lngLat);
      return;
    }
    const fly = e.target.closest("[data-fly]");
    if (fly) {
      const [lng, lat] = fly.dataset.fly.split(",").map(Number);
      map.flyTo({ center: [lng, lat], zoom: Math.max(map.getZoom(), 16), padding: panelPadding(), duration: 800 });
    }
  });
  $("#layers-toggle").addEventListener("click", () => {
    const open = document.body.classList.toggle("filters-open");
    $("#layers-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#map").addEventListener("pointerdown", () => {
    document.body.classList.remove("filters-open");
    $("#layers-toggle").setAttribute("aria-expanded", "false");
  });

  initSearch();
  createMap(hash);
}

init();
