// Runs before the kernel's page. Inside an app card the page has no engine at its own address, so its
// same-origin calls (and the kernel's 127.0.0.1 fallback) go through the plugin's `http` tool instead.
(function () {
  var ready, failed;
  var app = new Promise(function (res, rej) { ready = res; failed = rej; });
  window.__distroReady = ready;
  window.__distroFailed = failed;
  var direct = window.fetch.bind(window);
  function enginePath(url) {
    if (url.charAt(0) === '/' && url.charAt(1) !== '/') return url;
    var m = /^https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?(\/.*)?$/.exec(url);
    return m ? (m[1] || '/') : null;
  }
  window.fetch = async function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || String(input);
    var path = enginePath(url);
    if (path === null) return direct(input, init);
    init = init || {};
    var args = { method: (init.method || 'GET').toUpperCase(), path: path };
    if (typeof init.body === 'string') args.body = init.body;
    else if (init.body instanceof FormData) {
      var f = init.body.get('file');
      if (f && f.text) args.file = { name: f.name, text: await f.text() };
    }
    var a = await app;
    var res = await a.callServerTool({ name: 'http', arguments: args });
    var sc = (res && res.structuredContent) || {};
    if (res && res.isError) sc = { status: 502, body: JSON.stringify({ error: (res.content && res.content[0] && res.content[0].text) || 'The engine is not available.' }) };
    return new Response(sc.body == null ? '' : sc.body, { status: sc.status || 502, headers: { 'Content-Type': 'application/json' } });
  };
})();
