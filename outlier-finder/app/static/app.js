// Feedback layer: page-load bar, busy buttons, toasts, and live progress for background jobs.
// Everything degrades to plain links and forms without JS (pages carry <noscript> refreshes).
(function () {
  'use strict';

  var ICONS = {
    ok: '<path d="M20 6 9 17l-5-5"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    error: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'
  };
  function svg(name) {
    return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>';
  }

  // ---- toasts --------------------------------------------------------------------------
  var region = document.getElementById('toasts');
  var TTL = { ok: 5000, info: 6000 };  // errors stay until dismissed

  function dismiss(t) {
    if (!t || t.dataset.leaving) return;
    t.dataset.leaving = '1';
    t.classList.add('leaving');
    setTimeout(function () { t.remove(); }, 200);
  }

  function arm(t) {
    var kind = ['error', 'info', 'ok'].filter(function (k) { return t.classList.contains(k); })[0] || 'ok';
    t.querySelector('.toast-close').addEventListener('click', function () { dismiss(t); });
    if (!TTL[kind]) return;
    // Pause the timer while hovered or while the tab is hidden, so nobody misses a message.
    var left = TTL[kind], started = Date.now(), timer;
    function run() { started = Date.now(); timer = setTimeout(function () { dismiss(t); }, left); }
    function pause() { clearTimeout(timer); left -= Date.now() - started; }
    t.addEventListener('mouseenter', pause);
    t.addEventListener('mouseleave', run);
    document.addEventListener('visibilitychange', function () { document.hidden ? pause() : run(); });
    run();
  }

  function toast(msg, kind) {
    kind = kind || 'ok';
    var t = document.createElement('div');
    t.className = 'toast ' + kind;
    t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    t.innerHTML = svg(kind) + '<p></p><button type="button" class="toast-close" aria-label="Dismiss">' + svg('x') + '</button>';
    t.querySelector('p').textContent = msg;
    region.appendChild(t);
    arm(t);
    return t;
  }
  window.toast = toast;

  document.querySelectorAll('[data-server-toast]').forEach(arm);

  // A message queued before a reload (e.g. "Breakdown ready") shows on the fresh page.
  function toastAfterReload(msg, kind) {
    try { sessionStorage.setItem('pending-toast', JSON.stringify({ msg: msg, kind: kind })); } catch (e) { /* private mode */ }
    location.reload();
  }
  try {
    var pending = sessionStorage.getItem('pending-toast');
    if (pending) { sessionStorage.removeItem('pending-toast'); pending = JSON.parse(pending); toast(pending.msg, pending.kind); }
  } catch (e) { /* storage unavailable */ }

  // ---- page-load bar -------------------------------------------------------------------
  // Shown for navigations and form posts that take longer than a blink.
  var bar = document.getElementById('loadbar');
  var barDelay;
  function startBar() {
    clearTimeout(barDelay);
    barDelay = setTimeout(function () { bar.classList.add('active'); }, 120);
  }
  function stopBar() {
    clearTimeout(barDelay);
    bar.classList.remove('active');
  }
  window.addEventListener('pageshow', function () {  // back/forward cache restores the old page state
    stopBar();
    document.querySelectorAll('.is-busy').forEach(unbusy);
  });

  function busy(btn) {
    if (!btn || btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.setAttribute('aria-busy', 'true');
    // Disable after the submit has captured the button's name/value.
    setTimeout(function () { btn.disabled = true; }, 0);
  }
  function unbusy(btn) {
    btn.classList.remove('is-busy');
    btn.removeAttribute('aria-busy');
    btn.disabled = false;
  }

  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;  // e.g. a cancelled confirm()
    var form = e.target;
    if (form.target && form.target !== '_self') return;
    var btn = e.submitter || form.querySelector('button[type=submit], button:not([type])');
    if (form.method.toLowerCase() === 'post') {
      if (form.dataset.submitting) { e.preventDefault(); return; }  // no double posts
      form.dataset.submitting = '1';
      busy(btn);
    }
    startBar();
  });
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-submitting]').forEach(function (f) { delete f.dataset.submitting; });
  });

  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if ((a.target && a.target !== '_self') || a.hasAttribute('download')) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.protocol.indexOf('http') !== 0) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    startBar();
  });

  // ---- background jobs -----------------------------------------------------------------
  // Poll a small JSON endpoint; update text in place; reload with a toast when the job ends.
  var offline = null;
  function poll(url, every, onData) {
    var delay = every;
    function tick() {
      fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (d) {
            if (r.status === 404) { location.reload(); return null; }  // job record gone: show the page as it is now
            if (!r.ok) { var e = new Error(d.error || ('HTTP ' + r.status)); e.server = true; throw e; }
            return d;
          });
        })
        .then(function (d) {
          if (d === null) return;
          if (offline) { dismiss(offline); offline = null; toast('Reconnected to the server.', 'info'); }
          delay = every;
          if (onData(d) !== false) setTimeout(tick, delay);
        })
        .catch(function (err) {
          if (!offline) offline = toast(err.server
            ? 'Checking progress failed: ' + err.message + '. Retrying…'
            : "Can't reach the server. Retrying… Is uvicorn still running?", 'error');
          delay = Math.min(delay * 2, 15000);
          setTimeout(tick, delay);
        });
    }
    setTimeout(tick, every);
  }

  // Scans (header pill, Scans table, creators pages).
  var scanEls = document.querySelectorAll('[data-scan-job]');
  var scanIds = {};
  scanEls.forEach(function (el) { scanIds[el.dataset.scanJob] = true; });
  Object.keys(scanIds).forEach(function (id) {
    poll('/api/scans/' + id, 2500, function (s) {
      document.querySelectorAll('[data-scan-job="' + id + '"] [data-scan-field]').forEach(function (f) {
        var v = s[f.dataset.scanField];
        if (f.dataset.scanField === 'cost_usd') v = v ? Number(v).toFixed(2) : '–';
        if (v !== null && v !== undefined && f.textContent !== String(v)) f.textContent = v;
      });
      if (s.status === 'running') return true;
      var msg = s.message ? ': ' + s.message : '';
      var kinds = { finished: ['Scan finished' + msg, 'ok'], cancelled: ['Scan cancelled' + msg, 'info'] };
      var k = kinds[s.status] || ['Scan ' + String(s.status).replace('_', ' ') + msg, 'error'];
      toastAfterReload(k[0], k[1]);
      return false;
    });
  });

  // Analysis stepper on the video page.
  var job = document.querySelector('[data-analysis-job]');
  if (job) {
    var steps = Array.prototype.map.call(job.querySelectorAll('[data-step]'), function (li) { return li.dataset.step; });
    var label = job.querySelector('[data-step-label]');
    var fill = job.querySelector('.stepper-bar span');
    var render = function (step) {
      var at = Math.max(0, steps.indexOf(step));
      job.querySelectorAll('[data-step]').forEach(function (li, i) {
        li.classList.toggle('done', i < at);
        li.classList.toggle('current', i === at);
        if (i === at) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      });
      if (label) label.textContent = job.querySelectorAll('[data-step]')[at].querySelector('.step-name').textContent;
      if (fill) fill.style.transform = 'scaleX(' + ((at + 0.5) / steps.length) + ')';
    };
    poll('/api/analysis-status/' + job.dataset.analysisJob, 2000, function (a) {
      if (a.status === 'queued' || a.status === 'running') { render(a.step); return true; }
      if (a.status === 'done') toastAfterReload('Breakdown ready.', 'ok');
      else toastAfterReload('Analysis failed: ' + (a.error || 'unknown error'), 'error');
      return false;
    });
  }

  // Section nav on the breakdown: highlight the section in view.
  var nav = document.querySelector('.bd-nav');
  if (nav && 'IntersectionObserver' in window) {
    var links = {};
    nav.querySelectorAll('a[href^="#"]').forEach(function (a) { links[a.getAttribute('href').slice(1)] = a; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting || !links[en.target.id]) return;
        Object.keys(links).forEach(function (k) { links[k].removeAttribute('aria-current'); });
        links[en.target.id].setAttribute('aria-current', 'true');
      });
    }, { rootMargin: '-15% 0px -70% 0px' });
    Object.keys(links).forEach(function (id) { var s = document.getElementById(id); if (s) io.observe(s); });
  }
  // Bulk add creators: live count + cost estimate, and .txt/.csv import into the textarea.
  var bulk = document.querySelector('[data-bulk-add]');
  if (bulk) {
    var ta = bulk.querySelector('textarea[name=handles]'), out = bulk.querySelector('[data-bulk-count]');
    var sel = bulk.querySelector('select[name=platform]'), per = parseFloat(bulk.dataset.perCreator) || 0;
    var split = function (text) {
      var seen = {}, list = [];
      (text || '').split(/[\s,;|]+/).forEach(function (chunk) {
        chunk.split(/(?<=[\w.])(?=@)/).forEach(function (p) {
          p = p.trim().replace(/\.+$/, '').replace(/^["'()\[\]<>]+|["'()\[\]<>]+$/g, '').replace(/\.+$/, '');
          if (!p || p === '@') return;
          var k = p.toLowerCase().replace(/\/+$/, '');
          if (!seen[k]) { seen[k] = 1; list.push(p); }
        });
      });
      return list;
    };
    var platformOf = function (e) {
      var t = e.toLowerCase();
      if (t.indexOf('tiktok.com') > -1) return 'tiktok';
      if (t.indexOf('instagram.com') > -1) return 'instagram';
      if (t.indexOf('youtube.com') > -1 || t.indexOf('youtu.be') > -1) return 'youtube';
      return sel ? sel.value : '';
    };
    var update = function () {
      var list = split(ta.value), paid = list.filter(function (e) { return platformOf(e) !== 'youtube'; }).length;
      if (!list.length) { out.textContent = 'No handles yet.'; out.classList.remove('warn'); return; }
      out.textContent = list.length + (list.length === 1 ? ' creator' : ' creators') +
        (paid ? ' · up to ≈$' + (paid * per).toFixed(2) + ' (less if some are already tracked)' : ' · free');
      out.classList.toggle('warn', paid * per > 1);
    };
    ta.addEventListener('input', update);
    if (sel) sel.addEventListener('change', update);
    var file = bulk.querySelector('[data-handles-file]');
    if (file) file.addEventListener('change', function () {
      var f = file.files && file.files[0];
      if (!f) return;
      if (f.size > 1024 * 1024) { window.toast && window.toast('That file is over 1 MB. Use a plain list of handles.', 'error'); file.value = ''; return; }
      var r = new FileReader();
      r.onload = function () {
        ta.value = (ta.value.trim() ? ta.value.trim() + '\n' : '') + String(r.result || '');
        file.value = '';
        update();
        var n = split(ta.value).length;
        window.toast && window.toast('Imported ' + f.name + ': ' + n + ' creators in the list. Check it, then press Add creators.', 'info');
      };
      r.onerror = function () { window.toast && window.toast('Could not read ' + f.name + '.', 'error'); };
      r.readAsText(f);
    });
    update();
  }
})();
