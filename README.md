# Brainstem distro template

Make your own Brainstem for your own needs, without changing the Brainstem.

A **distro** is the Brainstem kernel, unchanged and pinned to one commit, plus whatever you build around it. People install your distro as a **plugin** in the AI tool they already use (Claude Code, Claude Desktop, Codex, VS Code, Cursor). The plugin starts your distro on their machine and opens the Brainstem's own chat window right inside that tool.

Click **Use this template** to start one.

## What you get

| Piece | What it does |
|---|---|
| `plugin/kernel.json` | The kernel commit you stand on, and the hash of its chat page. |
| `plugin/distro.json` | Your distro: its name, and what to run. |
| `plugin/server.py` | The plugin. It starts your engine on a free local port and serves the kernel's chat page as an app window inside the AI tool. Standard library only, nothing to install. |
| `plugin/bridge.js` | Lets the chat page talk to your engine from inside the app window. |
| `plugin/engine/` | Your engine's files, if your distro brings its own. |
| `tools/check.py` | Proves you stand on the kernel unchanged. |
| `tests/` | The plugin end to end, plus a real app-window run in a browser. |

## Make your distro

1. **Name it.** In `plugin/distro.json` set `id` and `display_name`. Use the same `id` in `plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json` and `.claude-plugin/marketplace.json`.
2. **Choose the engine.** Choose one of these two:
   - **The kernel itself** (the default). `"engine": {"url": "http://127.0.0.1:7071"}` uses a Brainstem that's already installed.
   - **Your own engine** that keeps the kernel's `/chat` and `/health` contract. Use `"engine": {"command": ["{python}", "{root}/engine/my_engine.py", "serve", "{port}"]}`. The plugin picks the port. If your engine doesn't serve `/agents`, set `"agents_dir"` and the plugin manages the agents folder for the chat window.
3. **Optional: use your own name and window size.** In `distro.json`, `"labels"` swaps the words people see on the kernel's page without changing the page itself, for example `[["RAPP Brainstem", "My Brainstem"], ["Message brainstem...", "Message me..."]]`. The swaps apply in order and also cover text the page adds later. `"height"` sets the window height (default 680).
4. **Add what your users need.** Build on top of the kernel, never inside it:
   - **Agent files:** drop agent files into the agents folder.
   - **Sidecars:** a sidecar talks to the engine only through `/chat` and `/health`.
   - **Your engine:** your own engine, as in step 2.
5. **Check it.** Run `python3 tools/check.py` and `python3 tests/test_plugin.py`. For the real app window, run `cd tests/host && npm install && npm test`.

## How people install it

- **Claude Code:** run `/plugin marketplace add <you>/<your-repo>`, then `/plugin install <id>@<id>`.
- **Claude Desktop, Codex, VS Code, Cursor:** add a local MCP server that runs `python3 <path>/plugin/server.py`.

After that they ask their AI tool to "open <your distro>" and the chat window appears.

## The rules

1. **Never patch the kernel.** Your distro pins a commit and builds on top of it. If every user needs a change and it can't be done from outside, ask the kernel's owner.
2. **Move the pin on purpose.** Update `sha` and `ui_blob` in `kernel.json`, then run the checks.
3. **Keep the contract.** `POST /chat` answers in the field `response`. `agent_logs` is text, one line per agent. `GET /health` returns `status`, `version`, `model`, `agents` and `quarantined`.

## Distros built on this

| Distro | What it adds |
|---|---|
| [Airaptr](https://github.com/airaptr/raptr) | Any OpenRouter model, scheduled jobs, built-in memory, a public demo mode |

Add yours with a pull request.

The app window uses the official MCP Apps client (`plugin/vendor/`, see its LICENSE).
