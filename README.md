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

## What every distro can do

- **Open its window** inside the AI tool: the kernel's own chat page, connected to your engine.
- **One conversation for everyone.** You in the window, and every AI tool that has the plugin (Claude, Cursor, Codex, VS Code...), talk to the same engine `/chat` in one shared conversation. Each AI's turns are tagged with its name, the window shows them as they happen, and each AI's `chat` answer includes what you said in the window since it last spoke. It's a group chat with all your AIs.
- **Bring the other AIs in.** Mention `@Claude`, `@Codex`, `@Copilot` (or `@all`) and the distro brings them into the conversation itself, through the command-line tools the user is already signed in to (`"voices"` in `distro.json`). They run with no tools, from an empty folder, and only see the conversation. The distro can mention them too, so it can call on them without being asked. `"max_rounds"` (default 6) caps how many turns one message can set off.
- **Take over.** Say "take over: <goal>" (or just "take over"). The distro keeps the AIs working toward the goal and only contacts you, with a desktop notification and a note in the window, when it needs a decision from you or when it's done. Say "stop" to stop, or "keep going" to continue.
- **Any model as a voice, with a privacy gate.** A voice can also be a model behind an API: `{"kind": "api", "model": "x-ai/grok-4.7", "key_from": {"file": "~/.config/me/key.json", "field": "key"}}` (OpenRouter by default). Add `"gate": "redact"` and the conversation is cleaned on this machine before it leaves. Emails, keys, card and phone numbers, home folders, IP addresses and every term on your local private list (`"denylist"`: one per line, `re:` for a pattern; keep it out of any repo) are swapped for placeholders like `[EMAIL_1]`. The real values go back into the reply locally, the same way every time. If the gate can't run, nothing is sent. Each gated call is recorded locally in `gate-log.jsonl` (counts and a fingerprint, never the values).
- **Credit for your calls.** Set `"attribution": {"url": "https://your.site", "title": "Your App"}` and the API voices send the same app headers your engine does, so a provider that ranks apps (OpenRouter) counts them for you.
- **Optional on-device screen (off by default).** Set `"screen": {"url": "http://127.0.0.1:11434/v1", "model": "<a local model>"}` and a model on your own machine reads the conversation first. It catches what patterns can't, like names, places, and health or money details, before the gate. Only local addresses are accepted, and an unreachable screen blocks the call. With the gate and the screen on, cheap or free models that keep your data become safe to use.
- **Any AI can drive it.** Tools with plugins get `take_over` (hand over a goal and wait for the result), `chat` and `conversation`. Anything with a terminal (an agent, a script, a scheduled job) uses the same conversation: `python3 plugin/server.py --take-over "goal"`, `--say "message"`, `--conversation`, with `DISTRO_SPEAKER=<name>` saying who it is. When an AI is driving and waiting, the final note goes to it instead of the person's desktop.
- **Learn new skills from the AI tool.** The `add_agent` tool lets Claude (or any host) write an agent file and install it. If the file doesn't load, the host gets the error back and the file is removed, so the host can fix the code and try again.
- **Keep the AI tool in the loop.** What people ask in the window, and the agents they add or remove, goes back to the host as context, so it knows what just happened without anyone pasting it.
- **Run a scripted demo.** Point `"demo"` in `distro.json` at a file like `{"steps": ["What can you do? Answer in under 80 words.", "..."]}`. In the message box the up arrow loads the next step, the down arrow goes back, and Enter sends it. Keep every step short enough that the answer fits on one screen.

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
