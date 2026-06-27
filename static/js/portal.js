(function () {
  'use strict';

  const app = document.querySelector('.app');
  let point = app.dataset.point;
  let unit = app.dataset.unit || 'l/s';
  let chart = null;
  let lastSeries = [];
  let lastChartData = null;
  let chartMeta = { point: '', count: 0 };
  let pinnedXExtremes = null;
  let pendingChartRestore = null;

  const $ = (id) => document.getElementById(id);
  const els = {
    badge: $('badge'), status: $('status'), pointSel: $('sel-point'), pointSearch: $('point-search'),
    range: $('sel-range'), ma: $('sel-ma'), analyze: $('btn-analyze'), refresh: $('btn-refresh'),
    vCaudal: $('v-caudal'), vProb: $('v-prob'), vTipo: $('v-tipo'),
    vHomolog1d: $('v-homolog-1d'), vHomolog7d: $('v-homolog-7d'), vHomolog30d: $('v-homolog-30d'),
    vRuptures: $('v-ruptures'), vAuc: $('v-auc'),
    vNmin: $('v-nmin'), vNmax: $('v-nmax'), recentNights: $('recent-nights'), anomalies: $('anomalies'),
    features: $('features'), history: $('history'), chart: $('chart'), riskPanel: $('risk-panel'),
    dailyStats: $('daily-stats'), dailyStatsTitle: $('daily-stats-title'), panelDailyStats: $('panel-daily-stats'),
    sixsigmaPanel: $('sixsigma-patterns'),
    hidePct: $('chk-hide-pct'), hideLh: $('chk-hide-lh'), showSigma: $('chk-sixsigma'),
    lhSigma: $('chk-lh-sigma'), lhSigmaSel: $('sel-lh-sigma'), legendLhNormal: $('legend-lh-normal'),
    volumeToolbar: $('volume-toolbar'), qinMode: $('sel-qin-mode'), qinManual: $('inp-qin-manual'),
    qinApply: $('btn-qin-apply'), volumeMeta: $('volume-meta'),
    btnProjTable: $('btn-proj-table'), projModal: $('volume-projection-modal'),
    projModalClose: $('btn-proj-modal-close'), projBackdrop: $('volume-projection-backdrop'),
    projDetail: $('volume-projection-detail'), projCaption: $('volume-projection-caption'),
    tanksSummary: $('volume-tanks-summary'),
    legendVol: document.querySelectorAll('.legend-vol'),
    legendLh: document.querySelectorAll('.legend-lh'),
    legendPct: document.querySelectorAll('.legend-pct'),
    legendSigma: document.querySelectorAll('.legend-sigma'),
    zoomBtns: document.querySelectorAll('.btn-z'),
  };

  function setStatus(msg, err) {
    els.status.textContent = msg || '';
    els.status.className = 'status' + (err ? ' error' : '');
  }

  function currentPoint() { return els.pointSel.value || point; }

  function updateUrl(tag) {
    const u = new URL(location.href);
    u.searchParams.set('point', tag);
    history.replaceState(null, '', u);
    document.title = 'GPU Tag — ' + tag;
  }

  function setBadge(estado) {
    const e = estado || 'ok';
    els.badge.textContent = e.replace('_', ' ');
    els.badge.className = 'badge badge-' + e;
  }

  function applyUnit(nextUnit, profile) {
    if (nextUnit) unit = nextUnit;
    const lbl = document.querySelector('.card .lbl + #v-caudal');
    const cardLbl = els.vCaudal && els.vCaudal.previousElementSibling;
    if (cardLbl && profile) {
      const t = profile.type || '';
      cardLbl.textContent = t === 'nivel' ? 'Nivel' : t === 'volumen' ? 'Volumen' : t === 'presion' ? 'Presión' : 'Valor';
    }
  }

  function fmtPct(v) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    if (Number.isNaN(n)) return '—';
    const cls = n < 0 ? 'pct-down' : n > 0 ? 'pct-up' : '';
    return '<span class="' + cls + '">' + (n > 0 ? '+' : '') + n.toFixed(1) + '%</span>';
  }

  function seriesValueName(profile) {
    const t = (profile && profile.type) || '';
    if (t === 'nivel') return 'Nivel';
    if (t === 'volumen') return 'Volumen actual';
    if (t === 'presion') return 'Presión';
    if (t === 'caudal_entrada') return 'Caudal entrada';
    if (t === 'caudal_salida') return 'Caudal salida';
    return 'Valor';
  }

  function fmtTable(rows, cols, emptyMsg) {
    if (!rows || !rows.length) return '<p class="empty">' + (emptyMsg || 'Sin datos') + '</p>';
    let h = '<table><thead><tr>' + cols.map((c) => '<th>' + c.label + '</th>').join('') + '</tr></thead><tbody>';
    rows.forEach((r) => {
      h += '<tr>' + cols.map((c) => '<td>' + (r[c.key] ?? '—') + '</td>').join('') + '</tr>';
    });
    return h + '</tbody></table>';
  }

  async function loadPoints(q) {
    const url = '/api/points?limit=0' + (q ? '&q=' + encodeURIComponent(q) : '');
    const data = await fetch(url).then((r) => r.json());
    const sel = els.pointSel;
    const cur = sel.value || point;
    sel.innerHTML = '';
    (data.points || []).forEach((p) => {
      const o = document.createElement('option');
      o.value = p.tag;
      o.textContent = p.label;
      sel.appendChild(o);
    });
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    else if (sel.options.length) { sel.selectedIndex = 0; point = sel.value; }
  }

  function clearReport() {
    setBadge('ok');
    els.badge.textContent = '—';
    els.vCaudal.textContent = '—';
    els.vProb.textContent = '—';
    if (els.vTipo) els.vTipo.textContent = '—';
    if (els.vHomolog1d) els.vHomolog1d.textContent = '—';
    if (els.vHomolog7d) els.vHomolog7d.textContent = '—';
    if (els.vHomolog30d) els.vHomolog30d.textContent = '—';
    if (els.dailyStats) els.dailyStats.innerHTML = '<p class="empty">Sin datos</p>';
    els.vRuptures.textContent = '—';
    els.vAuc.textContent = '—';
    els.vNmin.textContent = '—';
    els.vNmax.textContent = '—';
    els.recentNights.innerHTML = '<p class="empty">Sin reporte — pulsa Analizar GPU</p>';
    els.anomalies.innerHTML = '<p class="empty">Sin reporte</p>';
    els.features.innerHTML = '<li class="empty">Ejecuta Analizar GPU</li>';
  }

  function renderReport(report) {
    const sc = report.current_score || {};
    const risk = report.risk_assessment || {};
    const prof = report.variable_profile || {};
    const noct = (report.nocturnal || {}).summary || {};
    const metrics = report.model_metrics || {};
    applyUnit(report.unit || prof.unit, prof);
    setBadge(risk.estado || sc.estado);
    if (els.vTipo) els.vTipo.textContent = prof.label || risk.tipo_label || '—';
    els.vCaudal.textContent = sc.caudal != null ? sc.caudal + ' ' + (report.unit || unit) : '—';
    els.vProb.textContent = sc.prob != null ? (sc.prob * 100).toFixed(1) + '%' : '—';
    const h = risk.homolog || {};
    if (els.vHomolog1d) els.vHomolog1d.innerHTML = fmtPct(h.dev_pct_1d);
    if (els.vHomolog7d) els.vHomolog7d.innerHTML = fmtPct(h.dev_pct_7d);
    if (els.vHomolog30d) els.vHomolog30d.innerHTML = fmtPct(h.dev_pct_30d);
    els.vRuptures.textContent = report.rupture_events ?? '—';
    els.vAuc.textContent = metrics.roc_auc ?? '—';
    els.vNmin.textContent = noct.min_nocturno_global != null ? noct.min_nocturno_global + ' ' + unit : '—';
    els.vNmax.textContent = noct.max_nocturno_global != null ? noct.max_nocturno_global + ' ' + unit : '—';
    els.recentNights.innerHTML = fmtTable((report.nocturnal || {}).recent_nights, [
      { key: 'date_local', label: 'Fecha' },
      { key: 'night_min', label: 'Min' },
      { key: 'night_max', label: 'Max' },
      { key: 'night_mean', label: 'Media' },
    ], 'Sin noches');
    els.anomalies.innerHTML = fmtTable((report.nocturnal || {}).anomalies, [
      { key: 'date_local', label: 'Fecha' },
      { key: 'night_min', label: 'Min' },
      { key: 'night_max', label: 'Max' },
      { key: 'anomaly', label: 'Tipo' },
    ], 'Sin anomalías');
    els.features.innerHTML = '';
    ((metrics.top_features || []).slice(0, 8)).forEach((f) => {
      const li = document.createElement('li');
      li.innerHTML = '<span>' + f.feature + '</span><span>' + f.importance + '</span>';
      els.features.appendChild(li);
    });
    if (!metrics.top_features || !metrics.top_features.length) {
      els.features.innerHTML = '<li class="empty">Ejecuta Analizar GPU</li>';
    }
    renderRiskPanel(risk, prof);
    const pre = (report.current_alarm || {}).prealarm;
    if (pre && pre.score >= 60 && els.riskPanel) {
      const comp = pre.componentes || {};
      els.riskPanel.innerHTML += '<p class="empty" style="margin-top:8px">Score compuesto: <strong>' + pre.score + '</strong>/100 ('
        + (pre.color || '') + ') — pct ' + comp.percentil + ' · Δ ' + comp.razon_cambio
        + ' · pers ' + comp.persistencia + ' · tend ' + comp.tendencia + ' · corr ' + comp.correlacion + '</p>';
    }
    renderDailyStats(report.daily_stats || [], prof, report.unit || unit);
    const alarm = report.current_alarm || {};
    const lim = report.limits || {};
    const bandas = lim.bandas_ll_hh || {};
    if (alarm.ll_activo != null || alarm.hh_activo != null || alarm.l_activo != null) {
      const extra = ' · L=' + (alarm.l_activo ?? '—') + ' H=' + (alarm.h_activo ?? '—')
        + ' LL=' + (alarm.ll_activo ?? '—') + ' HH=' + (alarm.hh_activo ?? '—');
      const dow = bandas.por_dow
        ? ' (L/H p' + (bandas.l_pct || 10) + '/p' + (bandas.h_pct || 90)
          + ' · LL/HH p' + String(bandas.ll_pct || 2).padStart(2, '0') + '/p' + (bandas.hh_pct || 98) + ' wd/sat/dom)'
        : '';
      const prefix = alarm.rotura_inmediata ? 'ROTURA INMEDIATA: '
        : (alarm.prealarm && alarm.prealarm.score >= 95 ? 'PREALARMA ROJA: '
        : (alarm.prealarm && alarm.prealarm.score >= 80 ? 'PREALARMA NARANJA: '
        : (alarm.prealarm && alarm.prealarm.score >= 60 ? 'PREALARMA AMARILLA: '
        : (alarm.in_alarm ? 'ALARMA ' + alarm.tipo + ': '
        : (alarm.advertencia ? 'ADVERTENCIA: ' : 'Reporte: ')))));
      setStatus(
        prefix + (alarm.mensaje || '') + ' · ' +
        (report.generated_at || '').replace('T', ' ').slice(0, 19) + ' UTC' + extra + dow,
        alarm.in_alarm
      );
    } else {
      setStatus('Reporte: ' + (report.generated_at || '').replace('T', ' ').slice(0, 19) + ' UTC');
    }
  }

  function renderRiskPanel(risk, prof) {
    if (!els.riskPanel) return;
    if (!risk || !risk.mensaje) {
      els.riskPanel.innerHTML = '<p class="empty">Ejecuta Analizar GPU para evaluar riesgo</p>';
      return;
    }
    const tags = (risk.reglas || []).map((r) => {
      const cls = risk.nivel >= 3 ? 'risk-tag alarm' : 'risk-tag';
      return '<span class="' + cls + '">' + r.replace(/_/g, ' ') + '</span>';
    }).join('');
    const riesgos = (prof.riesgos || []).map((r) => '<span class="risk-tag">' + r.replace(/_/g, ' ') + '</span>').join('');
    els.riskPanel.innerHTML =
      '<p class="risk-msg"><strong>' + (risk.estado || 'ok').replace('_', ' ') + '</strong> — ' + risk.mensaje + '</p>' +
      (tags ? '<div class="risk-tags">' + tags + '</div>' : '') +
      (prof.hh_significa ? '<p class="empty" style="margin-top:8px">' + prof.hh_significa + '</p>' : '') +
      (riesgos ? '<p class="empty">Monitorea: ' + riesgos + '</p>' : '');
  }

  function renderDailyStats(rows, prof, u) {
    if (!els.dailyStats) return;
    const isNivel = (prof && prof.type) === 'nivel';
    const unitLbl = u || unit;
    if (els.dailyStatsTitle) {
      els.dailyStatsTitle.textContent = isNivel
        ? 'Nivel diario (% vs día / semana / mes anterior)'
        : 'Referencia diaria (% vs día / semana / mes anterior)';
    }
    if (!rows || !rows.length) {
      els.dailyStats.innerHTML = '<p class="empty">Sin datos diarios — amplía el rango (≥30 días para % mes)</p>';
      return;
    }
    const display = rows.slice().reverse().map((r) => ({
      date_local: r.date_local,
      nivel_medio: r.nivel_medio != null ? r.nivel_medio + ' ' + unitLbl : '—',
      pct_vs_dia: fmtPct(r.pct_vs_dia),
      pct_vs_semana: fmtPct(r.pct_vs_semana),
      pct_vs_mes: fmtPct(r.pct_vs_mes),
    }));
    els.dailyStats.innerHTML = fmtTable(display, [
      { key: 'date_local', label: 'Fecha' },
      { key: 'nivel_medio', label: isNivel ? 'Nivel medio' : 'Media diaria' },
      { key: 'pct_vs_dia', label: '% vs día ant.' },
      { key: 'pct_vs_semana', label: '% vs semana ant.' },
      { key: 'pct_vs_mes', label: '% vs mes ant.' },
    ], 'Sin datos');
  }

  function renderChartProfile(data) {
    const prof = data.variable_profile || {};
    if (prof.unit || data.unit) applyUnit(data.unit || prof.unit, prof);
    if (data.variable_profile && els.vTipo) {
      els.vTipo.textContent = data.variable_profile.label || data.variable_profile.type;
    }
  }

  function rangeData(arr, lo, hi) {
    return arr.filter((p) => p[lo] != null && p[hi] != null && !Number.isNaN(p[lo]) && !Number.isNaN(p[hi]))
      .map((p) => [p.x, p[lo], p[hi]]);
  }

  function lineData(arr, key) {
    return arr.filter((p) => p[key] != null && !Number.isNaN(p[key])).map((p) => [p.x, p[key]]);
  }

  const COL = { normal: '#ffffff', warn: '#f1c40f', orange: '#ff9800', alarm: '#e74c3c', rupture: '#ff1744', ignored: '#78909c' };

  function eventLevel(p) {
    if (p.anom_status === 'ignorado') return 'ignored';
    if (p.anom_status === 'rotura_inmediata' || p.rotura_inmediata) return 'rupture';
    if (p.anom_status === 'alarma' || p.anom_status === 'prealarma_roja' || p.prealarm_color === 'rojo') return 'alarm';
    if (p.anom_status === 'prealarma_naranja' || p.prealarm_color === 'naranja') return 'orange';
    if (p.anom_status === 'prealarma_amarilla' || p.prealarm_color === 'amarillo') return 'warn';
    if (p.anom_level >= 3) return 'alarm';
    if (p.anom_level >= 2) return 'orange';
    if (p.anom_status === 'advertencia' || p.anom_status === 'pre_alarma' || p.anom_level >= 1) return 'warn';
    return 'normal';
  }

  function eventColor(level) {
    if (level === 'rupture') return COL.rupture;
    if (level === 'alarm') return COL.alarm;
    if (level === 'orange') return COL.orange;
    if (level === 'warn') return COL.warn;
    if (level === 'ignored') return COL.ignored;
    return COL.normal;
  }

  function anomalyTooltipHtml(p, u) {
    if (!p || p.anom_status === 'ignorado') {
      return '<span style="color:#78909c">Dato ignorado (nulo, pegado o sin actualización)</span>';
    }
    const lines = [
      '<b>Valor:</b> ' + fmtNum(p.y, 2) + ' ' + u,
    ];
    if (p.l != null && p.h != null) {
      lines.push('<b>Rango normal L–H:</b> ' + fmtNum(p.l, 2) + ' – ' + fmtNum(p.h, 2));
    }
    if (p.ll != null && p.hh != null) {
      lines.push('<b>Alarma segura LL–HH:</b> ' + fmtNum(p.ll, 2) + ' – ' + fmtNum(p.hh, 2));
    }
    if (p.anom_limit) lines.push('<b>Límite superado:</b> ' + p.anom_limit);
    if (p.anom_pct) lines.push('<b>Percentil:</b> ' + p.anom_pct);
    if (p.anom_duration) lines.push('<b>Duración fuera:</b> ' + p.anom_duration + ' muestras');
    if (p.anom_rate != null) lines.push('<b>Razón de cambio:</b> ' + fmtNum(p.anom_rate, 3) + ' /paso');
    if (p.anom_confidence) lines.push('<b>Confianza:</b> ' + p.anom_confidence);
    if (p.prealarm_score != null) {
      lines.push('<b>Score prealarma:</b> ' + fmtNum(p.prealarm_score, 1) + '/100'
        + (p.prealarm_color ? ' (' + p.prealarm_color + ')' : ''));
      if (p.prealarm_pct != null) {
        lines.push('<small>30% pct ' + p.prealarm_pct + ' · 25% Δ ' + p.prealarm_rate
          + ' · 20% pers ' + p.prealarm_persist + ' · 15% tend ' + p.prealarm_trend
          + ' · 10% corr ' + p.prealarm_corr + '</small>');
      }
    }
    if (p.anom_msg) {
      const cls = p.anom_status === 'rotura_inmediata' ? 'color:#ff1744;font-weight:bold'
        : (p.anom_status === 'alarma' || p.anom_status === 'prealarma_roja') ? 'color:#e74c3c'
        : (p.anom_status === 'prealarma_naranja') ? 'color:#ff9800'
        : (p.anom_status === 'prealarma_amarilla' || p.anom_status === 'advertencia') ? 'color:#f1c40f' : 'color:#aaa';
      lines.push('<span style="' + cls + '">' + p.anom_msg + '</span>');
    }
    return lines.join('<br/>');
  }

  function realLineData(arr) {
    return arr.map((p) => {
      const level = eventLevel(p);
      const flagged = level !== 'normal';
      return {
        x: p.x,
        y: p.y,
        marker: {
          enabled: true,
          radius: level === 'rupture' ? 6 : (flagged ? 4.5 : 2.5),
          fillColor: eventColor(level),
          lineColor: level === 'rupture' ? '#fff' : '#111',
          lineWidth: level === 'rupture' ? 2 : 1,
          symbol: level === 'rupture' ? 'star' : 'circle',
        },
      };
    });
  }

  function seriesInRange(min, max) {
    return lastSeries.filter((p) => p.x >= min && p.x <= max);
  }

  function chartStatusSuffix(min, max) {
    const sel = seriesInRange(min, max);
    if (!sel.length) return '';
    const ys = sel.map((p) => p.y);
    const mean = ys.reduce((a, b) => a + b, 0) / ys.length;
    return ` · visible: ${sel.length} pts · media ${mean.toFixed(2)} ${unit}`;
  }

  function initRange() {
    if (!els.range) return;
    const u = new URL(location.href);
    const fini = u.searchParams.get('fini');
    if (fini && [...els.range.options].some((o) => o.value === fini)) {
      els.range.value = fini;
    } else {
      els.range.value = '*-14d';
    }
  }

  function initLhSigma() {
    if (!els.lhSigma || !els.lhSigmaSel) return;
    const u = new URL(location.href);
    const useSigma = u.searchParams.get('lh_sigma');
    if (useSigma === '1' || useSigma === '2' || useSigma === '3') {
      els.lhSigma.checked = true;
      els.lhSigmaSel.value = useSigma;
    } else {
      els.lhSigma.checked = false;
    }
    syncLhSigmaControls();
  }

  function lhSigmaValue() {
    if (!els.lhSigma || !els.lhSigma.checked) return 0;
    const n = parseInt(els.lhSigmaSel && els.lhSigmaSel.value, 10);
    return (n === 1 || n === 2 || n === 3) ? n : 0;
  }

  function syncLhSigmaControls() {
    if (!els.lhSigmaSel) return;
    const on = els.lhSigma && els.lhSigma.checked;
    els.lhSigmaSel.disabled = !on;
    updateLhLegendLabel(lhSigmaValue());
  }

  function updateLhLegendLabel(sigmaN) {
    if (!els.legendLhNormal) return;
    const sw = '<i class="sw lh-normal"></i> ';
    els.legendLhNormal.innerHTML = sigmaN
      ? sw + 'L–H normal (±' + sigmaN + 'σ)'
      : sw + 'L–H normal (p10/p90)';
  }

  function updateLhSigmaUrl() {
    const u = new URL(location.href);
    const n = lhSigmaValue();
    if (n) u.searchParams.set('lh_sigma', String(n));
    else u.searchParams.delete('lh_sigma');
    history.replaceState(null, '', u);
  }

  function initMa() {
    if (!els.ma) return;
    const u = new URL(location.href);
    const ma = u.searchParams.get('ma');
    if (ma && [...els.ma.options].some((o) => o.value === ma)) {
      els.ma.value = ma;
    } else {
      els.ma.value = '5';
    }
  }

  function updateRangeUrl() {
    const u = new URL(location.href);
    u.searchParams.set('fini', els.range.value);
    history.replaceState(null, '', u);
  }

  function resetPinnedView() {
    pinnedXExtremes = null;
    pendingChartRestore = null;
  }

  function rememberChartView() {
    if (!chart || !chart.xAxis || !chart.xAxis[0]) return;
    const ax = chart.xAxis[0];
    if (ax.min != null && ax.max != null) {
      pinnedXExtremes = { min: ax.min, max: ax.max };
    }
  }

  function restoreChartView(extremes) {
    if (!chart || !extremes) return;
    chart.xAxis[0].setExtremes(extremes.min, extremes.max, true, { trigger: 'preserveView' });
  }

  function applyDefaultView() {
    if (!chart || !lastSeries.length) return;
    applyChartZoom('2d');
  }

  function volumeBandData(bandSeries) {
    return (bandSeries || []).map((p) => [p.x, p.low, p.high]);
  }

  function fmtNum(v, dec) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    if (Number.isNaN(n)) return '—';
    return n.toFixed(dec == null ? 2 : dec);
  }

  function rebuildProjectionDetailFromModel(model, qin, meta) {
    if (!model) return [];
    const qinVal = Number(qin);
    if (Number.isNaN(qinVal)) return [];
    const lsToM3 = Number(model.ls_to_m3 || LS_TO_M3);
    const rows = [{
      kind: 'anchor',
      hora: model.anchor_hora || 'ancla',
      timestamp: model.anchor_timestamp || null,
      qin: qinVal,
      qout: null,
      delta_vol: null,
      volumen: Number(model.anchor_vol),
    }];
    let vol = Number(model.anchor_vol);
    (model.steps || []).forEach((step) => {
      const qout = Number(step.qout_ia || 0);
      const delta = (qinVal - qout) * lsToM3;
      vol = Math.max(0, vol + delta);
      rows.push({
        kind: 'step',
        hora: step.hora || '—',
        timestamp: null,
        qin: qinVal,
        qout: qout,
        delta_vol: Math.round(delta * 1000) / 1000,
        volumen: Math.round(vol * 1000) / 1000,
      });
    });
    return rows;
  }

  function qinTagsDisplay(t) {
    const parts = [];
    if (t.qin_point) parts.push(t.qin_point);
    if (t.qin2_point) parts.push(t.qin2_point);
    return parts.length ? parts.join(' + ') : '—';
  }

  function qoutTagDisplay(t) {
    if (t.qout_point) return t.qout_point;
    if (t.qout_label) return t.qout_label;
    return '—';
  }

  function renderTanksSummaryTable(tanks, currentPoint) {
    if (!els.tanksSummary) return;
    if (!tanks || !tanks.length) {
      els.tanksSummary.innerHTML = '<p class="empty">Sin estanques configurados</p>';
      return;
    }
    let html = '<table><thead><tr>'
      + '<th>Estanque</th><th>Point volumen</th><th>Point Qin</th><th>Point Qout</th>'
      + '<th class="num">Vol actual (m³)</th><th class="num">Qin (l/s)</th>'
      + '<th class="num">Proy +6h</th><th class="num">Proy +24h</th>'
      + '</tr></thead><tbody>';
    tanks.forEach((t) => {
      if (t.error) {
        html += '<tr><td>' + (t.recinto || t.point) + '</td><td colspan="7">' + t.error + '</td></tr>';
        return;
      }
      const isCurrent = t.current || t.point === currentPoint;
      html += '<tr class="' + (isCurrent ? 'row-current' : '') + '">'
        + '<td>' + (t.recinto || '—') + '</td>'
        + '<td>' + (t.vol_point || t.point) + '</td>'
        + '<td>' + qinTagsDisplay(t) + '</td>'
        + '<td>' + qoutTagDisplay(t) + '</td>'
        + '<td class="num">' + fmtNum(t.volumen_actual, 1) + '</td>'
        + '<td class="num">' + fmtNum(t.qin_used, 2) + '</td>'
        + '<td class="num">' + fmtNum(t.proy_6h, 1) + '</td>'
        + '<td class="num">' + fmtNum(t.proy_24h, 1) + '</td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    els.tanksSummary.innerHTML = html;
  }

  function renderProjectionStepsTable(volProj) {
    if (!els.projDetail || !volProj || !volProj.meta) {
      if (els.projDetail) els.projDetail.innerHTML = '<p class="empty">Sin proyección para este estanque</p>';
      return;
    }
    const m = volProj.meta || {};
    const rows = volProj.projection_detail || [];
    const qinHdr = m.qin_point ? ('Qin<br><small>' + m.qin_point + '</small>') : 'Qin (l/s)';
    const volHdr = m.vol_point ? ('Volumen<br><small>' + m.vol_point + '</small>') : 'Volumen (m³)';
    const qoutHdr = m.qout_label ? ('Qout<br><small>' + m.qout_label + '</small>') : 'Qout (l/s)';

    if (els.projCaption) {
      const parts = [
        (volProj.recinto || currentPoint()),
        'Qin fijo ' + fmtNum(m.qin_used, 2) + ' l/s',
        'Ancla ' + fmtNum(m.last_volume, 2) + ' m³',
      ];
      if (m.qin_mode) parts.push('modo ' + m.qin_mode);
      els.projCaption.textContent = parts.join(' · ');
    }

    if (!rows.length) {
      els.projDetail.innerHTML = '<p class="empty">Sin pasos de proyección</p>';
      return;
    }

    let html = '<table><thead><tr>'
      + '<th>Hora</th><th class="num">' + qinHdr + '</th><th class="num">' + qoutHdr + '</th>'
      + '<th class="num">ΔVol (m³)</th><th class="num">' + volHdr + '</th>'
      + '</tr></thead><tbody>';
    rows.forEach((row) => {
      const isAnchor = row.kind === 'anchor';
      const deltaCls = row.delta_vol > 0 ? 'delta-pos' : row.delta_vol < 0 ? 'delta-neg' : '';
      const rowCls = isAnchor ? 'row-anchor' : (row.volumen === 0 ? 'row-empty' : '');
      html += '<tr class="' + rowCls + '">'
        + '<td>' + (isAnchor ? (row.hora + ' (ancla)') : row.hora) + '</td>'
        + '<td class="num">' + fmtNum(row.qin, 2) + '</td>'
        + '<td class="num">' + (row.qout != null ? fmtNum(row.qout, 2) : '—') + '</td>'
        + '<td class="num ' + deltaCls + '">' + (row.delta_vol != null ? fmtNum(row.delta_vol, 2) : '—') + '</td>'
        + '<td class="num">' + fmtNum(row.volumen, 2) + '</td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    els.projDetail.innerHTML = html;
  }

  function closeProjectionModal() {
    if (els.projModal) els.projModal.classList.add('hidden');
    document.body.classList.remove('modal-open');
  }

  async function openProjectionModal() {
    if (!lastChartData || !lastChartData.volume_projection) {
      setStatus('Selecciona un tag de volumen con proyección demo_ia', true);
      return;
    }
    if (els.projModal) els.projModal.classList.remove('hidden');
    document.body.classList.add('modal-open');
    if (els.tanksSummary) els.tanksSummary.innerHTML = '<p class="empty">Cargando estanques…</p>';
    renderProjectionStepsTable(lastChartData.volume_projection);

    try {
      const params = chartQueryParams();
      params.delete('fini');
      params.delete('ma');
      const res = await fetch('/api/volume/tanks?' + params.toString());
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      const tanks = data.tanks || [];
      const current = currentPoint();
      const currentTank = tanks.find((t) => t.point === current);
      if (currentTank && currentTank.projection_detail) {
        lastChartData.volume_projection.projection_detail = currentTank.projection_detail;
        if (currentTank.meta) lastChartData.volume_projection.meta = currentTank.meta;
        renderProjectionStepsTable(lastChartData.volume_projection);
      }
      renderTanksSummaryTable(tanks, current);
    } catch (e) {
      if (els.tanksSummary) els.tanksSummary.innerHTML = '<p class="empty">' + e.message + '</p>';
    }
  }

  function syncVolumeToolbar(profile, volProj) {
    const isVol = profile && profile.type === 'volumen';
    if (els.volumeToolbar) els.volumeToolbar.classList.toggle('hidden', !isVol);
    if (els.btnProjTable) els.btnProjTable.classList.toggle('hidden', !(isVol && volProj && volProj.meta));
    els.legendVol.forEach((el) => el.classList.toggle('hidden', !isVol || !volProj));
    if (!isVol) {
      if (els.volumeMeta) els.volumeMeta.textContent = '';
      closeProjectionModal();
      return;
    }
    if (!volProj || !volProj.meta) {
      if (els.volumeMeta) els.volumeMeta.textContent = 'Sin perfil de consumo demo_ia para este estanque — agrega mapeo en config.json → volume_recintos';
      return;
    }
    const m = volProj.meta;
    if (els.qinManual && m.qin_actual != null && !els.qinManual.value) {
      els.qinManual.placeholder = String(m.qin_actual);
    }
    const parts = ['Recinto ' + (volProj.recinto || '—')];
    if (m.qin_point) {
      let qinLine = 'Qin ' + m.qin_point + ' = ' + (m.qin_actual != null ? m.qin_actual + ' l/s' : '—');
      if (m.qin_source === 'rt3') qinLine += ' (RT3 en vivo)';
      else if (m.qin_source === 'export') qinLine += ' (export demo_ia)';
      parts.push(qinLine);
    } else {
      parts.push('Qin actual ' + (m.qin_actual != null ? m.qin_actual + ' l/s' : '—'));
    }
    if (m.qout_label) {
      parts.push('Qout ' + m.qout_label);
    }
    if (m.qin_export != null) {
      parts.push('export IA tenía ' + m.qin_export + ' l/s' + (m.qin_export_fecha ? ' (' + m.qin_export_fecha + ')' : ''));
    }
    parts.push('Qin ideal ' + (m.qin_ideal != null ? m.qin_ideal + ' l/s' : '—'));
    parts.push('Usando ' + (m.qin_used != null ? m.qin_used + ' l/s' : '—'));
    if (m.volumen_banda_min != null && m.volumen_banda_max != null) {
      parts.push('Banda config ' + m.volumen_banda_min + '–' + m.volumen_banda_max + ' m³');
    }
    if (m.volume_band_pct != null) {
      parts.push('Banda ±' + Math.round(m.volume_band_pct * 100) + '% vs ideal');
    }
    if (els.volumeMeta) els.volumeMeta.textContent = parts.join(' · ');
    if (els.projModal && !els.projModal.classList.contains('hidden') && volProj) {
      renderProjectionStepsTable(volProj);
    }
  }

  function syncQinManualInput() {
    if (!els.qinMode || !els.qinManual) return;
    const manual = els.qinMode.value === 'manual';
    els.qinManual.disabled = !manual;
    if (!manual) els.qinManual.value = '';
  }

  const PROJ_SERIES_ID = 'vol-projection-used';
  const LS_TO_M3 = 0.9;

  function simulateProjectionFromModel(model, qin) {
    if (!model || !model.steps || !model.steps.length) return [];
    const qinVal = Number(qin);
    if (Number.isNaN(qinVal)) return [];
    let vol = Number(model.anchor_vol);
    const out = [{ x: model.anchor_x, y: vol }];
    model.steps.forEach((step) => {
      vol = Math.max(0, vol + (qinVal - Number(step.qout_ia || 0)) * (model.ls_to_m3 || LS_TO_M3));
      out.push({ x: step.x, y: Math.round(vol * 1000) / 1000 });
    });
    return out;
  }

  function manualQinValue() {
    if (!els.qinManual || els.qinManual.value === '') return null;
    const qin = parseFloat(els.qinManual.value);
    return Number.isNaN(qin) ? null : qin;
  }

  function updateManualProjectionLive() {
    if (!chart || !lastChartData || !lastChartData.volume_projection) return;
    if (!els.qinMode || els.qinMode.value !== 'manual') return;
    const qin = manualQinValue();
    if (qin == null) return;

    const model = lastChartData.volume_projection.projection_model;
    const seriesData = simulateProjectionFromModel(model, qin);
    if (seriesData.length < 2) return;

    const hs = chart.get(PROJ_SERIES_ID);
    const seriesName = 'Proyección (' + qin + ' l/s)';
    if (hs) {
      hs.update({ name: seriesName, data: seriesData }, false);
      chart.redraw(false);
    }

    const meta = lastChartData.volume_projection.meta || {};
    meta.qin_used = qin;
    meta.qin_mode = 'manual';
    lastChartData.volume_projection.projection_series = seriesData;
    lastChartData.volume_projection.projection_detail = rebuildProjectionDetailFromModel(model, qin, meta);
    syncVolumeToolbar(lastChartData.variable_profile, lastChartData.volume_projection);
  }

  function scheduleManualProjectionUpdate() {
    if (!els.qinMode || els.qinMode.value !== 'manual') return;
    updateManualProjectionLive();
  }

  function isVolumePoint(tag) {
    const p = (tag || '').toLowerCase();
    return p.includes('volumen') || p.endsWith('.vol');
  }

  function chartQueryParams() {
    const params = new URLSearchParams({
      point: currentPoint(),
      fini: els.range.value,
      ma: els.ma.value,
    });
    if (isVolumePoint(currentPoint()) && els.qinMode) {
      params.set('qin_mode', els.qinMode.value);
      if (els.qinMode.value === 'manual' && els.qinManual && els.qinManual.value !== '') {
        params.set('qin', els.qinManual.value);
      }
    }
    const sigmaN = lhSigmaValue();
    if (sigmaN) params.set('lh_sigma', String(sigmaN));
    return params;
  }

  function syncZoomButtons(active) {
    els.zoomBtns.forEach((b) => b.classList.toggle('active', b.dataset.zoom === (active || 'all')));
  }

  function chartTimeMax() {
    if (!lastSeries.length) return Date.now();
    return Math.max(...lastSeries.map((p) => p.x), Date.now());
  }

  function applyChartZoom(key) {
    if (!chart || !lastSeries.length) return;
    syncZoomButtons(key);
    if (key === 'all') {
      chart.xAxis[0].setExtremes(null, null, true);
      rememberChartView();
      return;
    }
    const days = { '1d': 1, '2d': 2, '1w': 7, '2w': 14, '1m': 30, '3m': 90, '6m': 180, '1y': 365 }[key] || 2;
    const max = chartTimeMax();
    chart.xAxis[0].setExtremes(max - days * 86400000, max, true);
    rememberChartView();
  }

  function syncPctLegend() {
    const hide = els.hidePct && els.hidePct.checked;
    els.legendPct.forEach((el) => el.classList.toggle('hidden', hide));
  }

  function syncLhLegend() {
    const hide = els.hideLh && els.hideLh.checked;
    els.legendLh.forEach((el) => el.classList.toggle('hidden', hide));
  }

  function syncSigmaLegend() {
    const show = els.showSigma && els.showSigma.checked;
    els.legendSigma.forEach((el) => el.classList.toggle('hidden', !show));
  }

  const PAT_STYLE = {
    trend_up: { color: '#ab47bc', symbol: 'triangle', label: 'Ascendente' },
    trend_down: { color: '#7e57c2', symbol: 'triangle-down', label: 'Descendente' },
    stuck: { color: '#78909c', symbol: 'square', label: 'Pegado' },
    ooc_high: { color: '#e040fb', symbol: 'diamond', label: 'Fuera +3σ' },
    ooc_low: { color: '#e040fb', symbol: 'diamond', label: 'Fuera −3σ' },
    run_above: { color: '#ba68c8', symbol: 'circle', label: 'Sobre CL' },
    run_below: { color: '#9575cd', symbol: 'circle', label: 'Bajo CL' },
  };

  function renderSixSigmaPanel(ss) {
    if (!els.sixsigmaPanel) return;
    const recent = (ss && ss.recent) || [];
    if (!recent.length) {
      els.sixsigmaPanel.innerHTML = '<p class="empty">Sin patrones detectados</p>';
      return;
    }
    els.sixsigmaPanel.innerHTML = fmtTable(
      recent.slice().reverse().map((r) => ({
        time: (r.time || '').replace('T', ' ').slice(0, 16),
        tipo: (PAT_STYLE[r.type] || {}).label || r.type,
        valor: r.y,
        detalle: r.label || '—',
      })),
      [
        { key: 'time', label: 'Hora' },
        { key: 'tipo', label: 'Patrón' },
        { key: 'valor', label: 'Valor' },
        { key: 'detalle', label: 'Detalle' },
      ],
      'Sin patrones'
    );
  }

  function nowPlotLine() {
    const now = Date.now();
    const label = new Date(now).toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit', hour12: false });
    return [{
      value: now,
      color: '#c62828',
      width: 2,
      zIndex: 6,
      label: {
        text: 'Ahora ' + label,
        style: { color: '#ef5350', fontWeight: '600', fontSize: '11px' },
        align: 'left',
        rotation: 0,
        x: 4,
        y: 14,
      },
    }];
  }

  function xAxisPlotLines(series) {
    return midnightPlotLines(series).concat(nowPlotLine());
  }

  function midnightPlotLines(series) {
    if (!series || !series.length) return [];
    const min = Math.min(...series.map((p) => p.x));
    const max = Math.max(...series.map((p) => p.x));
    const lines = [];
    const cur = new Date(min);
    cur.setHours(0, 0, 0, 0);
    while (cur.getTime() <= max) {
      lines.push({
        value: cur.getTime(),
        color: 'rgba(160,160,160,0.55)',
        width: 1,
        zIndex: 1,
      });
      cur.setDate(cur.getDate() + 1);
    }
    return lines;
  }

  function clearEmptyChart(data) {
    if (chart) {
      chart.destroy();
      chart = null;
    }
    lastSeries = [];
    lastChartData = data || null;
    syncVolumeToolbar(data && data.variable_profile, null);
    if (els.dailyStats) els.dailyStats.innerHTML = '<p class="empty">Sin datos en el rango seleccionado</p>';
    if (els.sixsigmaPanel) els.sixsigmaPanel.innerHTML = '<p class="empty">Sin datos</p>';
  }

  function buildChart(data, chartOpts) {
    chartOpts = chartOpts || {};
    const keepView = chartOpts.keepView === true;
    if (keepView) rememberChartView();
    const savedView = keepView ? pinnedXExtremes : null;
    pendingChartRestore = savedView ? { min: savedView.min, max: savedView.max } : null;
    if (data.empty) {
      clearEmptyChart(data);
      setStatus(data.message || 'Sin datos', false);
      return;
    }
    const s = data.series || [];
    const prof = data.variable_profile || {};
    applyUnit(data.unit || prof.unit, prof);
    lastSeries = s;
    lastChartData = data;
    chartMeta = { point: data.point || currentPoint(), count: data.count || s.length };
    const hidePct = els.hidePct && els.hidePct.checked;
    const hideLh = els.hideLh && els.hideLh.checked;
    const showSigma = els.showSigma && els.showSigma.checked;
    const sigmaLh = lhSigmaValue();
    updateLhLegendLabel(data.lh_sigma || sigmaLh);
    const volProj = data.volume_projection;
    const isVolumeChart = (prof.type === 'volumen') && volProj;

    const series = [];
    if (!isVolumeChart && !hideLh) {
      const lhLabel = sigmaLh ? ('L–H (±' + sigmaLh + 'σ)') : 'L–H (normal)';
      series.push(
        { name: 'LL–L (adv. baja)', type: 'arearange', data: rangeData(s, 'll', 'l'), color: '#f1c40f', fillOpacity: 0.12, lineWidth: 0, zIndex: 0, enableMouseTracking: false },
        { name: lhLabel, type: 'arearange', data: rangeData(s, 'l', 'h'), color: '#4caf82', fillOpacity: 0.28, lineWidth: 1, zIndex: 1,
          lineColor: '#4caf82', marker: { enabled: false } },
        { name: 'H–HH (adv. alta)', type: 'arearange', data: rangeData(s, 'h', 'hh'), color: '#f1c40f', fillOpacity: 0.12, lineWidth: 0, zIndex: 0, enableMouseTracking: false },
      );
    }
    if (showSigma) {
      series.push(
        { name: '±3σ', type: 'arearange', data: rangeData(s, 's3_lo', 's3_hi'), color: '#ce93d8', fillOpacity: 0.12, lineWidth: 1, zIndex: 0, dashStyle: 'Dash' },
        { name: '±2σ', type: 'arearange', data: rangeData(s, 's2_lo', 's2_hi'), color: '#ba68c8', fillOpacity: 0.08, zIndex: 0 },
        { name: 'CL', type: 'line', data: lineData(s, 'cl'), color: '#ce93d8', lineWidth: 1.5, dashStyle: 'Dot', marker: { enabled: false }, zIndex: 1 },
      );
    }
    if (!hidePct && !isVolumeChart) {
      series.push(
        { name: 'P05–P20', type: 'arearange', data: rangeData(s, 'p05', 'p20'), color: '#b0b0b0', fillOpacity: 0.35, zIndex: 0 },
        { name: 'P20–P80', type: 'arearange', data: rangeData(s, 'p20', 'p80'), color: '#707070', fillOpacity: 0.55, zIndex: 1 },
        { name: 'P80–P95', type: 'arearange', data: rangeData(s, 'p80', 'p95'), color: '#b0b0b0', fillOpacity: 0.35, zIndex: 0 },
        { name: 'P50', type: 'line', data: lineData(s, 'p50'), color: '#9e9e9e', dashStyle: 'ShortDash', lineWidth: 1, marker: { enabled: false }, zIndex: 2 },
      );
    }
    if (isVolumeChart && volProj.ideal_band_series && volProj.ideal_band_series.length) {
      series.push({
        name: 'Banda ideal ±10%',
        type: 'arearange',
        data: volumeBandData(volProj.ideal_band_series),
        color: '#81c784',
        fillColor: 'rgba(129,199,132,0.35)',
        fillOpacity: 0.35,
        lineWidth: 1,
        lineColor: 'rgba(129,199,132,0.7)',
        zIndex: 1,
        enableMouseTracking: true,
      });
    }
    if (isVolumeChart && volProj.ideal_band_projection_series && volProj.ideal_band_projection_series.length) {
      series.push({
        name: 'Banda ideal ±10% (proy.)',
        type: 'arearange',
        data: volumeBandData(volProj.ideal_band_projection_series),
        color: '#4fc3f7',
        fillColor: 'rgba(79,195,247,0.35)',
        fillOpacity: 0.35,
        lineWidth: 1,
        lineColor: 'rgba(79,195,247,0.8)',
        zIndex: 1,
        enableMouseTracking: true,
      });
    }
    if (isVolumeChart && volProj.ideal_series && volProj.ideal_series.length) {
      series.push({
        name: 'Volumen ideal',
        type: 'line',
        data: volProj.ideal_series,
        color: '#66bb6a',
        dashStyle: 'Dash',
        lineWidth: 1.5,
        marker: { enabled: false },
        zIndex: 2,
      });
    }
    if (isVolumeChart && volProj.ideal_projection_series && volProj.ideal_projection_series.length > 1) {
      series.push({
        name: 'Volumen ideal (proy.)',
        type: 'line',
        data: volProj.ideal_projection_series,
        color: '#4fc3f7',
        dashStyle: 'Dash',
        lineWidth: 1.5,
        marker: { enabled: false },
        zIndex: 2,
      });
    }
    series.push({ name: seriesValueName(prof), type: 'line', data: realLineData(s), color: '#ffffff', lineWidth: 2, zIndex: 4 });
    if (data.ruptures && data.ruptures.length) {
      series.push({ name: 'Rotura', type: 'scatter', data: data.ruptures, color: '#ef5350', marker: { radius: 5, symbol: 'triangle' }, zIndex: 4 });
    }
    if (data.pre_ruptures && data.pre_ruptures.length) {
      series.push({ name: 'Pre-rotura', type: 'scatter', data: data.pre_ruptures, color: '#ff9800', marker: { radius: 4, symbol: 'circle' }, zIndex: 4 });
    }
    if (showSigma && data.sixsigma && data.sixsigma.markers) {
      Object.keys(data.sixsigma.markers).forEach((key) => {
        const pts = data.sixsigma.markers[key];
        if (!pts || !pts.length) return;
        const st = PAT_STYLE[key] || { color: '#ce93d8', symbol: 'circle', label: key };
        series.push({
          name: st.label,
          type: 'scatter',
          data: pts,
          color: st.color,
          marker: { radius: 5, symbol: st.symbol },
          zIndex: 5,
        });
      });
    }

    if (volProj) {
      if (volProj.projection_series && volProj.projection_series.length > 1) {
        series.push({
          id: PROJ_SERIES_ID,
          name: 'Proyección (' + ((volProj.meta && volProj.meta.qin_used) || '?') + ' l/s)',
          type: 'line',
          data: volProj.projection_series,
          color: '#ffb74d',
          dashStyle: 'ShortDash',
          lineWidth: 2,
          marker: { enabled: true, radius: 2 },
          zIndex: 6,
        });
      }
      if (volProj.projection_ideal_series && volProj.projection_ideal_series.length > 1) {
        series.push({
          name: 'Proyección qin ideal',
          type: 'line',
          data: volProj.projection_ideal_series,
          color: '#ffeb3b',
          dashStyle: 'ShortDash',
          lineWidth: 1.5,
          marker: { enabled: false },
          zIndex: 6,
        });
      }
    }

    const opts = {
      chart: {
        backgroundColor: '#252525',
        zoomType: 'x',
        panning: { enabled: true, type: 'x' },
        panKey: 'shift',
        style: { fontFamily: 'Segoe UI, system-ui, sans-serif' },
        events: {
          load() {
            if (pendingChartRestore) {
              restoreChartView(pendingChartRestore);
              pendingChartRestore = null;
            }
          },
        },
      },
      accessibility: { enabled: false },
      rangeSelector: { enabled: false },
      navigator: {
        enabled: true,
        outlineColor: '#555',
        maskFill: 'rgba(108,158,255,0.15)',
        series: { color: '#666', lineColor: '#888' },
      },
      scrollbar: { enabled: true },
      title: { text: null },
      credits: { enabled: false },
      xAxis: {
        type: 'datetime',
        min: savedView ? savedView.min : undefined,
        max: savedView ? savedView.max : undefined,
        lineColor: '#555',
        tickColor: '#555',
        labels: { style: { color: '#aaa' } },
        title: { text: 'Tiempo', style: { color: '#aaa' } },
        plotLines: xAxisPlotLines(s),
        events: {
          afterSetExtremes(e) {
            if (e.trigger === 'syncExtremes' || e.trigger === 'preserveView') return;
            if (e.min != null && e.max != null) {
              pinnedXExtremes = { min: e.min, max: e.max };
            }
            const msg = chartMeta.point + ' · ' + chartMeta.count + ' pts · ma=' + els.ma.value + ' min';
            if (e.min != null && e.max != null) {
              setStatus(msg + chartStatusSuffix(e.min, e.max), false);
            }
          },
        },
      },
      yAxis: {
        title: { text: unit, style: { color: '#aaa' } },
        gridLineColor: '#3a3a3a',
        labels: { style: { color: '#aaa' } },
      },
      tooltip: {
        shared: false,
        useHTML: true,
        backgroundColor: 'rgba(30,30,30,0.95)',
        borderColor: '#555',
        style: { color: '#eee' },
        xDateFormat: '%Y-%m-%d %H:%M',
        valueDecimals: 2,
        formatter: function () {
          const pt = this.point || {};
          const raw = pt.options || pt;
          const x = raw.x != null ? raw.x : (pt.x != null ? pt.x : this.x);
          const p = s.find((row) => row.x === x) || raw;
          const yVal = p.y != null ? p.y : pt.y;
          const hdr = '<b>' + Highcharts.dateFormat('%Y-%m-%d %H:%M', x) + '</b><br/>';
          if (this.series && this.series.name !== seriesValueName(prof)) {
            return hdr + this.series.name + ': <b>' + fmtNum(this.y, 2) + '</b> ' + unit;
          }
          return hdr + anomalyTooltipHtml(Object.assign({}, p, { y: yVal }), unit);
        },
      },
      legend: { enabled: true, itemStyle: { color: '#e8ecf4' } },
      plotOptions: {
        series: { animation: false, states: { inactive: { opacity: 0.85 } } },
        line: { lineWidth: 2, marker: { enabled: true, radius: 2 } },
        arearange: { enableMouseTracking: false, lineWidth: 0 },
      },
      series,
    };

    if (chart) {
      chart.destroy();
      chart = null;
    }
    chart = Highcharts.stockChart(els.chart, opts);
    if (savedView) {
      restoreChartView(savedView);
      pendingChartRestore = null;
    } else if (!keepView) {
      applyDefaultView();
    }
    syncPctLegend();
    syncLhLegend();
    syncSigmaLegend();
    renderSixSigmaPanel(data.sixsigma);
    renderChartProfile(data);
    renderDailyStats(data.daily_stats || [], prof, unit);
    syncVolumeToolbar(prof, volProj);
    const ssSum = data.sixsigma && data.sixsigma.summary ? Object.entries(data.sixsigma.summary).map(([k, v]) => k + ':' + v).join(' ') : '';
    const spanDays = s.length > 1 ? ((s[s.length - 1].x - s[0].x) / 86400000).toFixed(0) : 0;
    setStatus(chartMeta.point + ' · ' + chartMeta.count + ' pts · ' + spanDays + 'd · ' + els.range.value + ' · ma=' + els.ma.value + ' min' + (ssSum ? ' · σ ' + ssSum : ''), false);
  }

  async function loadChart(chartOpts) {
    const tag = currentPoint();
    const r = await fetch('/api/chart?' + chartQueryParams().toString());
    const data = await r.json();
    if (!r.ok && !data.empty) throw new Error(data.error || ('HTTP ' + r.status));
    data.point = tag;
    buildChart(data, chartOpts);
  }

  async function loadReport() {
    const tag = currentPoint();
    updateUrl(tag);
    const data = await fetch('/api/report?point=' + encodeURIComponent(tag)).then((r) => r.json());
    if (data.report) {
      renderReport(data.report);
    } else {
      clearReport();
      setStatus('Sin reporte guardado — pulsa Analizar GPU', false);
    }
  }

  async function loadHistory() {
    const data = await fetch('/api/reports').then((r) => r.json());
    const rows = data.reports || [];
    if (!rows.length) { els.history.innerHTML = '<p class="empty">Ninguno aún</p>'; return; }
    let h = '<table><thead><tr><th>Point</th><th>Estado</th><th>Prob</th><th>Fecha</th></tr></thead><tbody>';
    rows.forEach((r) => {
      h += '<tr class="clickable" data-point="' + r.point + '"><td>' + r.point + '</td><td>' + (r.estado || '—') + '</td><td>' + (r.prob != null ? (r.prob * 100).toFixed(0) + '%' : '—') + '</td><td>' + (r.generated_at || '').slice(0, 16) + '</td></tr>';
    });
    els.history.innerHTML = h + '</tbody></table>';
    els.history.querySelectorAll('tr.clickable').forEach((tr) => {
      tr.addEventListener('click', () => { els.pointSel.value = tr.dataset.point; point = tr.dataset.point; refreshAll(); });
    });
  }

  async function runAnalyze() {
    els.analyze.disabled = true;
    setStatus('Analizando con XGBoost GPU…', false);
    try {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ point: currentPoint(), fini: els.range.value, ma: parseInt(els.ma.value, 10) }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Error');
      renderReport(data.report);
      await loadChart();
      await loadHistory();
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.analyze.disabled = false;
    }
  }

  async function refreshAll() {
    resetPinnedView();
    setStatus('Cargando…', false);
    try {
      await loadChart();
      await loadReport();
      await loadHistory();
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  let searchTimer;
  els.pointSearch.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadPoints(els.pointSearch.value.trim()), 300);
  });
  els.pointSel.addEventListener('change', () => { resetPinnedView(); point = els.pointSel.value; refreshAll(); });
  els.range.addEventListener('change', () => { resetPinnedView(); updateRangeUrl(); refreshAll(); });
  if (els.qinMode) {
    els.qinMode.addEventListener('change', () => {
      syncQinManualInput();
      if (els.qinMode.value === 'manual') {
        const m = lastChartData && lastChartData.volume_projection && lastChartData.volume_projection.meta;
        if (m && m.qin_actual != null && els.qinManual && !els.qinManual.value) {
          els.qinManual.value = String(m.qin_actual);
        }
        scheduleManualProjectionUpdate();
      } else {
        loadChart({ keepView: true }).catch((e) => setStatus(e.message, true));
      }
    });
  }
  if (els.qinApply) {
    els.qinApply.addEventListener('click', () => loadChart({ keepView: true }).catch((e) => setStatus(e.message, true)));
  }
  if (els.qinManual) {
    els.qinManual.addEventListener('input', scheduleManualProjectionUpdate);
    els.qinManual.addEventListener('change', scheduleManualProjectionUpdate);
    els.qinManual.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') scheduleManualProjectionUpdate();
    });
  }
  if (els.btnProjTable) {
    els.btnProjTable.addEventListener('click', () => {
      openProjectionModal().catch((e) => setStatus(e.message, true));
    });
  }
  if (els.projModalClose) els.projModalClose.addEventListener('click', closeProjectionModal);
  if (els.projBackdrop) els.projBackdrop.addEventListener('click', closeProjectionModal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeProjectionModal();
  });
  els.analyze.addEventListener('click', runAnalyze);
  els.refresh.addEventListener('click', refreshAll);
  els.zoomBtns.forEach((btn) => {
    btn.addEventListener('click', () => applyChartZoom(btn.dataset.zoom));
  });
  if (els.hidePct) {
    els.hidePct.addEventListener('change', () => {
      syncPctLegend();
      if (lastChartData) buildChart(lastChartData, { keepView: true });
    });
  }
  if (els.hideLh) {
    els.hideLh.addEventListener('change', () => {
      syncLhLegend();
      if (lastChartData) buildChart(lastChartData, { keepView: true });
    });
  }
  if (els.showSigma) {
    els.showSigma.addEventListener('change', () => {
      syncSigmaLegend();
      if (lastChartData) buildChart(lastChartData, { keepView: true });
    });
  }
  if (els.lhSigma) {
    els.lhSigma.addEventListener('change', () => {
      syncLhSigmaControls();
      updateLhSigmaUrl();
      loadChart({ keepView: true }).catch((e) => setStatus(e.message, true));
    });
  }
  if (els.lhSigmaSel) {
    els.lhSigmaSel.addEventListener('change', () => {
      if (!els.lhSigma || !els.lhSigma.checked) return;
      updateLhSigmaUrl();
      loadChart({ keepView: true }).catch((e) => setStatus(e.message, true));
    });
  }

  initRange();
  initMa();
  initLhSigma();
  updateRangeUrl();
  syncQinManualInput();
  loadPoints('').then(refreshAll);
})();
