# Dogfood log

Use your own distro first. Write one line here for every rough edge you hit, dated, with what you saw.

Each line ends up in one of three places:
- **your distro**, if only you need the fix;
- **userland** (an agent file or a sidecar that talks to `/chat` and `/health`), if other distros need it too;
- **the kernel**, only if every user needs it and it can't be done from outside. The kernel stays unchanged in your
  distro; propose kernel changes upstream, never patch your pinned copy.

<!-- example:
- 2026-10-27: the chat page showed no error when the engine was down. Fixed in plugin/bridge.js.
-->
