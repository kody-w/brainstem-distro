// Drive the plugin through a real host path: stdio MCP client -> plugin -> app card -> one chat turn.
//   node run.mjs <plugin dir> <message> [sandbox]
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
const { chromium } = await import(process.env.PLAYWRIGHT || 'playwright');

const [dir, message = 'Reply with exactly: card works', sandbox = 'allow-scripts allow-same-origin allow-forms'] = process.argv.slice(2);
const proc = spawn('python3', [dir + '/server.py'], { stdio: ['pipe', 'pipe', 'inherit'] });
let buf = '', next = 1; const waiting = new Map();
proc.stdout.on('data', d => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); waiting.get(m.id)?.(m); } });
const rpc = (method, params) => new Promise((res, rej) => { const id = next++; waiting.set(id, m => m.error ? rej(new Error(m.error.message)) : res(m.result)); proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });

const out = {};
await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: { extensions: { 'io.modelcontextprotocol/ui': { mimeTypes: ['text/html;profile=mcp-app'] } } }, clientInfo: { name: 'test-host', version: '1' } });
const { tools } = await rpc('tools/list', {});
const open = tools.find(t => t.name === 'open');
out.open_has_ui = open._meta.ui.resourceUri;
out.http_app_only = JSON.stringify(tools.find(t => t.name === 'http')._meta.ui.visibility);
await rpc('tools/call', { name: 'open', arguments: {} });
const res = await rpc('resources/read', { uri: open._meta.ui.resourceUri });
const html = res.contents[0].text; out.mime = res.contents[0].mimeType; out.bytes = html.length;

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 960, height: 760 } });
const calls = [];
await page.exposeFunction('hostCallTool', async p => { const params = JSON.parse(p); calls.push(params.arguments?.method + ' ' + params.arguments?.path); return JSON.stringify(await rpc('tools/call', params)); });
await page.setContent('<html><body style="margin:0;background:#222"></body></html>');
await page.addScriptTag({ content: readFileSync(new URL('./host.bundle.js', import.meta.url), 'utf8'), type: 'module' });
await page.evaluate(([h, s]) => window.startHost(h, s), [html, sandbox]);
await page.waitForFunction(() => window.appInitialized === true, null, { timeout: 20000 });
out.initialized = true;
const frame = page.frames().find(f => f !== page.mainFrame());
const errors = []; page.on('pageerror', e => errors.push(String(e)));
await frame.waitForSelector('#input', { timeout: 20000 });
const box = await frame.$('#input');
await box.fill(message); await box.press('Enter');
await frame.waitForFunction(() => [...document.querySelectorAll('.msg.assistant:not(.typing-indicator)')].some(e => e.innerText.trim().length > 0), null, { timeout: 120000 }).catch(() => {});
await page.waitForTimeout(1500);
out.reply = await frame.evaluate(() => { const els = [...document.querySelectorAll('.msg.assistant:not(.typing-indicator)')]; return els.length ? els[els.length - 1].innerText.trim().slice(0, 300) : null; });
out.seen = await frame.evaluate(() => ({ title: document.title, placeholder: document.querySelector('#input').placeholder,
  welcome: (document.body.innerText.match(/Welcome[^\n]*/) || [''])[0], leftover: [...new Set(document.body.innerText.match(/RAPP|[Bb]rainstem/g) || [])] }));
out.calls = calls; out.page_errors = errors;
await page.screenshot({ path: dir + '/../card.png' });
console.log(JSON.stringify(out, null, 2));
await browser.close(); proc.kill();
