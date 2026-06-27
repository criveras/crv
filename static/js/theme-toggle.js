(function () {
  'use strict';

  const KEY = 'gpu_tag_theme';
  const DEFAULT_THEME = 'light';

  function injectThemeStyles() {
    if (document.getElementById('theme-toggle-styles')) return;
    const style = document.createElement('style');
    style.id = 'theme-toggle-styles';
    style.textContent = `
      .topbar-actions { display: flex; align-items: center; gap: 10px; }
      .btn-theme { min-width: 104px; }
      body.theme-dark {
        --bg: #1a1f2e; --bg2: #252b3d; --border: #3a4258; --text: #e8ecf4; --muted: #9aa3b8;
        --accent: #6c9eff; --ok: #4caf82; --warn: #e6a23c; --alarm: #ef5350;
      }
      body.theme-dark .btn { background: var(--bg2); border-color: var(--border); color: var(--text); }
      body.theme-dark .btn:hover { background: #2f3650; }
      body.theme-dark .btn-primary { background: #2a4070; border-color: var(--accent); color: #fff; }
      body.theme-dark .btn-z:hover,
      body.theme-dark .btn-z.active { background: #2f3650; }
      body.theme-dark .badge-ok { background: #1e3d32; color: var(--ok); }
      body.theme-dark .badge-vigilancia { background: #3d3520; color: var(--warn); }
      body.theme-dark .badge-pre_alarma { background: #4a3020; color: #ff9800; }
      body.theme-dark .badge-alarma { background: #4a2020; color: var(--alarm); }
      body.theme-dark .card,
      body.theme-dark .panel { box-shadow: none; }
      body.theme-dark .risk-tag { background: #2f3650; border-color: var(--border); color: var(--muted); }
      body.theme-dark .risk-tag.alarm { background: #4a2020; color: var(--alarm); border-color: #633030; }
      body.theme-dark tr.clickable:hover { background: #2f3650; }
      body.theme-dark .modal-backdrop { background: rgba(0, 0, 0, 0.65); }
      body.theme-dark .modal-box { box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45); }
      body.theme-dark .chart { background: #252525; }
      body.theme-dark .sw.real { background: #fff; }
      body.theme-dark .highcharts-background,
      body.theme-dark .highcharts-plot-background { fill: #252525 !important; }
      body.theme-dark .highcharts-grid-line { stroke: #3a3a3a !important; }
      body.theme-dark .highcharts-axis-line,
      body.theme-dark .highcharts-tick { stroke: #555 !important; }
      body.theme-dark .highcharts-axis-labels text,
      body.theme-dark .highcharts-axis-title,
      body.theme-dark .highcharts-legend-item text { fill: #e8ecf4 !important; color: #e8ecf4 !important; }
      body.theme-dark .highcharts-tooltip-box { fill: rgba(30,30,30,0.95) !important; stroke: #555 !important; }
      body.theme-dark .highcharts-tooltip text { fill: #eee !important; color: #eee !important; }
      @media (max-width: 640px) {
        .topbar { align-items: flex-start; }
        .topbar-actions { flex-wrap: wrap; justify-content: flex-end; }
      }
    `;
    document.head.appendChild(style);
  }

  function currentTheme() {
    const saved = localStorage.getItem(KEY);
    return saved === 'dark' || saved === 'light' ? saved : DEFAULT_THEME;
  }

  function applyTheme(theme) {
    document.body.classList.toggle('theme-dark', theme === 'dark');
    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
      btn.textContent = theme === 'dark' ? 'Tema claro' : 'Tema oscuro';
      btn.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
      btn.title = theme === 'dark' ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro';
    }
  }

  function createButton() {
    const topbar = document.querySelector('.topbar');
    const badge = document.getElementById('badge');
    if (!topbar || document.getElementById('btn-theme-toggle')) return;

    const actions = document.createElement('div');
    actions.className = 'topbar-actions';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'btn-theme-toggle';
    btn.className = 'btn btn-theme';
    btn.addEventListener('click', function () {
      const next = document.body.classList.contains('theme-dark') ? 'light' : 'dark';
      localStorage.setItem(KEY, next);
      applyTheme(next);
    });

    if (badge && badge.parentNode === topbar) {
      topbar.removeChild(badge);
      actions.appendChild(btn);
      actions.appendChild(badge);
      topbar.appendChild(actions);
    } else {
      actions.appendChild(btn);
      topbar.appendChild(actions);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    injectThemeStyles();
    createButton();
    applyTheme(currentTheme());
  });
})();
