#!/usr/bin/env python3
"""App simple: dato real + limites LL/HH step por hora."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from flask import Flask, jsonify, request

from analyze import DEFAULT_CONFIG, load_config, prepare_dataset
from variable_profiles import get_profile

BASE_DIR = Path(__file__).resolve().parent
RT3_HOST = os.environ.get("RT3_API_HOST", "http://rt3-d2:8090")
POINTS_URL = os.environ.get("RT3_POINTS_URL", f"{RT3_HOST}/api/points")
TIMEOUT = int(os.environ.get("RT3_API_TIMEOUT", "120"))

app = Flask(__name__)


def base_cfg() -> dict:
    if DEFAULT_CONFIG.is_file():
        return load_config(DEFAULT_CONFIG)
    return {"point": "cp.pcp.huiliches", "unit": "l/s", "fini": "*-14d", "ma": 5, "preset_points": []}


def cfg_for_point(point: str, fini: str, ma: int) -> dict:
    cfg = base_cfg()
    cfg.update({"point": point, "fini": fini, "ma": ma})
    over = (cfg.get("variable_overrides") or {}).get(point, {})
    prof = get_profile(point, over.get("unit") or "", cfg)
    cfg["unit"] = prof.get("unit") or cfg.get("unit", "")
    cfg["variable_type"] = prof.get("type")
    return cfg


def ts_ms(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


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


def build_hourly_steps(df: pd.DataFrame) -> dict[str, Any]:
    src = df.dropna(subset=["time_local", "value"]).copy()
    if src.empty:
        return {"ll": [], "hh": [], "band": []}
    src["hour"] = pd.to_datetime(src["time_local"]).dt.hour
    limits: dict[int, tuple[float, float]] = {}
    for hour, group in src.groupby("hour"):
        lim = sigma3_limits(group["value"].astype(float).tolist())
        if lim:
            limits[int(hour)] = lim
    start = pd.Timestamp(src["time_local"].min()).floor("h")
    end = pd.Timestamp(src["time_local"].max()).floor("h") + pd.Timedelta(hours=1)
    ll: list[list[float]] = []
    hh: list[list[float]] = []
    band: list[list[float]] = []
    cur = start
    while cur <= end:
        lim = limits.get(int(cur.hour))
        if lim:
            x = ts_ms(cur)
            lo, hi = lim
            ll.append([x, lo])
            hh.append([x, hi])
            band.append([x, lo, hi])
        cur += pd.Timedelta(hours=1)
    return {"ll": ll, "hh": hh, "band": band}


@app.route("/")
def index():
    cfg = base_cfg()
    point = request.args.get("point") or cfg.get("point", "cp.pcp.huiliches")
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CRV App2 - LL/HH Step</title>
  <script src="https://code.highcharts.com/stock/highstock.js"></script>
  <script src="https://code.highcharts.com/highcharts-more.js"></script>
  <style>
    :root {{ --bg:#1f1f1f; --panel:#2b2b2b; --border:#444; --text:#eee; --muted:#aaa; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--text); }}
    .app {{ max-width:1320px; margin:0 auto; padding:16px; }}
    h1 {{ margin:0 0 12px; font-size:20px; font-weight:600; }}
    .bar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }}
    select,input,button {{ background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px 10px; }}
    select {{ min-width:360px; }}
    button {{ cursor:pointer; }}
    button:hover {{ background:#3a3a3a; }}
    #status {{ color:var(--muted); margin:8px 0; min-height:18px; }}
    #chart {{ height:620px; border:1px solid var(--border); border-radius:8px; background:#252525; }}
    .small {{ font-size:12px; color:var(--muted); }}
  </style>
</head>
<body>
<div class="app">
  <h1>App2 - Dato real + LL/HH step horario sigma 3</h1>
  <div class="bar">
    <select id="point"></select>
    <label>Rango <input id="fini" value="*-14d" style="width:90px"></label>
    <label>MA <input id="ma" type="number" value="5" style="width:70px"></label>
    <button id="load">Cargar</button>
  </div>
  <div id="status">Inicializando...</div>
  <div id="chart"></div>
  <p class="small">Bandas calculadas en backend agrupando el historico visible por hora del dia. LL/HH = mediana ± 3 sigma, sigma estimado desde p10/p90.</p>
</div>
<script>
const initialPoint = {point!r};
let chart = null;
const $ = id => document.getElementById(id);
function fmt(v,d=2) {{ return v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d); }}
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
  $('status').textContent = `${{data.point}} · ${{data.count}} pts · ${{data.unit}} · LL/HH step horario sigma 3`;
  const opts = {{
    chart: {{ backgroundColor:'#252525', zoomType:'x', panning:{{enabled:true,type:'x'}}, panKey:'shift' }},
    accessibility: {{ enabled:false }},
    rangeSelector: {{ enabled:false }},
    navigator: {{ enabled:true, maskFill:'rgba(180,180,180,.15)', series:{{color:'#666', lineColor:'#888'}} }},
    credits: {{ enabled:false }},
    title: {{ text:null }},
    xAxis: {{ type:'datetime', labels:{{style:{{color:'#aaa'}}}}, lineColor:'#555', tickColor:'#555' }},
    yAxis: {{ title:{{text:data.unit, style:{{color:'#aaa'}}}}, labels:{{style:{{color:'#aaa'}}}}, gridLineColor:'#3a3a3a' }},
    legend: {{ enabled:true, itemStyle:{{color:'#eee'}} }},
    tooltip: {{
      shared:false, useHTML:true, backgroundColor:'rgba(30,30,30,.96)', borderColor:'#666', style:{{color:'#eee'}},
      formatter:function() {{
        const x = this.x;
        let html = '<b>' + Highcharts.dateFormat('%Y-%m-%d %H:%M', x) + '</b><br>';
        if (this.series.name === 'Valor real') html += '<b>Valor:</b> ' + fmt(this.y) + ' ' + data.unit;
        else if (this.point.low != null) html += '<b>LL/HH step:</b> ' + fmt(this.point.low) + ' - ' + fmt(this.point.high) + ' ' + data.unit;
        else html += this.series.name + ': ' + fmt(this.y) + ' ' + data.unit;
        const band = (data.steps.band || []).filter(b => b[0] <= x).slice(-1)[0];
        if (band) html += '<br><span style="display:inline-block;width:10px;height:10px;background:#00bcd4;margin-right:5px"></span><b>Limites:</b> LL ' + fmt(band[1]) + ' / HH ' + fmt(band[2]);
        return html;
      }}
    }},
    plotOptions: {{ series:{{animation:false}}, arearange:{{lineWidth:1, marker:{{enabled:false}}}} }},
    series: [
      {{ name:'Banda LL-HH step', type:'arearange', step:'left', data:data.steps.band, color:'#00bcd4', lineColor:'#00bcd4', fillColor:'rgba(0,188,212,.10)', fillOpacity:.10, zIndex:1 }},
      {{ name:'LL step', type:'line', step:'left', data:data.steps.ll, color:'#4dd0e1', dashStyle:'ShortDash', lineWidth:1, marker:{{enabled:false}}, zIndex:2 }},
      {{ name:'HH step', type:'line', step:'left', data:data.steps.hh, color:'#4dd0e1', dashStyle:'ShortDash', lineWidth:1, marker:{{enabled:false}}, zIndex:2 }},
      {{ name:'Valor real', type:'line', data:data.series, color:'#ffffff', lineWidth:2, marker:{{enabled:true, radius:2}}, zIndex:5 }}
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
        series = [[ts_ms(r.time_local), round(float(r.value), 3)] for r in df.dropna(subset=["time_local", "value"]).itertuples()]
        steps = build_hourly_steps(df)
        return jsonify({"point": point, "unit": unit, "count": len(series), "series": series, "steps": steps})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "crv-app2", "port": int(os.environ.get("PORT", "5093"))})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5093"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=False)
