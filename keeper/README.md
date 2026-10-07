# Brainstem keeper

Keeper keeps your Brainstem chat available when an update breaks or the network
is down. It tests updates and automatically returns to the last working
version. It keeps your sign-in in place and can be removed without removing
Brainstem.

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
| `signin status` | Shows the machine holder generation and registered Brainstems by alias, including metadata drift and newer successful sign-ins. |
| `signin adopt [--from <dir>]` | Adopts an existing Brainstem's token record as the machine holder without starting a device flow. Defaults to the main install. |
| `signin register <dir>` | Adds a Brainstem to the private allowlist and atomically projects the holder into it. |
| `signin unregister <dir>` | Removes a Brainstem from the allowlist and leaves its token file untouched. |
| `signin sync` | Adopts the newest non-failing sign-in from a registered Brainstem, then projects that generation to all registered Brainstems. |
| `install-service` | Installs keeper as a macOS LaunchAgent or Linux systemd user service. If neither is available, prints the one command to run. |
| `uninstall-service` | Removes only the keeper service. |
| `uninstall` | Stops keeper-managed processes and removes `~/.brainstem/keeper/` and the keeper service. |

`install-service` copies the script and the current kernel pin into
`~/.brainstem/keeper/`, so the service does not depend on the repository staying
in the same place. When a sign-in holder exists, `watch` also runs `signin sync`
once per cycle before checking Brainstem health.

`signin` never starts OAuth, requests `offline_access`, creates refresh tokens,
or prints credential values. Status and command results use stable aliases such
as `main` and `brainstem-1`, generation numbers, and timestamps rather than
registered filesystem paths. Generation matching deliberately uses only the
holder's recorded file size and modification time; keeper never compares or
hashes token contents.

## What keeper stores

- `~/.brainstem/keeper/state.json`: last known-good release, failed releases,
  safe-copy release, and the last 20 events.
- `~/.brainstem/keeper/keeper.log`: keeper, installer, and server output.
- `~/.brainstem/keeper/signin/.copilot_token`: the machine's private holder
  record.
- `~/.brainstem/keeper/signin/generation.json`: holder generation, timestamp,
  source alias, size, and modification time.
- `~/.brainstem/keeper/signin/manifest.json`: the private allowlist mapping
  stable aliases to registered Brainstem directories.
- `~/.brainstem/keeper/carry/`: temporary 0600 copies of the kernel's sign-in
  files while the official installer runs. Copies are deleted after the files
  are safely present beside `brainstem.py` again.
- `~/.brainstem/keeper/safe/<version>/`: tracked kernel files only. User
  `.env`, token, data, and untracked agents are not copied.
- `~/.brainstem/keeper/safe-venv/`: the safe copy's Python environment.

The protected sign-in files are `.copilot_token`, `.copilot_session`,
`.copilot_pending`, and `.brainstem_secret`, matching the credential and
session files written by the pinned kernel. Keeper never logs or hashes their
contents. The safe server links to those existing files and the user's `.env`.
It uses the user's agents only when they can be imported; otherwise it uses the
agents shipped in the safe copy.

The shared holder intentionally covers only `.copilot_token`. The existing
temporary carry mechanism remains because upgrades must also preserve
`.copilot_session`, `.copilot_pending`, and `.brainstem_secret`, and because it
protects installs before a holder has been adopted. Holder and manifest
directories are mode 0700 and their files are mode 0600. Projection uses a
same-directory temporary file, file and directory `fsync`, and `os.replace`.
`uninstall` removes the holder and manifest with the rest of keeper but never
deletes token files from Brainstem directories.

## Upgrade and recovery behavior

Keeper uses the official Brainstem installer and immutable
`brainstem-vX.Y.Z` release tags. A release is trusted only after both checks
pass:

1. `GET /health` returns 200 JSON with the expected version.
2. `POST /chat` with `{}` returns a 400 JSON error without calling a model.

A failed version is recorded and skipped until a higher target is available.
Use `--force` to retry it. Keeper does not depend on installer banner wording.
If the official installer launches Brainstem, keeper waits for the complete
health and chat contract, verifies the expected version, and adopts the process
holding the port. If the installer honors `--no-launch`, keeper starts the
installed version itself afterward. A hard timeout stops only an installer that
neither exits nor produces a healthy Brainstem.

Run the unit suite with:

```bash
python3 -m unittest discover -s keeper/tests -v
```

The Linux end-to-end proof is stored in `tests/e2e_linux.sh` and can be launched
on the configured NAS with:

```bash
bash keeper/tests/run_e2e_on_nas.sh
```

The separate shared-sign-in proof installs one official Brainstem plus two
token-free twins, verifies adopt/register/adopt-forward/uninstall, and can be
launched with:

```bash
bash keeper/tests/run_signin_e2e_on_nas.sh
```
