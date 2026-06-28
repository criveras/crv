#!/usr/bin/env python3
"""App simple: dato real + limites LL/HH step por hora."""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, jsonify, request

from analyze import DEFAULT_CONFIG, load_config, prepare_dataset
from variable_profiles import get_profile

APP2_VERSION = "app2-v2026.06.28-001"
TZ_NAME = "America/Santiago"
TZ = ZoneInfo(TZ_NAME)

BASE_DIR = Path(__file__).resolve().parent
RT3_HOST = os.environ.get("RT3_API_HOST", "http://rt3-d3:8090")
POINTS_URL = os.environ.get("RT3_POINTS_URL", f"{RT3_HOST}/api/points")
TIMEOUT = int(os.environ.get("RT3_API_TIMEOUT", "120"))

app = Flask(__name__)


def base_cfg() -> dict:
    if DEFAULT_CONFIG.is_file():
        return load_config(DEFAULT_CONFIG)
    return {"point": "cp.pcp.huiliches", "unit": "l/s", "fini": "*-14d", "ma": 5, "preset_points": [], "timezone": TZ_NAME}


def cfg_for_point(point: str, fini: str, ma: int) -> dict:
    cfg = base_cfg()
    cfg.update({"point": point, "fini": fini, "ma": ma, "timezone": cfg.get("timezone") or TZ_NAME})
    over = (cfg.get("variable_overrides") or {}).get(point, {})
    prof = get_profile(point, over.get("unit") or "", cfg)
    cfg["unit"] = prof.get("unit") or cfg.get("unit", "")
    cfg["variable_type"] = prof.get("type")
    return cfg


def as_cl(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize(TZ)
    return t.tz_convert(TZ)


def ts_ms(ts: pd.Timestamp) -> int:
    return int(as_cl(ts).timestamp() * 1000)


def pct(vals: list[float], q: float) -> float:
    s = pd.Series(vals, dtype="float64")
    return float(s.quantile(q))


def sigma3_limits(vals: list[float]) -> tuple[float, float] | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if len(clean) < 3:
        return None
    p10 = pct(clean, 0.10)
    p50 = pct(clean, 0.50)
    p90 = pct(clean, 0.90)
    sigma = (p90 - p10) / 2.563
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = max(abs(p50) * 0.01, 0.01)
    return round(p50 - 3 * sigma, 3), round(p50 + 3 * sigma, 3)


def easter_date(year: int) -> datetime:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    h = (19 * a + b - d - f + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day)


def is_chile_holiday(t: pd.Timestamp) -> bool:
    dt = as_cl(t).to_pydatetime()
    fixed = {
        "01-01", "05-01", "05-21", "06-20", "06-29", "07-16", "08-15",
        "09-18", "09-19", "10-12", "10-31", "11-01", "12-08", "12-25",
    }
    if dt.strftime("%m-%d") in fixed:
        return True
    easter = easter_date(dt.year)
    return dt.date() in {(easter - timedelta(days=2)).date(), (easter - timedelta(days=1)).date()}


def day_kind(ts: pd.Timestamp) -> str:
    t = as_cl(ts)
    if is_chile_holiday(t):
        return "holiday"
    if t.weekday() >= 5:
        return "weekend"
    return "weekday"


def build_midnight_lines(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    cur = as_cl(start).floor("D")
    stop = as_cl(end).ceil("D")
    out = []
    while cur <= stop:
        out.append({"value": ts_ms(cur), "color": "#8a8a8a", "width": 1, "dashStyle": "ShortDot", "zIndex": 20})
        cur += pd.Timedelta(days=1)
    return out


def build_hourly_steps(df: pd.DataFrame) -> dict[str, Any]:
    src = df.dropna(subset=["time_local", "value"]).copy()
    if src.empty:
        return {"weekday": [], "weekend": [], "holiday": [], "midnight_lines": []}
    src["time_cl"] = src["time_local"].map(as_cl)
    src["hour_cl"] = src["time_cl"].map(lambda t: int(t.hour))
    src["kind"] = src["time_cl"].map(day_kind)

    limits: dict[tuple[str, int], tuple[float, float]] = {}
    for (kind, hour), group in src.groupby(["kind", "hour_cl"]):
        lim = sigma3_limits(group["value"].astype(float).tolist())
        if lim:
            limits[(str(kind), int(hour))] = lim

    # Fallback: feriado usa patron fin de semana, luego habil, si hay pocas muestras rojas.
    for hour in range(24):
        if ("holiday", hour) not in limits:
            if ("weekend", hour) in limits:
                limits[("holiday", hour)] = limits[("weekend", hour)]
            elif ("weekday", hour) in limits:
                limits[("holiday", hour)] = limits[("weekday", hour)]

    start = src["time_cl"].min().floor("h")
    end = src["time_cl"].max().floor("h") + pd.Timedelta(hours=1)
    rows: dict[str, list[list[float | None]]] = {"weekday": [], "weekend": [], "holiday": []}
    prev_kind: str | None = None
    cur = start
    while cur <= end:
        kind = day_kind(cur)
        lim = limits.get((kind, int(cur.hour)))
        if lim:
            x = ts_ms(cur)
            lo, hi = lim
            if prev_kind and prev_kind != kind and rows[prev_kind]:
                rows[prev_kind].append([x, None, None])
            rows[kind].append([x, lo, hi])
            prev_kind = kind
        cur += pd.Timedelta(hours=1)
    rows["midnight_lines"] = build_midnight_lines(start, end)
    return rows


@app.route("/")
def index():
    cfg = base_cfg()
    point = request.args.get("point") or cfg.get("point", "cp.pcp.huiliches")
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CRV App2 - LL/HH Step {APP2_VERSION}</title>
  <script src="https://code.highcharts.com/stock/highstock.js"></script>
  <script src="https://code.highcharts.com/highcharts-more.js"></script>
  <style>
    :root {{ --bg:#1f1f1f; --panel:#2b2b2b; --border:#444; --text:#eee; --muted:#aaa; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--text); }}
    .app {{ max-width:1320px; margin:0 auto; padding:16px; }}
    .head {{ display:flex; justify-content:space-between; gap:10px; align-items:center; margin-bottom:12px; }}
    h1 {{ margin:0; font-size:20px; font-weight:600; }}
    .ver {{ font-size:12px; color:#d0d0d0; background:#333; border:1px solid #555; border-radius:999px; padding:5px 9px; }}
    .bar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }}
    select,input,button {{ background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px 10px; }}
    select {{ min-width:360px; }}
    button {{ cursor:pointer; }}
    button:hover {{ background:#3a3a3a; }}
    #status {{ color:var(--muted); margin:8px 0; min-height:18px; }}
    #chart {{ height:620px; border:1px solid var(--border); border-radius:8px; background:#252525; }}
    .small {{ font-size:12px; color:var(--muted); }}
    .legend-chip {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; }}
  </style>
</head>
<body>
<div class="app">
  <div class="head"><h1>App2 - Dato real + LL/HH step horario sigma 3</h1><span class="ver">{APP2_VERSION}</span></div>
  <div class="bar">
    <select id="point"></select>
    <label>Rango <input id="fini" value="*-14d" style="width:90px"></label>
    <label>MA <input id="ma" type="number" value="5" style="width:70px"></label>
    <button id="load">Cargar</button>
  </div>
  <div id="status">Inicializando...</div>
  <div id="chart"></div>
  <p class="small">Hora Chile. <span class="legend-chip" style="background:#00bcd4"></span>lunes-viernes <span class="legend-chip" style="background:#e040fb"></span>sábado/domingo <span class="legend-chip" style="background:#ff1744"></span>feriado Chile. Línea vertical gris = 00:00.</p>
</div>
<script>
const APP2_VERSION = {APP2_VERSION!r};
const initialPoint = {point!r};
let chart = null;
const $ = id => document.getElementById(id);
Highcharts.setOptions({{ time: {{ timezone: 'America/Santiago', useUTC: false }} }});
function fmt(v,d=2) {{ return v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d); }}
function stepSeries(name, data, color, fill) {{ return {{ name:name, type:'arearange', step:'left', data:data||[], color:color, lineColor:color, fillColor:fill, fillOpacity:.10, lineWidth:1, marker:{{enabled:false}}, zIndex:2, tooltip:{{ pointFormatter:function() {{ return '<span style="color:'+color+'">●</span> '+name+': <b>LL '+fmt(this.low)+' / HH '+fmt(this.high)+'</b><br/>'; }} }} }}; }}
async function loadPoints() {{
  const r = await fetch('/api/points2');
  const data = await r.json();
  const sel = $('point');
  sel.innerHTML = '';
  (data.points || []).forEach(p => {{
    const o = document.createElement('option');
    o.value = p.tag;
    o.textContent = p.label;
    sel.appendChild(o);
  }});
  if ([...sel.options].some(o => o.value === initialPoint)) sel.value = initialPoint;
}}
async function loadChart() {{
  const p = $('point').value || initialPoint;
  const qs = new URLSearchParams({{ point:p, fini:$('fini').value, ma:$('ma').value }});
  $('status').textContent = 'Cargando ' + p + '...';
  const r = await fetch('/api/chart2?' + qs.toString());
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  $('status').textContent = `${{APP2_VERSION}} · ${{data.point}} · ${{data.count}} pts · ${{data.unit}} · ${{data.tz}}`;
  const opts = {{
    chart: {{ backgroundColor:'#252525', zoomType:'x', panning:{{enabled:true,type:'x'}}, panKey:'shift' }},
    accessibility: {{ enabled:false }},
    rangeSelector: {{ enabled:false }},
    navigator: {{ enabled:true, maskFill:'rgba(180,180,180,.15)', series:{{color:'#666', lineColor:'#888'}} }},
    credits: {{ enabled:false }},
    title: {{ text:null }},
    xAxis: {{ type:'datetime', plotLines:data.steps.midnight_lines || [], labels:{{style:{{color:'#aaa'}}}}, lineColor:'#555', tickColor:'#555' }},
    yAxis: {{ title:{{text:data.unit, style:{{color:'#aaa'}}}}, labels:{{style:{{color:'#aaa'}}}}, gridLineColor:'#3a3a3a' }},
    legend: {{ enabled:true, itemStyle:{{color:'#eee'}} }},
    tooltip: {{ shared:true, useHTML:true, backgroundColor:'rgba(30,30,30,.96)', borderColor:'#666', style:{{color:'#eee'}}, xDateFormat:'%Y-%m-%d %H:%M' }},
    plotOptions: {{ series:{{animation:false, states:{{inactive:{{opacity:1}}}}}}, arearange:{{lineWidth:1, marker:{{enabled:false}}}} }},
    series: [
      stepSeries('LL/HH lunes-viernes', data.steps.weekday, '#00bcd4', 'rgba(0,188,212,.10)'),
      stepSeries('LL/HH sábado-domingo', data.steps.weekend, '#e040fb', 'rgba(224,64,251,.10)'),
      stepSeries('LL/HH feriado Chile', data.steps.holiday, '#ff1744', 'rgba(255,23,68,.11)'),
      {{ name:'Valor real', type:'line', data:data.series, color:'#ffffff', lineWidth:2, marker:{{enabled:true, radius:2}}, zIndex:5, tooltip:{{valueDecimals:3, valueSuffix:' '+data.unit}} }}
    ]
  }};
  if (chart) chart.destroy();
  chart = Highcharts.stockChart('chart', opts);
}}
$('load').addEventListener('click', () => loadChart().catch(e => $('status').textContent = 'ERROR: ' + e.message));
loadPoints().then(loadChart).catch(e => $('status').textContent = 'ERROR: ' + e.message);
</script>
</body>
</html>"""


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api/points2")
def points2():
    cfg = base_cfg()
    presets = set(cfg.get("preset_points") or [])
    out = []
    try:
        r = requests.get(POINTS_URL, timeout=TIMEOUT)
        r.raise_for_status()
        for p in r.json().get("points", []):
            tag = p.get("tag") or p.get("point") or ""
            if not tag:
                continue
            name = p.get("nombre") or p.get("descripcion") or ""
            label = " - ".join(x for x in [tag, name] if x)
            out.append({"tag": tag, "label": label, "preset": tag in presets})
    except requests.RequestException:
        for tag in [cfg.get("point")] + list(cfg.get("preset_points") or []):
            if tag:
                out.append({"tag": tag, "label": tag, "preset": True})
    out.sort(key=lambda x: (not x["preset"], x["tag"]))
    return jsonify({"points": out, "count": len(out)})


@app.route("/api/chart2")
def chart2():
    point = (request.args.get("point") or base_cfg().get("point") or "").strip()
    if not point:
        return jsonify({"error": "missing point"}), 400
    fini = request.args.get("fini") or base_cfg().get("fini", "*-14d")
    ma = int(request.args.get("ma") or base_cfg().get("ma", 5))
    try:
        cfg = cfg_for_point(point, fini, ma)
        df, _, _ = prepare_dataset(cfg)
        prof = get_profile(point, cfg.get("unit", ""), cfg)
        unit = prof.get("unit") or cfg.get("unit", "")
        clean = df.dropna(subset=["time_local", "value"])
        series = [[ts_ms(r.time_local), round(float(r.value), 3)] for r in clean.itertuples() if math.isfinite(float(r.value))]
        steps = build_hourly_steps(df)
        return jsonify({"version": APP2_VERSION, "tz": TZ_NAME, "point": point, "unit": unit, "count": len(series), "series": series, "steps": steps})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "crv-app2", "version": APP2_VERSION, "port": int(os.environ.get("PORT", "5093")), "rt3": RT3_HOST})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5093"))
    print(f"{APP2_VERSION} en puerto {port} usando RT3_API_HOST={RT3_HOST}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=False)
