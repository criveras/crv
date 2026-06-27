(function () {
  'use strict';

  const KEY = 'gpu_tag_theme';
  const DEFAULT_THEME = 'light';

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
    createButton();
    applyTheme(currentTheme());
  });
})();
