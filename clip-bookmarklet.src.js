// Readable source for the clip bookmarklet (mobile-first). The live version has
// __ENDPOINT__ (e.g. https://your.host/clip) and __TOKEN__ substituted by the
// setup page, then URL-encoded into a `javascript:` link.
//
// It reads the rendered DOM + selection (never CSP-blocked) and sends it. Two
// transports, because sites lock down different CSP directives:
//   1. fetch() — works where `connect-src` allows the host (e.g. the BBC allows
//      `connect-src https:`). It's *catchable*, so we try it first.
//   2. hidden <form> POST in a new tab — rides `form-action` instead, for sites
//      that block `connect-src` but allow form submission. The fallback when
//      fetch is CSP-blocked (only a site locking *both* defeats both → use the
//      extension there).
// The captured bytes ride the request body, never a URL. The server does the
// HTML→markdown extraction.
(function () {
  try {
    var sel = window.getSelection && window.getSelection();
    var selHtml = '';
    if (sel && sel.rangeCount && !sel.isCollapsed) {
      var d = document.createElement('div');
      for (var i = 0; i < sel.rangeCount; i++) d.appendChild(sel.getRangeAt(i).cloneContents());
      selHtml = d.innerHTML;
    }
    var meta = function (q) {
      var e = document.querySelector(q);
      return e ? e.getAttribute('content') || e.getAttribute('datetime') || '' : '';
    };
    var EP = '__ENDPOINT__';
    var p = {
      token: '__TOKEN__',
      url: location.href,
      title: document.title,
      html: selHtml ? '' : document.documentElement.outerHTML,
      selection: selHtml,
      author: meta('meta[name="author"]'),
      published: meta('meta[property="article:published_time"]') || meta('time[datetime]'),
    };
    var toast = function (msg, ok) {
      var e = document.createElement('div');
      e.textContent = msg;
      e.style.cssText =
        'position:fixed;z-index:2147483647;left:50%;top:24px;transform:translateX(-50%);background:' +
        (ok ? '#0a7f3f' : '#b00020') +
        ';color:#fff;font:600 14px system-ui;padding:10px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)';
      document.body.appendChild(e);
      setTimeout(function () { e.remove(); }, 2800);
    };
    // Fallback: hidden form POST in a new tab (rides form-action, not connect-src).
    var formFallback = function () {
      var f = document.createElement('form');
      f.method = 'POST';
      f.action = EP;
      f.target = '_blank';
      f.style.display = 'none';
      for (var k in p) {
        var a = document.createElement('textarea'); // handles large HTML + newlines
        a.name = k;
        a.value = p[k] == null ? '' : p[k];
        f.appendChild(a);
      }
      document.body.appendChild(f);
      f.submit();
      setTimeout(function () { f.remove(); }, 1000);
    };
    toast('Clipping…', true);
    // Try fetch first — it's catchable, so we can detect a connect-src block and
    // fall back. (A blocked form submit is silent, so it can only be the fallback.)
    fetch(EP, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(p) })
      .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); })
      .then(function (j) {
        toast(j && j.ok ? (j.updated ? 'Updated ' : 'Clipped ') + (j.path || '') : 'Error: ' + ((j && j.detail) || 'failed'), j && j.ok);
      })
      .catch(function () { formFallback(); });
  } catch (e) {
    alert('Clip error: ' + e.message);
  }
})();
