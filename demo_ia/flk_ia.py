#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flk_ia.py
Flask independiente (puerto 5059) para navegar vistas IA por recinto.

Objetivo:
- Mostrar contenido de /recintos/<recinto>/ia del servidor rt3-ia principal.
- Entregar menu para elegir recinto y abrir simulaciones rapidamente.
"""

import os
import re
import json
import configparser
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_ini():
    cfg_path = os.environ.get("FLK_IA_CONFIG", os.path.join(BASE_DIR, "flk_ia.ini"))
    parser = configparser.ConfigParser()
    if os.path.exists(cfg_path):
        parser.read(cfg_path)
    return parser


_ini = _load_ini()
_ini_section = _ini["rt3-flk-ia"] if _ini.has_section("rt3-flk-ia") else {}


def _env_or_ini(key, default=""):
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    return _ini_section.get(key, default)


# Cuando flk_ia corre en servidor externo, consumir IA via rt3-apirun (proxy /api/ia).
IA_BASE_URL = (_env_or_ini("IA_BASE_URL", "http://rt3-d2:8090/api/ia") or "").strip().rstrip("/")
API_VIEW_BASE = (_env_or_ini("API_VIEW_BASE", "http://rt3-d2:8090") or "").strip().rstrip("/")
HTTP_TIMEOUT = float(_env_or_ini("FLK_IA_TIMEOUT", "8"))
APP_HOST = (_env_or_ini("FLK_IA_HOST", "0.0.0.0") or "").strip()
APP_PORT = int(_env_or_ini("FLK_IA_PORT", "5059"))


def _fetch_recintos_from_index():
    """Lee recintos desde el HTML de la pagina principal rt3-ia."""
    url = IA_BASE_URL
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    html = r.text
    # Captura enlaces tipo /recintos/<nombre>/ia
    names = re.findall(r'/recintos/([^"/\?]+)/ia', html)
    cleaned = sorted(set([n.strip() for n in names if n and n.strip()]))
    return cleaned


def _build_ia_url(recinto):
    recinto = (recinto or "").strip().strip("/")
    if not recinto:
        return IA_BASE_URL
    return IA_BASE_URL + "/recintos/{}/ia".format(quote(recinto, safe=""))


def _build_projection_url(recinto, qin_fijo="", profile_group="weekday"):
    recinto = (recinto or "").strip().strip("/")
    if not recinto:
        return IA_BASE_URL
    profile = (profile_group or "weekday").strip().lower()
    if profile not in {"weekday", "weekend"}:
        profile = "weekday"
    qin = (qin_fijo or "").strip()
    params = [("vol_view", "today"), ("profile_group", profile)]
    if qin:
        params.insert(0, ("qin_fijo", qin))
    qs = "&".join(["{}={}".format(k, quote(str(v), safe=".-_")) for k, v in params])
    return IA_BASE_URL + "/recintos/{}/ia?{}".format(quote(recinto, safe=""), qs)


def _decode_first_json(raw_text):
    text = (raw_text or "").lstrip(" \t\r\n,")
    decoder = json.JSONDecoder()
    obj, idx = decoder.raw_decode(text)
    return obj, text[idx:]


def _find_balanced_segment(text, start_idx, open_char, close_char):
    if start_idx < 0 or start_idx >= len(text) or text[start_idx] != open_char:
        raise ValueError("segmento balanceado invalido")
    depth = 0
    in_string = False
    string_quote = ""
    escaped = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == string_quote:
                in_string = False
            continue
        if ch in ("'", '"'):
            in_string = True
            string_quote = ch
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start_idx:i + 1], i + 1
    raise ValueError("no se pudo cerrar segmento balanceado")


def _parse_js_value_literal(raw):
    s = (raw or "").strip()
    if not s:
        return None
    if s == "null":
        return None
    if s in ("true", "false"):
        return s == "true"
    try:
        return float(s)
    except Exception:
        return s.strip("'\"")


def _extract_chart_vol_data(html):
    marker = "renderLineChart('chart-vol',"
    pos = html.find(marker)
    if pos < 0:
        raise ValueError("No se encontro chart-vol en HTML de IA")
    tail = html[pos + len(marker):]
    labels_start = tail.find("[")
    if labels_start < 0:
        raise ValueError("No se encontraron labels de chart-vol")
    labels_raw, after_labels_idx = _find_balanced_segment(tail, labels_start, "[", "]")
    labels = json.loads(labels_raw)

    # Datasets vienen como sintaxis JS, no JSON puro.
    datasets_start = tail.find("[", after_labels_idx)
    if datasets_start < 0:
        raise ValueError("No se encontraron datasets de chart-vol")
    datasets_raw, after_datasets_idx = _find_balanced_segment(tail, datasets_start, "[", "]")

    values_arrays = []
    scan = 0
    while len(values_arrays) < 3:
        vpos = datasets_raw.find("values", scan)
        if vpos < 0:
            break
        bstart = datasets_raw.find("[", vpos)
        if bstart < 0:
            break
        arr_raw, bend = _find_balanced_segment(datasets_raw, bstart, "[", "]")
        try:
            values_arrays.append(json.loads(arr_raw))
        except Exception:
            values_arrays.append([])
        scan = bend

    options = {}
    options_start = tail.find("{", after_datasets_idx)
    if options_start >= 0:
        try:
            options_raw, _ = _find_balanced_segment(tail, options_start, "{", "}")
            for key in ("horizontal", "maxY", "bandMin", "bandMax"):
                m = re.search(r"\b" + key + r"\s*:\s*([^,\n}]+)", options_raw)
                if m:
                    options[key] = _parse_js_value_literal(m.group(1))
            mvl = re.search(r"\bverticalLines\s*:\s*(\[[\s\S]*?\])", options_raw)
            if mvl:
                try:
                    options["verticalLines"] = json.loads(mvl.group(1))
                except Exception:
                    options["verticalLines"] = []
        except Exception:
            options = {}

    out = {
        "labels": labels if isinstance(labels, list) else [],
        "vol_ia": [],
        "vol_ideal_ia": [],
        "vol_real": [],
        "qin_fijo_used": None,
        "target_vol": None,
        "max_y": None,
        "band_min": None,
        "band_max": None,
        "vertical_lines": [],
    }
    if len(values_arrays) > 0:
        out["vol_ia"] = values_arrays[0]
    if len(values_arrays) > 1:
        out["vol_ideal_ia"] = values_arrays[1]
    if len(values_arrays) > 2:
        out["vol_real"] = values_arrays[2]
    if isinstance(options, dict):
        out["target_vol"] = options.get("horizontal")
        out["max_y"] = options.get("maxY")
        out["band_min"] = options.get("bandMin")
        out["band_max"] = options.get("bandMax")
        out["vertical_lines"] = options.get("verticalLines") or []
    try:
        # Ejemplo esperado: "qin fijo = 61.01 l/s"
        m_qin = re.search(r"qin\s+fijo\s*=\s*([+-]?\d+(?:\.\d+)?)\s*l/s", html, re.IGNORECASE)
        if m_qin:
            out["qin_fijo_used"] = float(m_qin.group(1))
    except Exception:
        out["qin_fijo_used"] = None
    return out


def _fetch_projection_chart_payload(recinto, qin_fijo="", profile_group="weekday"):
    url = _build_projection_url(recinto, qin_fijo=qin_fijo, profile_group=profile_group)
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return _extract_chart_vol_data(r.text)


def _fetch_point_current_value(point_name):
    p = (point_name or "").strip()
    if not p:
        return None
    # Intento 1: endpoint simple por point
    try:
        url = API_VIEW_BASE + "/api/point/{}/value".format(quote(p, safe=""))
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.ok:
            payload = r.json()
            if isinstance(payload, dict) and payload.get("success"):
                return float(payload.get("value"))
    except Exception:
        pass

    # Intento 2: endpoint bulk (más robusto según despliegue)
    try:
        url2 = API_VIEW_BASE + "/api/points/values"
        r2 = requests.post(url2, json={"points": [p]}, timeout=HTTP_TIMEOUT)
        if not r2.ok:
            return None
        payload2 = r2.json()
        if not isinstance(payload2, dict) or not payload2.get("success"):
            return None
        values = payload2.get("values") or {}
        entry = values.get(p) if isinstance(values, dict) else None
        if isinstance(entry, dict) and entry.get("success"):
            return float(entry.get("value"))
    except Exception:
        return None
    return None


def _infer_recinto_from_qideal_point(point_name):
    p = (point_name or "").strip()
    if not p:
        return ""
    parts = p.split(".")
    if len(parts) < 2:
        return ""
    # Ej: caudal_ideal.tk_capi -> tk_capi
    return parts[-1].strip()


@app.route("/", methods=["GET"])
def index():
    selected = (request.args.get("recinto") or "").strip()
    error = ""
    recintos = []
    try:
        recintos = _fetch_recintos_from_index()
    except Exception as e:
        error = str(e)
    if not selected and recintos:
        selected = recintos[0]
    ia_url = _build_ia_url(selected) if selected else IA_BASE_URL
    return render_template_string(
        """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FLK IA - Simulaciones por recinto</title>
  <style>
    :root { color-scheme: light; }
    body { margin: 0; font-family: Arial, sans-serif; background: #f5f7fb; color: #1f2937; }
    .topbar {
      display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap;
      padding: 0.8rem 1rem; background: #111827; color: #f9fafb; border-bottom: 1px solid #374151;
    }
    .topbar label { font-size: 0.92rem; }
    .topbar select, .topbar input, .topbar button {
      height: 34px; border-radius: 6px; border: 1px solid #cbd5e1; padding: 0 0.55rem; font-size: 0.92rem;
    }
    .topbar button { cursor: pointer; background: #2563eb; color: white; border-color: #2563eb; }
    .topbar button:hover { background: #1d4ed8; }
    .topbar a { color: #93c5fd; text-decoration: none; }
    .topbar a:hover { text-decoration: underline; }
    .msg {
      margin: 0.6rem 1rem; padding: 0.55rem 0.7rem; border-radius: 6px; font-size: 0.9rem;
      background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
    }
    .frame-wrap { height: calc(100vh - 70px); padding: 0; }
    iframe { width: 100%; height: 100%; border: 0; background: #fff; }
  </style>
</head>
<body>
  <form class="topbar" method="get" action="/">
    <label for="recinto">Recinto:</label>
    <select name="recinto" id="recinto">
      {% for r in recintos %}
        <option value="{{ r }}" {% if r == selected %}selected{% endif %}>{{ r }}</option>
      {% endfor %}
    </select>
    <label for="custom">o manual:</label>
    <input id="custom" type="text" placeholder="ej: tk_crayada" oninput="setManual(this.value)">
    <button type="submit">Cargar IA</button>
    <a href="{{ ia_url }}" target="_blank" rel="noopener">Abrir en pestaña nueva</a>
    <span style="opacity:0.85;">Origen: {{ base_url }}</span>
  </form>

  {% if error %}
    <div class="msg">
      No se pudo cargar menu de recintos desde {{ base_url }}.
      Error: {{ error }}
    </div>
  {% endif %}

  <div class="frame-wrap">
    <iframe src="{{ ia_url }}" title="IA por recinto"></iframe>
  </div>

  <script>
    function setManual(v) {
      const sel = document.getElementById("recinto");
      if (!sel) return;
      const val = (v || "").trim();
      if (!val) return;
      const exists = Array.from(sel.options).some(o => o.value === val);
      if (!exists) {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = val + " (manual)";
        sel.appendChild(opt);
      }
      sel.value = val;
    }
  </script>
</body>
</html>
        """,
        recintos=recintos,
        selected=selected,
        ia_url=ia_url,
        base_url=IA_BASE_URL,
        error=error,
    )


@app.route("/proyeccion", methods=["GET"])
def projection_only():
    selected = (request.args.get("recinto") or "").strip()
    qin_fijo = (request.args.get("qin_fijo") or "").strip()
    qideal_point = (request.args.get("qideal_point") or "").strip()
    profile_group = (request.args.get("profile_group") or "weekday").strip().lower()
    if profile_group not in {"weekday", "weekend"}:
        profile_group = "weekday"

    error = ""
    recintos = []
    try:
        recintos = _fetch_recintos_from_index()
    except Exception as e:
        error = str(e)
    inferred_recinto = _infer_recinto_from_qideal_point(qideal_point)
    if inferred_recinto:
        selected = inferred_recinto
    if not selected and recintos:
        selected = recintos[0]
    # Primera carga: si no viene qin_fijo, tomar valor actual desde qideal_point.
    if (not qin_fijo) and qideal_point:
        try:
            point_val = _fetch_point_current_value(qideal_point)
            if point_val is not None:
                qin_fijo = str(point_val)
            else:
                error = "No se pudo leer valor actual de qideal_point: {}".format(qideal_point)
        except Exception as e:
            error = "Error leyendo qideal_point {}: {}".format(qideal_point, str(e))

    return render_template_string(
        """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FLK IA - Proyeccion volumen (solo grafica)</title>
  <style>
    :root { color-scheme: light; }
    body { margin: 0; font-family: Arial, sans-serif; background: #f5f7fb; color: #1f2937; }
    .topbar {
      display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap;
      padding: 0.8rem 1rem; background: #111827; color: #f9fafb; border-bottom: 1px solid #374151;
    }
    .topbar label { font-size: 0.92rem; }
    .topbar select, .topbar input, .topbar button {
      height: 34px; border-radius: 6px; border: 1px solid #cbd5e1; padding: 0 0.55rem; font-size: 0.92rem;
    }
    .topbar button { cursor: pointer; background: #2563eb; color: white; border-color: #2563eb; }
    .topbar button:hover { background: #1d4ed8; }
    .topbar a { color: #93c5fd; text-decoration: none; }
    .topbar a:hover { text-decoration: underline; }
    .hint { font-size: 0.82rem; opacity: 0.9; }
    .msg {
      margin: 0.6rem 1rem; padding: 0.55rem 0.7rem; border-radius: 6px; font-size: 0.9rem;
      background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
    }
    .panel { margin: 0.7rem 1rem 1rem; background:#fff; border:1px solid #dbe2ea; border-radius:8px; padding:0.65rem; }
    .chart-title { font-size: 1rem; margin: 0.1rem 0 0.6rem; color:#1f2937; }
    #chartWrap { position: relative; width: 100%; height: calc(100vh - 180px); min-height: 430px; }
    #chart { width: 100%; height: 100%; border: 1px solid #dbe2ea; border-radius: 6px; background:#fff; }
    #chartTooltip {
      position:absolute; display:none; pointer-events:none; z-index:5;
      background:#111827; color:#f9fafb; border-radius:6px; padding:6px 8px; font-size:12px; line-height:1.25;
      box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    }
    .legend { display:flex; gap:1rem; font-size:0.85rem; margin:0.45rem 0 0.25rem; color:#334155; }
    .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:0.35rem; vertical-align:middle; }
    .d1 { background:#ef6c00; } .d2 { background:#6b7280; } .d3 { background:#2e7d32; }
  </style>
</head>
<body>
  <form class="topbar" id="simForm" method="get" action="/proyeccion">
    <input type="hidden" name="recinto" id="recinto" value="{{ selected }}">
    <label for="qin_fijo">Qin fijo:</label>
    <input id="qin_fijo" type="text" name="qin_fijo" value="{{ qin_fijo }}" placeholder="ej: 61.01">
    <label for="qideal_point">Point qideal (GET):</label>
    <input id="qideal_point" type="text" name="qideal_point" value="{{ qideal_point }}" placeholder="ej: caudal_ideal.tk_capi">
    <span class="hint">Recinto detectado: {{ selected or '-' }}</span>
    <label for="profile_group">Perfil IA:</label>
    <select name="profile_group" id="profile_group">
      <option value="weekday" {% if profile_group == 'weekday' %}selected{% endif %}>Lunes-viernes</option>
      <option value="weekend" {% if profile_group == 'weekend' %}selected{% endif %}>Sabado-domingo</option>
    </select>
    <button type="submit">Proyectar</button>
    <span class="hint">Solo grafica: volumen proyectado hoy 00:00 a mañana 12:00</span>
  </form>

  {% if error %}
    <div class="msg">
      No se pudo cargar menu de recintos desde {{ base_url }}.
      Error: {{ error }}
    </div>
  {% endif %}

  <div class="panel">
    <h2 class="chart-title">Volumen proyectado hoy 00:00 a mañana 12:00 con qin fijo</h2>
    <div id="chartWrap">
      <svg id="chart" viewBox="0 0 1100 430" preserveAspectRatio="none"></svg>
      <div id="chartTooltip"></div>
    </div>
    <div class="legend">
      <span><i class="dot d1"></i>Vol IA</span>
      <span><i class="dot d2"></i>Vol ideal IA</span>
      <span><i class="dot d3"></i>Vol real</span>
    </div>
  </div>

  <script>
    function toNum(v) {
      if (v === null || v === undefined || v === '') return null;
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    }

    function svgEl(name, attrs) {
      const el = document.createElementNS('http://www.w3.org/2000/svg', name);
      Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, String(v)));
      return el;
    }

    function drawLineSeries(svg, values, color, minY, maxY, w, h, m) {
      const n = values.length;
      if (!n) return;
      const innerW = w - m.left - m.right;
      const innerH = h - m.top - m.bottom;
      let d = '';
      for (let i = 0; i < n; i++) {
        const v = toNum(values[i]);
        if (v === null) continue;
        const x = m.left + (n <= 1 ? 0 : (i * innerW / (n - 1)));
        const y = m.top + ((maxY - v) / (maxY - minY)) * innerH;
        d += (d ? ' L ' : 'M ') + x.toFixed(2) + ' ' + y.toFixed(2);
      }
      if (!d) return;
      svg.appendChild(svgEl('path', { d: d, fill: 'none', stroke: color, 'stroke-width': 2.2 }));
    }

    function drawChart(payload) {
      const svg = document.getElementById('chart');
      const chartWrap = document.getElementById('chartWrap');
      const tooltip = document.getElementById('chartTooltip');
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const view = svg.viewBox.baseVal;
      const w = view.width, h = view.height;
      const m = { left: 64, right: 20, top: 16, bottom: 48 };
      const labels = Array.isArray(payload.labels) ? payload.labels : [];
      const v1 = Array.isArray(payload.vol_ia) ? payload.vol_ia : [];
      const v2 = Array.isArray(payload.vol_ideal_ia) ? payload.vol_ideal_ia : [];
      const v3 = Array.isArray(payload.vol_real) ? payload.vol_real : [];
      const all = v1.concat(v2, v3).map(toNum).filter(v => v !== null);
      let minY = 0;
      let maxY = toNum(payload.max_y);
      if (maxY === null) maxY = all.length ? Math.max.apply(null, all) : 100;
      if (maxY <= minY) maxY = minY + 1;

      const innerW = w - m.left - m.right;
      const innerH = h - m.top - m.bottom;
      svg.appendChild(svgEl('rect', { x: m.left, y: m.top, width: w - m.left - m.right, height: h - m.top - m.bottom, fill: '#fbfdff', stroke: '#dbe2ea' }));
      svg.appendChild(svgEl('line', { x1: m.left, y1: m.top, x2: m.left, y2: h - m.bottom, stroke: '#64748b', 'stroke-width': 1 }));
      svg.appendChild(svgEl('line', { x1: m.left, y1: h - m.bottom, x2: w - m.right, y2: h - m.bottom, stroke: '#64748b', 'stroke-width': 1 }));
      for (let i = 0; i <= 5; i++) {
        const y = m.top + i * innerH / 5;
        svg.appendChild(svgEl('line', { x1: m.left, y1: y, x2: w - m.right, y2: y, stroke: '#e5e7eb', 'stroke-width': 1 }));
        const vTick = maxY - (i * (maxY - minY) / 5);
        const txt = svgEl('text', { x: m.left - 8, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#475569' });
        txt.textContent = vTick.toFixed(1);
        svg.appendChild(txt);
      }
      const xTicks = Math.min(8, Math.max(2, labels.length));
      for (let i = 0; i < xTicks; i++) {
        const idx = Math.round((labels.length - 1) * (i / (xTicks - 1)));
        const x = m.left + (labels.length <= 1 ? 0 : (idx * innerW / (labels.length - 1)));
        const t = svgEl('text', { x: x, y: h - m.bottom + 16, 'text-anchor': 'middle', 'font-size': 10, fill: '#475569' });
        t.textContent = labels[idx] || '';
        svg.appendChild(t);
      }
      const yTitle = svgEl('text', { x: 18, y: m.top + (innerH / 2), transform: 'rotate(-90 18 ' + (m.top + (innerH / 2)) + ')', 'text-anchor': 'middle', 'font-size': 11, fill: '#334155' });
      yTitle.textContent = 'Volumen';
      svg.appendChild(yTitle);
      const xTitle = svgEl('text', { x: m.left + (innerW / 2), y: h - 8, 'text-anchor': 'middle', 'font-size': 11, fill: '#334155' });
      xTitle.textContent = 'Fecha / Hora';
      svg.appendChild(xTitle);

      // Linea vertical roja punteada: marca tiempo actual (now), viene de IA en vertical_lines.
      const vlines = Array.isArray(payload.vertical_lines) ? payload.vertical_lines : [];
      const nowLine = vlines.find(v => String((v && v.color) || '').toLowerCase() === '#c62828');
      if (nowLine && nowLine.index !== undefined && nowLine.index !== null && labels.length > 0) {
        const idxNow = Number(nowLine.index);
        if (Number.isFinite(idxNow)) {
          const xNow = m.left + ((labels.length <= 1 ? 0 : (idxNow * innerW / (labels.length - 1))));
          svg.appendChild(svgEl('line', {
            x1: xNow, y1: m.top, x2: xNow, y2: h - m.bottom,
            stroke: '#c62828', 'stroke-width': 1.6, 'stroke-dasharray': '6 4'
          }));
          const nowTxt = svgEl('text', { x: xNow + 4, y: m.top + 12, 'text-anchor': 'start', 'font-size': 10, fill: '#c62828' });
          nowTxt.textContent = 'now';
          svg.appendChild(nowTxt);
        }
      }

      const target = toNum(payload.target_vol);
      if (target !== null) {
        const yT = m.top + ((maxY - target) / (maxY - minY)) * (h - m.top - m.bottom);
        svg.appendChild(svgEl('line', { x1: m.left, y1: yT, x2: w - m.right, y2: yT, stroke: '#c62828', 'stroke-width': 1.5, 'stroke-dasharray': '5 5' }));
      }

      drawLineSeries(svg, v1, '#ef6c00', minY, maxY, w, h, m);
      drawLineSeries(svg, v2, '#6b7280', minY, maxY, w, h, m);
      drawLineSeries(svg, v3, '#2e7d32', minY, maxY, w, h, m);

      const title = document.querySelector('.chart-title');
      const qin = document.getElementById('qin_fijo').value || '-';
      title.textContent = 'Volumen proyectado hoy 00:00 a mañana 12:00 con qin fijo = ' + qin;

      const hoverLine = svgEl('line', { x1: m.left, y1: m.top, x2: m.left, y2: h - m.bottom, stroke: '#334155', 'stroke-width': 1, 'stroke-dasharray': '4 4', display: 'none' });
      svg.appendChild(hoverLine);
      svg.onmousemove = function(ev) {
        const rect = svg.getBoundingClientRect();
        const px = ev.clientX - rect.left;
        const ratio = Math.max(0, Math.min(1, px / rect.width));
        const idx = labels.length <= 1 ? 0 : Math.round(ratio * (labels.length - 1));
        const x = m.left + (labels.length <= 1 ? 0 : (idx * innerW / (labels.length - 1)));
        hoverLine.setAttribute('x1', String(x));
        hoverLine.setAttribute('x2', String(x));
        hoverLine.setAttribute('display', 'block');
        const a = toNum(v1[idx]);
        const b = toNum(v2[idx]);
        const c = toNum(v3[idx]);
        tooltip.style.display = 'block';
        tooltip.style.left = Math.min(rect.width - 180, Math.max(6, px + 12)) + 'px';
        tooltip.style.top = Math.max(6, ev.clientY - rect.top - 42) + 'px';
        tooltip.innerHTML =
          '<div><b>' + (labels[idx] || '-') + '</b></div>' +
          '<div>Vol IA: ' + (a === null ? '-' : a.toFixed(2)) + '</div>' +
          '<div>Vol ideal IA: ' + (b === null ? '-' : b.toFixed(2)) + '</div>' +
          '<div>Vol real: ' + (c === null ? '-' : c.toFixed(2)) + '</div>';
      };
      svg.onmouseleave = function() {
        hoverLine.setAttribute('display', 'none');
        tooltip.style.display = 'none';
      };
    }

    async function loadProjection() {
      const recinto = document.getElementById('recinto').value;
      const qin = document.getElementById('qin_fijo').value.trim();
      const qidealPoint = document.getElementById('qideal_point').value.trim();
      const profile = document.getElementById('profile_group').value;
      const url = '/api/proyeccion/data?recinto=' + encodeURIComponent(recinto) + '&qin_fijo=' + encodeURIComponent(qin) + '&qideal_point=' + encodeURIComponent(qidealPoint) + '&profile_group=' + encodeURIComponent(profile);
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || ('status ' + res.status));
      }
      if (data.resolved_qin !== null && data.resolved_qin !== undefined) {
        document.getElementById('qin_fijo').value = String(data.resolved_qin);
      }
      drawChart(data.data || {});
    }

    document.getElementById('simForm').addEventListener('submit', async function(ev) {
      ev.preventDefault();
      const u = new URL(window.location.href);
      u.searchParams.set('qin_fijo', document.getElementById('qin_fijo').value.trim());
      u.searchParams.set('qideal_point', document.getElementById('qideal_point').value.trim());
      u.searchParams.set('profile_group', document.getElementById('profile_group').value);
      history.replaceState({}, '', u.toString());
      try { await loadProjection(); } catch (e) { alert('Error: ' + e.message); }
    });

    window.addEventListener('load', async function() {
      try { await loadProjection(); } catch (e) { console.error(e); }
    });
  </script>
</body>
</html>
        """,
        recintos=recintos,
        selected=selected,
        qin_fijo=qin_fijo,
        qideal_point=qideal_point,
        profile_group=profile_group,
        base_url=IA_BASE_URL,
        error=error,
    )


@app.route("/api/proyeccion/data", methods=["GET"])
def api_projection_data():
    recinto = (request.args.get("recinto") or "").strip()
    qin_fijo = (request.args.get("qin_fijo") or "").strip()
    qideal_point = (request.args.get("qideal_point") or "").strip()
    profile_group = (request.args.get("profile_group") or "weekday").strip().lower()
    if profile_group not in {"weekday", "weekend"}:
        profile_group = "weekday"
    if not recinto and qideal_point:
        recinto = _infer_recinto_from_qideal_point(qideal_point)
    if not recinto:
        return jsonify({"ok": False, "error": "missing recinto"}), 400
    try:
        resolved_qin = qin_fijo
        qin_source = "qin_fijo"
        warning = None
        valid_recintos = []
        try:
            valid_recintos = _fetch_recintos_from_index()
        except Exception:
            valid_recintos = []
        if valid_recintos and recinto not in valid_recintos:
            original = recinto
            recinto = valid_recintos[0]
            warning = "Recinto '{}' no existe. Se usa '{}'.".format(original, recinto)
        # Regla: si el usuario envía qin_fijo, ese valor manda.
        # qideal_point queda como valor inicial/fallback cuando qin_fijo está vacío.
        if (not resolved_qin) and qideal_point:
            point_val = _fetch_point_current_value(qideal_point)
            if point_val is None:
                # No cortar la simulación por typo/no encontrado:
                # dejar que IA use su valor por defecto (último qin disponible).
                msg = "No se pudo leer qideal_point: {}. Se usa qin por defecto del IA.".format(qideal_point)
                warning = (warning + " " + msg).strip() if warning else msg
                qin_source = "default_ia"
                resolved_qin = ""
            else:
                resolved_qin = str(point_val)
                qin_source = "qideal_point"
        try:
            payload = _fetch_projection_chart_payload(recinto, qin_fijo=resolved_qin, profile_group=profile_group)
        except Exception as e:
            msg = "No se pudo cargar grafica IA: {}".format(str(e))
            warning = (warning + " " + msg).strip() if warning else msg
            payload = {
                "labels": [],
                "vol_ia": [],
                "vol_ideal_ia": [],
                "vol_real": [],
                "target_vol": None,
                "max_y": None,
                "band_min": None,
                "band_max": None,
                "vertical_lines": [],
            }
        return jsonify(
            {
                "ok": True,
                "data": payload,
                "resolved_qin": resolved_qin if resolved_qin not in ("", None) else payload.get("qin_fijo_used"),
                "qin_source": qin_source,
                "qideal_point": qideal_point or None,
                "recinto": recinto,
                "warning": warning,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "service": "flk_ia", "base_url": IA_BASE_URL, "port": APP_PORT})


if __name__ == "__main__":
    print("flk_ia - http://{}:{}".format(APP_HOST, APP_PORT))
    print("IA_BASE_URL:", IA_BASE_URL)
    app.run(host=APP_HOST, port=APP_PORT, debug=False)

