(function () {
  var every = (parseInt(document.body.getAttribute('data-refresh'), 10) || 15) * 1000;
  var busy = false;
  function openState() {
    var s = {};
    document.querySelectorAll('details[id]').forEach(function (d) { s[d.id] = d.open; });
    return s;
  }
  function tick() {
    if (busy || document.hidden) { return; }
    busy = true;
    fetch(location.pathname + location.search, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (text) {
        var fresh = new DOMParser().parseFromString(text, 'text/html');
        var state = openState();
        document.querySelectorAll('[data-live]').forEach(function (el) {
          var key = el.getAttribute('data-live');
          var next = fresh.querySelector('[data-live="' + key + '"]');
          if (next && next.innerHTML !== el.innerHTML) { el.innerHTML = next.innerHTML; }
        });
        document.querySelectorAll('details[id]').forEach(function (d) {
          if (d.id in state) { d.open = state[d.id]; }
        });
        var clock = document.getElementById('clock');
        var freshClock = fresh.getElementById('clock');
        if (clock && freshClock) { clock.textContent = freshClock.textContent; }
        document.body.classList.remove('stale');
      })
      .catch(function () { document.body.classList.add('stale'); })
      .then(function () { busy = false; });
  }
  setInterval(tick, every);
})();
