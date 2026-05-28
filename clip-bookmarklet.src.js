// Readable source for the clip bookmarklet (mobile-first). The live version has
// __ENDPOINT__ (e.g. https://your.host/clip) and __TOKEN__ substituted by the
// setup page, then URL-encoded into a `javascript:` link.
//
// It does NOT load a converter in-page (strict `script-src` CSP would block it)
// and it does NOT use fetch() (strict `connect-src` CSP would block that). It
// reads the rendered DOM + selection and POSTs them via a hidden <form> opened
// in a new tab — form submission rides the `form-action` CSP directive, which
// most sites don't lock down, so the captured bytes get out where a fetch
// wouldn't. The server (voice-inbox /clip) does the HTML→markdown extraction.
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
    var fields = {
      token: '__TOKEN__',
      url: location.href,
      title: document.title,
      html: selHtml ? '' : document.documentElement.outerHTML,
      selection: selHtml,
      author: meta('meta[name="author"]'),
      published: meta('meta[property="article:published_time"]') || meta('time[datetime]'),
    };
    var form = document.createElement('form');
    form.method = 'POST';
    form.action = '__ENDPOINT__';
    form.target = '_blank';
    form.style.display = 'none';
    for (var k in fields) {
      var ta = document.createElement('textarea'); // textarea handles large HTML + newlines
      ta.name = k;
      ta.value = fields[k] == null ? '' : fields[k];
      form.appendChild(ta);
    }
    document.body.appendChild(form);
    form.submit();
    setTimeout(function () { form.remove(); }, 1000);
  } catch (e) {
    alert('Clip error: ' + e.message);
  }
})();
