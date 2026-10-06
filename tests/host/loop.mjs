// The three-way loop: the AI tool (via `chat`), the person (in the window) and the distro share one conversation.
//   DISTRO_CACHE=<empty dir> node loop.mjs <plugin dir>
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
const { chromium } = await import(process.env.PLAYWRIGHT || 'playwright');

const dir = process.argv[2];
const proc = spawn('python3', [dir + '/server.py'], { stdio: ['pipe', 'pipe', 'inherit'] });
let buf = '', next = 1; const waiting = new Map();
proc.stdout.on('data', d => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); waiting.get(m.id)?.(m); } });
const rpc = (method, params) => new Promise((res, rej) => { const id = next++; waiting.set(id, m => m.error ? rej(new Error(m.error.message)) : res(m.result)); proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });
const say = async text => (await rpc('tools/call', { name: 'chat', arguments: { message: text } })).content[0].text;

await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'claude-ai', version: '1' } });
const { tools } = await rpc('tools/list', {});
const open = tools.find(t => t.name === 'open');
const html = (await rpc('resources/read', { uri: open._meta.ui.resourceUri })).contents[0].text;
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 960, height: 760 } });
await page.exposeFunction('hostCallTool', async p => JSON.stringify(await rpc('tools/call', JSON.parse(p))));
await page.setContent('<html><body style="margin:0;background:#222"></body></html>');
await page.addScriptTag({ content: readFileSync(new URL('./host.bundle.js', import.meta.url), 'utf8'), type: 'module' });
await page.evaluate(h => window.startHost(h, 'allow-scripts allow-same-origin allow-forms'), html);
await page.waitForFunction(() => window.appInitialized === true, null, { timeout: 20000 });
const frame = page.frames().find(f => f !== page.mainFrame());
await frame.waitForSelector('#input');
const bubbles = () => frame.evaluate(() => [...document.querySelectorAll('.msg:not(.system):not(.typing-indicator) .bubble')].map(b => b.innerText.trim().slice(0, 120)));

const out = {};
out.claude_1 = await say('My favorite bird is the PELICAN. Reply only with: noted.');
await page.waitForTimeout(3500);
out.window_shows_claude = (await bubbles()).some(b => b.startsWith('Claude: My favorite bird'));
const box = await frame.$('#input');
await box.fill('What bird did Claude just say is its favorite? Reply with only the bird.'); await box.press('Enter');
await frame.waitForFunction(n => document.querySelectorAll('.msg.assistant:not(.typing-indicator)').length >= n, 2, { timeout: 120000 });
await page.waitForTimeout(1500);
out.window_answer = (await bubbles()).slice(-1)[0];
out.claude_2 = await say('Thanks. Reply with only: back to you.');
out.claude_sees_user = out.claude_2.includes('Since you last spoke') && out.claude_2.includes('What bird');
await page.waitForTimeout(3500);
out.window_final = await bubbles();
await page.screenshot({ path: dir + '/../loop.png' });
console.log(JSON.stringify(out, null, 2));
await browser.close(); proc.kill();
