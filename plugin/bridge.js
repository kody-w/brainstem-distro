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
  // Keep the AI tool aware of what happens in the window: the latest few turns and agent changes.
  var recent = [];
  function tell(a, args, body) {
    var line = null;
    try {
      if (args.method === 'POST' && args.path === '/chat') {
        var q = JSON.parse(args.body || '{}').user_input || '', d = JSON.parse(body || '{}');
        line = '- asked: ' + q.slice(0, 400) + '\n  answer: ' + String(d.response || '').slice(0, 800);
      } else if (args.method === 'POST' && args.path === '/agents/import' && args.file) line = '- added the agent file ' + args.file.name;
      else if (args.method === 'DELETE' && args.path.indexOf('/agents/') === 0) line = '- removed the agent file ' + args.path.slice(8);
    } catch (e) { return; }
    if (!line) return;
    recent.push(line); if (recent.length > 6) recent.shift();
    var caps = a.getHostCapabilities && a.getHostCapabilities();
    if (!caps || !caps.updateModelContext) return;
    var name = (window.__distro || {}).name || 'the app';
    a.updateModelContext({ content: [{ type: 'text', text: 'What the user just did in the ' + name + ' window (most recent last):\n' + recent.join('\n') }] }).catch(function () {});
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
    if (sc.status === 200) tell(a, args, sc.body);
    if (res && res.isError) sc = { status: 502, body: JSON.stringify({ error: (res.content && res.content[0] && res.content[0].text) || 'The engine is not available.' }) };
    return new Response(sc.body == null ? '' : sc.body, { status: sc.status || 502, headers: { 'Content-Type': 'application/json' } });
  };
})();

// Optional relabeling (distro.json "labels": [[from, to], ...], applied in order). The kernel's page stays
// byte-for-byte unchanged; only the words people see are swapped, including text the page adds later.
(function () {
  var labels = (window.__distro || {}).labels || [];
  if (!labels.length) return;
  function swap(s) {
    for (var i = 0; i < labels.length; i++) if (s.indexOf(labels[i][0]) >= 0) s = s.split(labels[i][0]).join(labels[i][1]);
    return s;
  }
  var ATTRS = ['placeholder', 'title', 'aria-label'];
  function relabel(root) {
    if (root.nodeType === 3) { var t = swap(root.nodeValue); if (t !== root.nodeValue) root.nodeValue = t; return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT), n = root;
    do {
      if (n.nodeType === 3) {
        var p = n.parentNode && n.parentNode.nodeName;
        if (p !== 'SCRIPT' && p !== 'STYLE' && p !== 'TEXTAREA') { var v = swap(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
      } else if (n.nodeType === 1) {
        for (var i = 0; i < ATTRS.length; i++) { var a = n.getAttribute(ATTRS[i]); if (a) { var b = swap(a); if (b !== a) n.setAttribute(ATTRS[i], b); } }
      }
    } while ((n = w.nextNode()));
  }
  function start() {
    relabel(document.body);
    document.title = swap(document.title);
    new MutationObserver(function (ms) {
      ms.forEach(function (m) {
        if (m.type === 'characterData') relabel(m.target);
        else if (m.type === 'attributes') relabel(m.target);
        else m.addedNodes.forEach(relabel);
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();

// Demo mode (distro.json "demo" points at {"steps": [...]}): in the message box, the up arrow loads the next
// scripted step, the down arrow goes back, Enter sends it as usual.
(function () {
  var steps = (window.__distro || {}).demo || [];
  if (!steps.length) return;
  var at = -1;
  function start() {
    var box = document.getElementById('input');
    if (!box) return;
    box.setAttribute('placeholder', box.getAttribute('placeholder') + '  (\u2191 next demo step)');
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      var v = box.value;
      if (v && (at < 0 || v !== steps[at])) return;  // the person is editing their own text; leave the arrows alone
      if (e.key === 'ArrowUp') { if (v === steps[at] || at < 0 || !v) at = Math.min(at + 1, steps.length - 1); }
      else at = Math.max(at - 1, -1);
      box.value = at < 0 ? '' : steps[at];
      box.dispatchEvent(new Event('input', { bubbles: true }));
      e.preventDefault(); e.stopImmediatePropagation();
    }, true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();

// One conversation for everyone. The window shows what was said before it opened, then follows turns that
// arrive from the AI tool (its `chat` tool) while it is open. Its own turns the page already draws itself.
(function () {
  var seen = -1;
  function draw(t, mine) {
    if (typeof appendMsg !== 'function') return;
    if (t.who === 'notice') { appendMsg('system', t.text); return; }
    var said = mine ? t.text : t.who + ': ' + t.text;
    appendMsg('user', said);
    if (t.reply) appendMsg('assistant', t.reply, t.agent_logs && String(t.agent_logs).trim() ? t.agent_logs : null);
    try { history.push({ role: 'user', content: said }, { role: 'assistant', content: t.reply }); } catch (e) {}
  }
  async function poll() {
    try {
      var r = await window.fetch('/distro/thread?after=' + Math.max(seen, 0));
      var turns = (await r.json()).turns || [];
      for (var i = 0; i < turns.length; i++) {
        var t = turns[i];
        if (seen < 0 || t.who !== 'window') draw(t, t.who === 'window');
        seen = Math.max(seen, t.n);
      }
      if (seen < 0) seen = 0;
    } catch (e) {}
    setTimeout(poll, 2000);
  }
  function start() { setTimeout(poll, 300); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
