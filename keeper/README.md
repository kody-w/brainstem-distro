# Brainstem keeper

Keeper is a seat belt for Brainstem. It remembers the last version that worked,
tests every upgrade before trusting it, and goes back automatically if the new
version is broken. If the installer or network is unavailable, it can start its
own known-good copy so the chat still comes up.

Removing keeper removes only keeper. It does not edit or remove the official
Brainstem installation.

Keeper is a standard-library-only Python 3.9+ script for macOS and Linux.
Windows is not supported in this release.

## Commands

Run commands with `python3 keeper/keeper.py <command>` from this repository:

| Command | What it does |
|---|---|
| `status` | Shows the installed version, last known-good version, failed versions, health, safe-copy version, and which copy is serving. |
| `start` | Keeps a healthy installed Brainstem running, reinstalls the last known-good release when needed, then falls back to the safe copy. |
| `upgrade` | Upgrades to the `version` pinned in `plugin/kernel.json`. |
| `upgrade --to 0.6.16` | Upgrades to one explicit release. |
| `upgrade --to 0.6.16 --force` | Retries a failed release or reinstalls a non-newer release. |
| `watch --interval 30` | Runs `start` repeatedly and checks for an upgrade once a day. |
| `install-service` | Installs keeper as a macOS LaunchAgent or Linux systemd user service. If neither is available, prints the one command to run. |
| `uninstall-service` | Removes only the keeper service. |
| `uninstall` | Stops keeper-managed processes and removes `~/.brainstem/keeper/` and the keeper service. |

`install-service` copies the script and the current kernel pin into
`~/.brainstem/keeper/`, so the service does not depend on the repository staying
in the same place.

## What keeper stores

- `~/.brainstem/keeper/state.json`: last known-good release, failed releases,
  safe-copy release, and the last 20 events.
- `~/.brainstem/keeper/keeper.log`: keeper, installer, and server output.
- `~/.brainstem/keeper/safe/<version>/`: tracked kernel files only. User
  `.env`, token, data, and untracked agents are not copied.
- `~/.brainstem/keeper/safe-venv/`: the safe copy's Python environment.

The safe server links to the user's existing `.env` and `.copilot_token`. It
uses the user's agents only when they can be imported; otherwise it uses the
agents shipped in the safe copy.

## Upgrade and recovery behavior

Keeper uses the official Brainstem installer and immutable
`brainstem-vX.Y.Z` release tags. A release is trusted only after both checks
pass:

1. `GET /health` returns 200 JSON with the expected version.
2. `POST /chat` with `{}` returns a 400 JSON error without calling a model.

A failed version is recorded and skipped until a higher target is available.
Use `--force` to retry it. The currently served official installer does not yet
honor `--no-launch`; keeper detects that installer revision and stops its
process group immediately after the completed-install banner, before its launch
phase can persist. Installer revisions with native `--no-launch` support run
normally.

Run the unit suite with:

```bash
python3 -m unittest discover -s keeper/tests -v
```

The Linux end-to-end proof is stored in `tests/e2e_linux.sh` and can be launched
on the configured NAS with:

```bash
bash keeper/tests/run_e2e_on_nas.sh
```
