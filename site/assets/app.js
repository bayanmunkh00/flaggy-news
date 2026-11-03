// Theme toggle and client-side search over search.json.
(function () {
  var root = document.documentElement;
  document.getElementById('theme').addEventListener('click', function () {
    var dark = getComputedStyle(root).getPropertyValue('--bg').trim() === '#0c0a09';
    var next = dark ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch (e) {}
  });

  var base = document.body.dataset.root || '';
  var box = document.getElementById('q');
  var results = document.getElementById('results');
  var content = document.getElementById('content');
  var index = null;

  function esc(s) {
    return s.replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function show(q) {
    q = q.trim().toLowerCase();
    if (!q) { results.hidden = true; content.hidden = false; return; }
    var hits = index.filter(function (a) {
      return (a.t + ' ' + a.o + ' ' + a.s).toLowerCase().indexOf(q) !== -1;
    }).slice(0, 50);
    results.innerHTML = hits.length
      ? hits.map(function (a) {
          return '<a class="hit" href="' + base + 'a/' + a.id + '.html">' + esc(a.t) +
                 '<small>' + esc(a.c) + ' · ' + a.d + '</small></a>';
        }).join('')
      : '<p class="empty">Олдсонгүй.</p>';
    results.hidden = false;
    content.hidden = true;
  }

  box.addEventListener('input', function () {
    if (index) return show(box.value);
    fetch(base + 'search.json').then(function (r) { return r.json(); }).then(function (d) {
      index = d;
      show(box.value);
    });
  });
})();
