// Trove clip bookmarklet (mobile-first). Readable source; the live version has
// __ENDPOINT__ (e.g. https://your.host/clip) and __TOKEN__ substituted, then is
// URL-encoded into a `javascript:` link.
//
// It does NOT load any converter in-page (that would trip strict `script-src`
// CSP). It just reads the rendered DOM + selection and POSTs them; the server
// (voice-inbox /clip) does the HTML→markdown extraction. The only thing that can
// still block it is a strict `connect-src` CSP forbidding the POST — irreducible
// for a bookmarklet, and where you'd reach for the extension instead.
(function () {
  try {
    var sel = window.getSelection && window.getSelection();
    var selHtml = '';
    if (sel && sel.rangeCount && !sel.isCollapsed) {
      var d = document.createElement('div');
      for (var i = 0; i < sel.rangeCount; i++) d.appendChild(sel.getRangeAt(i).cloneContents());
      selHtml = d.innerHTML;
    }
    var meta = function (s) {
      var e = document.querySelector(s);
      return e ? e.getAttribute('content') || e.getAttribute('datetime') || '' : '';
    };
    var payload = {
      url: location.href,
      title: document.title,
      html: selHtml ? '' : document.documentElement.outerHTML,
      selection: selHtml,
      excerpt: meta('meta[name="description"]') || meta('meta[property="og:description"]'),
      author: meta('meta[name="author"]') || meta('meta[property="article:author"]'),
      siteName: meta('meta[property="og:site_name"]'),
      published: meta('meta[property="article:published_time"]') || meta('time[datetime]'),
    };
    var toast = function (msg, ok) {
      var t = document.createElement('div');
      t.textContent = msg;
      t.style.cssText =
        'position:fixed;z-index:2147483647;left:50%;top:24px;transform:translateX(-50%);background:' +
        (ok ? '#0a7f3f' : '#b00020') +
        ';color:#fff;font:600 14px system-ui;padding:10px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)';
      document.body.appendChild(t);
      setTimeout(function () { t.remove(); }, 2800);
    };
    toast('Clipping…', true);
    fetch('__ENDPOINT__', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer __TOKEN__' },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); })
      .then(function (j) {
        toast(j && j.ok ? (j.updated ? 'Updated ✓' : 'Clipped ✓') + ' ' + (j.path || '') : 'Clip error: ' + ((j && j.detail) || 'failed'), j && j.ok);
      })
      .catch(function (e) { toast('Clip error (blocked by page?): ' + e.message, false); });
  } catch (e) {
    alert('Clip error: ' + e.message);
  }
})();
