// Checkbox dropdown filters: summary text, click-outside to close, keyword lists tied to genres,
// and auto-apply on close for forms marked data-autosubmit.
(function () {
  function summarize(d) {
    var picked = Array.prototype.filter.call(d.querySelectorAll('.multi-option'), function (o) {
      return !o.hidden && o.querySelector('input').checked;
    }).map(function (o) { return o.querySelector('span').textContent.trim(); });
    d.querySelector('summary').textContent = picked.length ? picked.join(', ') : d.dataset.all;
  }

  function syncKeywords(form) {
    var kw = form.querySelector('[data-keyword-filter]');
    if (!kw) return;
    var genres = Array.prototype.map.call(form.querySelectorAll('input[name=genre]:checked'),
      function (i) { return i.value; });
    var any = false;
    kw.querySelectorAll('.multi-option').forEach(function (o) {
      var show = genres.indexOf(o.dataset.group) !== -1;
      o.hidden = !show;
      if (!show) o.querySelector('input').checked = false;
      any = any || show;
    });
    kw.hidden = !any;  // like before: pick a genre to narrow by keyword
    summarize(kw.querySelector('details.multi'));
  }

  function state(form) { return new URLSearchParams(new FormData(form)).toString(); }

  document.querySelectorAll('form.filters').forEach(function (form) {
    var initial = state(form);
    syncKeywords(form);
    form.querySelectorAll('details.multi').forEach(function (d) {
      d.addEventListener('change', function () { syncKeywords(form); summarize(d); });
      d.querySelector('.multi-clear').addEventListener('click', function () {
        d.querySelectorAll('input').forEach(function (i) { i.checked = false; });
        syncKeywords(form); summarize(d);
      });
      d.addEventListener('toggle', function () {
        if (!d.open && form.hasAttribute('data-autosubmit') && state(form) !== initial) form.submit();
      });
    });
  });

  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.multi[open]').forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') document.querySelectorAll('details.multi[open]').forEach(function (d) { d.open = false; });
  });
})();
