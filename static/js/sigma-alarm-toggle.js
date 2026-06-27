(function () {
  'use strict';

  var key = 'gpu_tag_sigma_alarm';

  function isOn() {
    var saved = localStorage.getItem(key);
    if (saved === '1') return true;
    if (saved === '0') return false;
    return new URL(location.href).searchParams.get('sigma_alarm') === '1';
  }

  function save(on) {
    localStorage.setItem(key, on ? '1' : '0');
    var u = new URL(location.href);
    if (on) u.searchParams.set('sigma_alarm', '1');
    else u.searchParams.delete('sigma_alarm');
    history.replaceState(null, '', u);
  }

  function addParamToApiChart(urlText) {
    var u = new URL(urlText, location.origin);
    if (u.pathname === '/api/chart' && isOn()) {
      u.searchParams.set('sigma_alarm', '1');
    }
    if (u.origin === location.origin) return u.pathname + u.search + u.hash;
    return u.toString();
  }

  var oldFetch = window.fetch.bind(window);
  window.fetch = function (resource, options) {
    if (typeof resource === 'string') {
      resource = addParamToApiChart(resource);
    }
    return oldFetch(resource, options);
  };

  function makeCheck() {
    var toolbar = document.querySelector('.chart-toolbar');
    if (!toolbar || document.getElementById('chk-sigma-alarm')) return;

    var label = document.createElement('label');
    label.className = 'chk sigma-alarm-ctl';

    var chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.id = 'chk-sigma-alarm';
    chk.checked = isOn();

    label.appendChild(chk);
    label.appendChild(document.createTextNode(' LL/HH +/-3 sigma patron'));
    toolbar.appendChild(label);

    chk.addEventListener('change', function () {
      save(chk.checked);
      var b = document.getElementById('btn-refresh');
      if (b) b.click();
    });

    save(chk.checked);
  }

  document.addEventListener('DOMContentLoaded', makeCheck);
})();
