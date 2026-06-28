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

APP2_VERSION = "app2-v2026.06.28-005"
TZ_NAME = "America/Santiago"
TZ = ZoneInfo(TZ_NAME)
DEFAULT_PATTERN_FINI = "*-365d"

BASE_DIR = Path(__file__).resolve().parent
RT3_HOST = os.environ.get("RT3_API_HOST", "http://rt3-d3:8090")
POINTS_URL = os.environ.get("RT3_POINTS_URL", f"{RT3_HOST}/api/points")
TIMEOUT = int(os.environ.get("RT3_API_TIMEOUT", "120"))

app = Flask(__name__)

KIND_STYLE = {
    "weekday": {"name": "LL/HH 3σ patron lunes-viernes", "color": "#00bcd4", "fill": "rgba(0,188,212,.10)"},
    "weekend": {"name": "LL/HH 3σ patron sabado-domingo", "color": "#e040fb", "fill": "rgba(224,64,251,.10)"},
    "holiday": {"name": "LL/HH 3σ feriado Chile usando patron sab-dom", "color": "#ff1744", "fill": "rgba(255,23,68,.12)"},
}


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


def physical_range(point: str, unit: str, cfg: dict | None = None) -> tuple[float, float | None, str]:
    """Rango fisico usado para limpiar entrenamiento y visualizacion."""
    cfg = cfg or {}
    text = f"{point} {unit} {cfg.get('variable_type','')}".lower()
    if "pres" in text or unit.lower() in {"bar", "psi", "mca"}:
        return 0.0, 100.0, "presion 0..100"
    if "caudal" in text or "flow" in text or "l/s" in unit.lower() or "m3" in unit.lower():
        return 0.0, 500.0, "caudal 0..500"
    return 0.0, None, "general >=0"


def filter_physical(df: pd.DataFrame, min_v: float, max_v: float | None) -> pd.DataFrame:
    if df.empty or "value" not in df.columns:
        return df
    out = df.dropna(subset=["time_local", "value"]).copy()
    vals = pd.to_numeric(out["value"], errors="coerce")
    mask = vals.notna() & vals.map(lambda v: math.isfinite(float(v))) & (vals >= min_v)
    if max_v is not None:
        mask = mask & (vals <= max_v)
    return out.loc[mask].copy()


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


def sigma_limits(vals: list[float]) -> dict[str, float] | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if len(clean) < 3:
        return None
    p10 = pct(clean, 0.10)
    p50 = pct(clean, 0.50)
    p90 = pct(clean, 0.90)
    sigma = (p90 - p10) / 2.563
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = max(abs(p50) * 0.01, 0.01)
    return {
        "p50": round(p50, 3),
        "sigma": round(sigma, 6),
        "ll3": round(p50 - 3 * sigma, 3),
        "hh3": round(p50 + 3 * sigma, 3),
        "ll4": round(p50 - 4 * sigma, 3),
        "hh4": round(p50 + 4 * sigma, 3),
        "n": len(clean),
    }


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


def model_kind(kind: str) -> str:
    return "weekend" if kind in ("weekend", "holiday") else "weekday"


def build_midnight_lines(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    cur = as_cl(start).floor("D")
    stop = as_cl(end).ceil("D")
    out = []
    while cur <= stop:
        out.append({"value": ts_ms(cur), "color": "#8a8a8a", "width": 1, "dashStyle": "ShortDot", "zIndex": 20})
        cur += pd.Timedelta(days=1)
    return out


def close_segment(segments: dict[str, list[list[list[float]]]], kind: str | None, points: list[list[float]]) -> None:
    if kind and len(points) >= 2:
        segments[kind].append(points[:])


def learn_hourly_limits(pattern_df: pd.DataFrame) -> dict[tuple[str, int], dict[str, float]]:
    src = pattern_df.dropna(subset=["time_local", "value"]).copy()
    if src.empty:
        return {}
    src["time_cl"] = src["time_local"].map(as_cl)
    src["hour_cl"] = src["time_cl"].map(lambda t: int(t.hour))
    src["visual_kind"] = src["time_cl"].map(day_kind)
    src["model_kind"] = src["visual_kind"].map(model_kind)

    limits: dict[tuple[str, int], dict[str, float]] = {}
    for (mkind, hour), group in src.groupby(["model_kind", "hour_cl"]):
        lim = sigma_limits(group["value"].astype(float).tolist())
        if lim:
            limits[(str(mkind), int(hour))] = lim

    for hour in range(24):
        if ("weekend", hour) not in limits and ("weekday", hour) in limits:
            limits[("weekend", hour)] = limits[("weekday", hour)]
        if ("weekday", hour) not in limits and ("weekend", hour) in limits:
            limits[("weekday", hour)] = limits[("weekend", hour)]
    return limits


def build_hourly_steps(pattern_df: pd.DataFrame, visible_df: pd.DataFrame) -> dict[str, Any]:
    visible = visible_df.dropna(subset=["time_local", "value"]).copy()
    if visible.empty:
        empty = {"weekday": [], "weekend": [], "holiday": []}
        return {"segments3": empty, "segments4": empty, "bands": [], "midnight_lines": [], "styles": KIND_STYLE}

    limits = learn_hourly_limits(pattern_df)
    start = visible["time_local"].map(as_cl).min().floor("h")
    end = visible["time_local"].map(as_cl).max().floor("h") + pd.Timedelta(hours=1)

    segments3: dict[str, list[list[list[float]]]] = {"weekday": [], "weekend": [], "holiday": []}
    segments4: dict[str, list[list[list[float]]]] = {"weekday": [], "weekend": [], "holiday": []}
    bands: list[dict[str, Any]] = []
    cur_kind: str | None = None
    cur_points3: list[list[float]] = []
    cur_points4: list[list[float]] = []
    prev_x: int | None = None
    prev_lim: dict[str, float] | None = None
    prev_kind: str | None = None

    cur = start
    while cur <= end:
        vkind = day_kind(cur)
        mkind = model_kind(vkind)
        lim = limits.get((mkind, int(cur.hour)))
        if not lim:
            close_segment(segments3, cur_kind, cur_points3)
            close_segment(segments4, cur_kind, cur_points4)
            cur_kind = None
            cur_points3 = []
            cur_points4 = []
            prev_x = None
            prev_lim = None
            prev_kind = None
            cur += pd.Timedelta(hours=1)
            continue

        x = ts_ms(cur)
        if cur_kind != vkind:
            close_segment(segments3, cur_kind, cur_points3)
            close_segment(segments4, cur_kind, cur_points4)
            cur_kind = vkind
            cur_points3 = []
            cur_points4 = []
            prev_x = None
            prev_lim = None
            prev_kind = None

        cur_points3.append([x, lim["ll3"], lim["hh3"]])
        cur_points4.append([x, lim["ll4"], lim["hh4"]])
        if prev_x is not None and prev_lim is not None and prev_kind == vkind:
            style = KIND_STYLE[vkind]
            bands.append({
                "from": prev_x, "to": x, "ll3": prev_lim["ll3"], "hh3": prev_lim["hh3"],
                "ll4": prev_lim["ll4"], "hh4": prev_lim["hh4"], "kind": vkind,
                "name": style["name"], "color": style["color"],
            })

        prev_x, prev_lim, prev_kind = x, lim, vkind
        cur += pd.Timedelta(hours=1)

    close_segment(segments3, cur_kind, cur_points3)
    close_segment(segments4, cur_kind, cur_points4)
    return {"segments3": segments3, "segments4": segments4, "bands": bands, "midnight_lines": build_midnight_lines(start, end), "styles": KIND_STYLE}


def detect_three_point_alerts(real_df: pd.DataFrame, limits: dict[tuple[str, int], dict[str, float]], min_v: float, max_v: float | None) -> list[list[float]]:
    clean = filter_physical(real_df, min_v, max_v)
    if clean.empty:
        return []
    clean["time_cl"] = clean["time_local"].map(as_cl)
    clean = clean.sort_values("time_cl")
    out: list[list[float]] = []
    streak = 0
    for row in clean.itertuples():
        t = as_cl(row.time_local)
        val = float(row.value)
        kind = model_kind(day_kind(t))
        lim = limits.get((kind, int(t.hour)))
        outside3 = False
        if lim and math.isfinite(val):
            outside3 = val < lim["ll3"] or val > lim["hh3"]
        if outside3:
            streak += 1
            if streak >= 3:
                out.append([ts_ms(t), round(val, 3)])
        else:
            streak = 0
    return out


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
    #chart {{ height:620px; border:1px solid var(--border); border-radius:8px; background:#252525; overflow:hidden; }}
    .chart-inner {{ height:620px; width:100%; }}
    .small {{ font-size:12px; color:var(--muted); }}
    .legend-chip {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; }}
  </style>
</head>
<body>
<div class="app">
  <div class="head"><h1>App2 - Real visible + patron historico LL/HH horario</h1><span class="ver">{APP2_VERSION}</span></div>
  <div class="bar">
    <select id="point"></select>
    <label>Real <input id="fini" value="*-14d" style="width:90px"></label>
    <label>Patron <input id="pattern_fini" value="{DEFAULT_PATTERN_FINI}" style="width:90px"></label>
    <label>MA <input id="ma" type="number" value="5" style="width:70px"></label>
    <button id="load">Cargar</button>
  </div>
  <div id="status">Inicializando...</div>
  <div id="chart"></div>
  <p class="small">Entrenamiento limpio: presión 0..100, caudal 0..500, otros >=0. <span class="legend-chip" style="background:#00bcd4"></span>3σ <span class="legend-chip" style="background:#ff1744"></span>4σ alerta. Punto rojo = 3 puntos consecutivos fuera de 3σ.</p>
</div>
<script>
const APP2_VERSION = {APP2_VERSION!r};
const initialPoint = {point!r};
let chart = null;
let chartSeq = 0;
const $ = id => document.getElementById(id);
Highcharts.setOptions({{ time: {{ timezone: 'America/Santiago', useUTC: false }} }});
function fmt(v,d=2) {{ return v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d); }}
function bandAt(bands, x) {{ for (let i=0;i<bands.length;i++) {{ const b=bands[i]; if (x>=b.from && x<b.to) return b; }} return null; }}
function make3SigmaSeries(segments, styles) {{
  const out=[];
  ['weekday','weekend','holiday'].forEach(kind=>{{ const st=styles[kind]; (segments[kind]||[]).forEach((seg,idx)=>{{ if(!seg||seg.length<2)return; out.push({{name:st.name+(idx?' '+(idx+1):''),type:'arearange',step:'left',data:seg,color:st.color,lineColor:st.color,fillColor:st.fill,fillOpacity:.10,lineWidth:1,marker:{{enabled:false}},enableMouseTracking:false,linkedTo:idx?':previous':undefined,showInLegend:idx===0,zIndex:3}}); }}); }});
  return out;
}}
function make4SigmaSeries(segments) {{
  const out=[]; let shownBand=false,shownLL=false,shownHH=false;
  ['weekday','weekend','holiday'].forEach(kind=>{{ (segments[kind]||[]).forEach(seg=>{{ if(!seg||seg.length<2)return; out.push({{name:'ALERTA 4σ zona',type:'arearange',step:'left',data:seg,color:'#ff1744',lineColor:'#ff1744',fillColor:'rgba(255,23,68,.045)',fillOpacity:.045,dashStyle:'ShortDash',lineWidth:1,marker:{{enabled:false}},enableMouseTracking:false,showInLegend:!shownBand,linkedTo:shownBand?':previous':undefined,zIndex:1}}); shownBand=true; const ll=seg.map(p=>[p[0],p[1]]),hh=seg.map(p=>[p[0],p[2]]); out.push({{name:'LL 4σ alerta',type:'line',step:'left',data:ll,color:'#ff1744',dashStyle:'ShortDash',lineWidth:1.2,marker:{{enabled:false}},enableMouseTracking:false,showInLegend:!shownLL,linkedTo:shownLL?':previous':undefined,zIndex:4}}); out.push({{name:'HH 4σ alerta',type:'line',step:'left',data:hh,color:'#ff1744',dashStyle:'ShortDash',lineWidth:1.2,marker:{{enabled:false}},enableMouseTracking:false,showInLegend:!shownHH,linkedTo:shownHH?':previous':undefined,zIndex:4}}); shownLL=true; shownHH=true; }}); }});
  return out;
}}
async function loadPoints() {{
  const r=await fetch('/api/points2'); const data=await r.json(); const sel=$('point'); sel.innerHTML='';
  (data.points||[]).forEach(p=>{{ const o=document.createElement('option'); o.value=p.tag; o.textContent=p.label; sel.appendChild(o); }});
  if ([...sel.options].some(o=>o.value===initialPoint)) sel.value=initialPoint;
}}
async function loadChart() {{
  const p=$('point').value||initialPoint;
  const qs=new URLSearchParams({{point:p,fini:$('fini').value,pattern_fini:$('pattern_fini').value,ma:$('ma').value}});
  $('status').textContent='Cargando '+p+'...';
  const r=await fetch('/api/chart2?'+qs.toString()); const data=await r.json(); if(!r.ok)throw new Error(data.error||r.statusText);
  $('status').textContent=`${{APP2_VERSION}} · ${{data.point}} · real ${{data.count}} pts · patrón limpio ${{data.pattern_count}}/${{data.pattern_raw_count}} pts · ${{data.filter}} · alertas ${{(data.alert_points||[]).length}} · ${{data.unit}} · ${{data.tz}}`;
  const holder=$('chart'); const innerId='chart_inner_'+(++chartSeq); holder.innerHTML='<div id="'+innerId+'" class="chart-inner"></div>';
  const bands=data.steps.bands||[];
  const series=make4SigmaSeries(data.steps.segments4||{{}}).concat(make3SigmaSeries(data.steps.segments3||{{}},data.steps.styles||{{}})).concat([
    {{name:'Valor real',type:'line',data:data.series,color:'#ffffff',lineWidth:2,marker:{{enabled:false}},zIndex:7,tooltip:{{valueDecimals:3,valueSuffix:' '+data.unit}}}},
    {{name:'Alerta 3 puntos fuera 3σ',type:'scatter',data:data.alert_points||[],color:'#ff1744',marker:{{enabled:true,radius:4,symbol:'circle',lineColor:'#fff',lineWidth:1}},zIndex:10}}
  ]);
  chart=Highcharts.stockChart(innerId,{{chart:{{backgroundColor:'#252525',zoomType:'x',panning:{{enabled:true,type:'x'}},panKey:'shift'}},accessibility:{{enabled:false}},rangeSelector:{{enabled:false}},navigator:{{enabled:true,maskFill:'rgba(180,180,180,.15)',series:{{color:'#666',lineColor:'#888'}}}},credits:{{enabled:false}},title:{{text:null}},xAxis:{{type:'datetime',plotLines:data.steps.midnight_lines||[],labels:{{style:{{color:'#aaa'}}}},lineColor:'#555',tickColor:'#555'}},yAxis:{{title:{{text:data.unit,style:{{color:'#aaa'}}}},labels:{{style:{{color:'#aaa'}}}},gridLineColor:'#3a3a3a'}},legend:{{enabled:true,itemStyle:{{color:'#eee'}}}},tooltip:{{shared:false,useHTML:true,backgroundColor:'rgba(30,30,30,.96)',borderColor:'#666',style:{{color:'#eee'}},formatter:function(){{const x=this.x||(this.point&&this.point.x);let html='<b>'+Highcharts.dateFormat('%Y-%m-%d %H:%M',x)+'</b><br>';html+='<span style="color:'+this.series.color+'">●</span> '+this.series.name+': <b>'+fmt(this.y,3)+'</b> '+data.unit;const b=bandAt(bands,x);if(b)html+='<br><span style="color:'+b.color+'">●</span> 3σ: <b>LL '+fmt(b.ll3)+' / HH '+fmt(b.hh3)+'</b><br><span style="color:#ff1744">●</span> 4σ alerta: <b>LL '+fmt(b.ll4)+' / HH '+fmt(b.hh4)+'</b>';return html;}}}},plotOptions:{{series:{{animation:false,states:{{inactive:{{opacity:1}}}}}},arearange:{{lineWidth:1,marker:{{enabled:false}},enableMouseTracking:false}}}},series:series}});
}}
$('load').addEventListener('click',()=>loadChart().catch(e=>$('status').textContent='ERROR: '+e.message));
loadPoints().then(loadChart).catch(e=>$('status').textContent='ERROR: '+e.message);
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
    pattern_fini = request.args.get("pattern_fini") or DEFAULT_PATTERN_FINI
    ma = int(request.args.get("ma") or base_cfg().get("ma", 5))
    try:
        real_cfg = cfg_for_point(point, fini, ma)
        pattern_cfg = cfg_for_point(point, pattern_fini, ma)
        df_real, _, _ = prepare_dataset(real_cfg)
        df_pattern_raw, _, _ = prepare_dataset(pattern_cfg)
        prof = get_profile(point, real_cfg.get("unit", ""), real_cfg)
        unit = prof.get("unit") or real_cfg.get("unit", "")
        min_v, max_v, filter_label = physical_range(point, unit, real_cfg)
        df_pattern = filter_physical(df_pattern_raw, min_v, max_v)
        df_real_clean = filter_physical(df_real, min_v, max_v)
        clean = df_real_clean.dropna(subset=["time_local", "value"])
        series = [[ts_ms(r.time_local), round(float(r.value), 3)] for r in clean.itertuples() if math.isfinite(float(r.value))]
        limits = learn_hourly_limits(df_pattern)
        steps = build_hourly_steps(df_pattern, df_real_clean)
        alert_points = detect_three_point_alerts(df_real_clean, limits, min_v, max_v)
        pattern_count = int(len(df_pattern.dropna(subset=["time_local", "value"])))
        pattern_raw_count = int(len(df_pattern_raw.dropna(subset=["time_local", "value"])))
        return jsonify({"version": APP2_VERSION, "tz": TZ_NAME, "point": point, "unit": unit, "count": len(series), "pattern_count": pattern_count, "pattern_raw_count": pattern_raw_count, "filter": filter_label, "alert_points": alert_points, "fini": fini, "pattern_fini": pattern_fini, "series": series, "steps": steps})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "crv-app2", "version": APP2_VERSION, "port": int(os.environ.get("PORT", "5093")), "rt3": RT3_HOST, "default_pattern_fini": DEFAULT_PATTERN_FINI})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5093"))
    print(f"{APP2_VERSION} en puerto {port} usando RT3_API_HOST={RT3_HOST}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=False)
