// A minimal MCP Apps host built on the official AppBridge, the same piece real hosts use.
import { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';

window.startHost = async function (html, sandbox) {
  const iframe = document.createElement('iframe');
  iframe.setAttribute('sandbox', sandbox);
  iframe.style.cssText = 'width:900px;height:700px;border:0';
  document.body.appendChild(iframe);
  const bridge = new AppBridge(null, { name: 'test-host', version: '1.0.0' }, { serverTools: {}, openLinks: {} });
  bridge.oncalltool = async (params) => JSON.parse(await window.hostCallTool(JSON.stringify(params)));
  bridge.oninitialized = () => { window.appInitialized = true; };
  await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
  iframe.srcdoc = html;
};
