# codex-resume

Automatically recover stopped Codex threads from one monitor on macOS or Linux. The monitor connects to the shared daemon used by `codex agents`; it does not require tmux, navigate the TUI, or open a terminal for every conversation.

It observes working root threads, detects failed turns, and resumes an incomplete Goal or submits `Continue`. Repeated failures switch that thread to a 15-minute retry interval, allowing another attempt after a temporary outage or usage cap clears.

This repository contains the **free terminal monitor**. A desktop app is planned separately. This is an independent project, not an official OpenAI tool.

## Requirements

- macOS or Linux.
- Python **3.10 or newer**, with `venv` and pip.
- Git to clone the repository.
- An installed and configured Codex CLI. The tested version is **0.164.0**; compatibility with other versions is not guaranteed because the app-server protocol can change.
- The Python package listed in [`requirements.txt`](requirements.txt): `websocket-client` and `rich`.

The watcher uses the existing daemon's authentication. Do not put API keys in this repository or pass a key to the watcher. Configure your model provider in Codex itself.

## Installation

### 1. Install prerequisites

On macOS, if you use [Homebrew](https://brew.sh/):

```bash
brew install python git node
```

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Check that Python is at least 3.10:

```bash
python3 --version
```

If Codex is not already installed, install Node.js and npm for your platform, then install the tested CLI version:

```bash
npm install -g @openai/codex@0.164.0
codex --version
```

The npm package for that Codex version requires Node.js 16 or newer. If you already have a working Codex installation, keep it and skip the installation command. Sign in or configure your custom provider using your normal Codex setup before continuing.

### 2. Clone and install the Python dependency

```bash
git clone https://github.com/HanYC666/codex-resume.git
cd codex-resume
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The virtual environment keeps dependencies separate from system Python. When returning in a new terminal, run `source .venv/bin/activate` again from this directory. Alternatively, use `.venv/bin/python` instead of `python` in the commands below.

### 3. Start Codex and check the connection

In your Codex terminal:

```bash
codex agents
```

Keep the daemon and your working threads running. In the watcher terminal, from the repository directory with the virtual environment active:

```bash
python codex_multi_recovery.py --check -v
```

A successful check ends with `Connection OK. Loaded threads (...)`. This performs no recovery. If it lists zero threads, start or resume a task through Codex first.

Use the **same OS user and `CODEX_HOME`** as your Codex installation. Threads connected to private/embedded app servers or another computer are not visible to this watcher.

### 4. Inspect a dry run, then enable recovery

Start the watcher while the threads are still working:

```bash
python codex_multi_recovery.py --dry-run -v
```

A dry run logs intended operations without submitting them. After checking its output, stop it with **Ctrl+C**, ensure the threads are working again, and enable real recovery:

```bash
python codex_multi_recovery.py -v
```

Leave the watcher running. Ctrl+C stops it and prints the run summary. `deactivate` leaves the Python virtual environment.

## How recovery works

The default poll interval is five seconds. Only root threads observed working during the current run are enrolled; inactive historical conversations and subagents are excluded. The watcher inspects stopped threads in either `idle` or `systemError`, verifies a failed turn, and rechecks the state before submission.

| Thread state | Recovery operation |
| --- | --- |
| An incomplete Goal exists | Set its status to `active` through `thread/goal/set`, corresponding to Goal resume |
| No incomplete Goal exists | Submit `Continue` through `turn/start` |

These are app-server operations, not injected keystrokes. While Codex is still active or retrying internally, the watcher waits. Detection normally occurs on the next poll; no intentional delay is added before the initial recovery checks and submission.

### Repeated errors and quota resets

Retry state is independent for each thread:

1. Recover the first eligible failed turn immediately after checks.
2. After **two consecutive matching errors**, or **three consecutive errors of any type**, enter slow retry mode.
3. Retry at most once every **15 minutes**, measured from the previous recovery attempt. The same failed turn may be retried when the timer expires.
4. Stay in slow mode until a completed turn is observed after the failure. Merely returning to `active` does not reset the error streak.

Repeated polls of the same failed turn do not count as new errors. Error matching normalizes common categories and ignores changing request IDs where recognized. Quota/usage-limit errors are eligible for recovery. The schedule does not query the provider's reset time; another attempt happens within roughly 15 minutes of a cap clearing, provided the watcher and daemon remain available and the turn is still eligible. Polling and RPC latency can add delay.

The watcher does not approve permission requests or security notices. Safety/permission failures remain excluded. Persistent outages can still produce repeated API requests, especially before slow mode begins.

### Provider-specific model fallback

For the specific error `Invalid input type 'configuration_update'` / `invalid_input_type`, the watcher applies this default policy:

| Current model | Fallback model | Reasoning |
| --- | --- | --- |
| `gpt-6.1-sol` | `gpt-6-sol` | Preserve the same level |
| `gpt-6-astra` | `gpt-6-sol` | Preserve the same level |

This is a user-specified provider workaround. The error text describes an unsupported input type; it does not, by itself, prove a model-specific usage limit. Other invalid input types and general 429/quota errors do not trigger model switching.

The watcher updates only the affected thread through `thread/settings/update`, then reads back the model and reasoning setting before issuing its normal Continue or Goal resume recovery. It preserves the explicit reasoning level; if the level is unset, it tries to resolve the source model's default from the model catalog. If it cannot preserve or confirm the settings, or no fallback is configured, it stops automatic recovery for that thread rather than guessing a lower level or repeatedly changing models. A confirmed fallback gets one immediate recovery even if the thread was already in 15-minute mode.

If a **new failed turn** reports the same configuration-update error after the confirmed switch, automatic recovery is disabled for that thread for the rest of the watcher run. An incomplete Goal is paused; a failed prompt thread remains stopped. There are no 15-minute retries for this stop condition. Other threads continue being monitored. The stop and fallback history survive a completed turn during the run, so an intervening success does not permit an endless fallback/retry cycle. A manual resume is left alone if the thread is already working when the stop check runs.

The panel shows `Stopped` and disables its retry countdown. Events explain why the thread stopped. This does not delete or archive the conversation, and users can still manage it manually in Codex. Dry-run mode logs the proposed switch/recovery without changing settings, resuming or pausing a Goal, or claiming the model actually changed.

### Recovery history

Each recovery records its time, thread/turn ID, action, normalized error category, retry mode and outcome **in memory only**. Nothing is saved to a history file. History and counters are discarded when the program exits. With `-v`, recovery records and backoff countdowns are printed to the terminal.

## Live terminal status

An interactive terminal displays a live panel below the scrolling event log. It refreshes once per second, including while the monitor waits between polls or for an RPC response. Events include local timestamps with the date and UTC offset, for example `[2026-10-28 22:37:36+1300]`.

The panel shows:

- Active, paused/waiting, ready and tracked thread counts (tracked root threads only).
- Distinct errors and total recovery attempts, successes and failures.
- Each thread's state, configured model, reasoning level, Prompt/Goal mode, recovery counts, retry mode and next-retry countdown.
- Poll interval, next poll, age of the last completed poll, uptime and current RPC activity.

“Immediate” means an eligible new failed turn can be recovered as soon as polling detects it; “15m” means the thread is in slow retry mode. The poll interval remains five seconds by default. “Success” requires an observed completed recovery turn; accepting a command or becoming active does not count. Counts combine Continue and Goal resume for each thread; the exit summary separates them. Dry-run plans are labelled separately and do not inflate real attempt or success counters.

The panel uses cyan for working threads, green for ready threads and completed recoveries, yellow for backoff or input requests, and red for errors or attention needed. State labels remain visible with colour disabled. `Done` counts completed recovery turns, not completed tasks. `Paused` includes threads waiting for input, in backoff, stopped on an error, or otherwise not working or ready.

Each thread also shows its configured model and reasoning level under its name, and `Prompt`, `Goal active`, `Goal paused`, `Goal blocked`, or a Goal-limit state under its status. These are the daemon's current configured settings, not independent per-token execution telemetry. An unset reasoning level is labelled `default`; unavailable model/mode values are marked `?`. Settings and Goal state refresh on each poll, including after manual changes.

Every row includes a short unique ID suffix, so identical or shortened titles remain distinguishable. Long names, including Unicode names, are shortened by terminal-cell width. Threads needing attention appear first, followed by errors, backoff, working and ready/paused threads. This is display ordering only; it does not change recovery decisions or polling.

Logs appear above the panel, including with `-v`. Wide terminals show separate counters; medium terminals combine them under `SENT/OK/FAIL`. Below 76 columns, thread cards replace the table. Metadata uses additional rows, so page capacity adapts to the available height. Very short terminals use a compact rotating view. When threads do not all fit, pages rotate every eight seconds with a page indicator; totals include every tracked thread, including those on other pages. Dry-run rows display planned operations instead of real submissions, labelled `PLANS` or `PLAN/OK/FAIL`. The panel is removed before the Ctrl+C summary. Output redirected to a file or pipe, and terminals with `TERM=dumb`, automatically use plain timestamped logs without control sequences. To disable the panel explicitly:

```bash
python codex_multi_recovery.py --no-live -v
```

After updating an existing installation, install the new display dependency:

```bash
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python codex_multi_recovery.py
```

## Ctrl+C run summary

The summary includes distinct errors observed, total commands attempted, and separate `Continue` and `Goal resume` rows:

| Column | Meaning |
| --- | --- |
| Attempts | Real recovery commands attempted |
| Successful | A subsequent recovery turn was observed completed |
| Failed | The recovery turn failed, or the server explicitly rejected submission |
| Unresolved | Still running, not observed yet, superseded without a known result, or submission outcome unknown |
| Interrupted | The recovery turn was observed interrupted |
| Dry-run | Planned operations that were not sent |

A server accepting a request is not counted as a completed recovery. For Goal resume, attribution uses the first subsequent turn in the observed history; unavailable history leaves the result unresolved. Repeated polls do not inflate the error count. Internal automatic retries are not separate failed turns. Dry-run plans are not counted as real attempts.

## Command-line options

```bash
python codex_multi_recovery.py --help
```

| Option | Purpose | Default |
| --- | --- | --- |
| `-v`, `--verbose` | Detailed RPC activity, thread states, decisions and retry records | Off |
| `--no-live` | Disable the live panel; keep timestamped logs | Off |
| `--dry-run` | Log planned operations without sending recovery commands | Off |
| `--check` | Initialize, list loaded thread IDs, and exit without recovery | Off |
| `--interval SECONDS` | Normal polling interval; must be positive | `5` |
| `--timeout SECONDS` | Connection/RPC timeout; must be positive | `30` |
| `--socket PATH` | Shared daemon's Unix socket | See below |
| `--url ws://127.0.0.1:PORT` | Connect to an existing local WebSocket endpoint instead of the Unix socket | Unset |

The default socket is `$CODEX_HOME/app-server-control/app-server-control.sock`, or `~/.codex/app-server-control/app-server-control.sock` when `CODEX_HOME` is unset. Example override:

```bash
python codex_multi_recovery.py --socket /path/to/app-server-control.sock -v
```

`--interval` does not change the fixed 15-minute slow retry interval. The watcher never starts a replacement private app server.

## Troubleshooting

- **Missing `websocket` module:** activate `.venv` and run `python -m pip install -r requirements.txt`. The package name is `websocket-client`, not `websocket`.
- **Missing socket or connection refused:** open `codex agents`, check that you are using the same OS user and `CODEX_HOME`, then rerun `--check -v`. Use `--socket` for a nonstandard socket location.
- **Zero loaded threads:** start or resume a task on the shared daemon. Old stored conversations are not loaded working threads.
- **Failed thread is not tracked:** a thread must be observed working during this watcher run. Starting the watcher after it has already failed does not enroll it. Start the watcher first, then resume the thread manually.
- **Backoff messages without submissions:** the thread is intentionally waiting for its 15-minute retry deadline. The verbose log shows the remaining time.
- **RPC timeout/disconnection:** the watcher closes an ambiguous transport connection instead of rapidly resending a possibly accepted operation. Restore the daemon connection and restart the watcher while your threads are working. Automatic reconnection is not implemented.

Verbose logs can include conversation titles, provider URLs and error messages. Inspect logs before sharing them.

## Development checks

```bash
python -m unittest discover -s tests -v
```

## Validation and limits

A real macOS test with Codex 0.164.0 confirmed Goal resume requests accepted for two threads after 503 and model-not-found failures, followed by both threads becoming active. Local real-server checks also passed WebSocket initialization and thread listing. Simulated tests covered two-thread error handling, deduplication, virtual-clock backoff timing, successful-turn reset, quota eligibility and summary accounting. Display checks cover terminal sizes from 24×8 to 150×32, 40 threads rotating through every page, long Unicode names, colliding ID suffixes, monochrome output, dry-run counters, timestamp sanitizing, and a real pseudo-terminal run with live refresh, interleaved events and Ctrl+C cleanup. The display tests use a simulated daemon; they do not establish new live-provider recovery coverage.

A Linux end-to-end test on 2026-10-28 used the production monitor in a real pseudo-terminal, Codex 0.164.0 over localhost WebSocket, and real streamed `gpt-6-luna` requests at `low` reasoning through the user's authorized provider. A dynamic wait tool kept the thread active for 65 seconds, with 13 active-status samples and continuous panel refresh. After the initial turn completed, a local forwarding relay injected exactly one HTTP 503. The monitor observed one failed turn, sent one `Continue`, and observed the recovery turn complete. The Ctrl+C summary reported one Continue attempt, one success, zero failures and zero unresolved attempts; cursor restoration also passed. The provider's upstream service was not interrupted. CLI request retries were disabled for this controlled fault test.

A further real Codex 0.164.0 protocol test used two threads and a local HTTP endpoint returning the exact configuration-update error. It confirmed `gpt-6.1-sol/high` → `gpt-6-sol/high` for a prompt and `gpt-6-astra/max` → `gpt-6-sol/max` for a Goal. The outgoing fallback request bodies retained those reasoning levels. Each thread received one recovery; the second failure disabled further recovery and left the Goal paused. Request counts remained unchanged during an additional eight-second observation. This was a deterministic local fault test, not evidence of a real provider usage-limit reset. Simulated tests also cover recurrence after an intervening success, rejected/unconfirmed switches, unset-effort resolution and dry-run isolation.

The sandbox's earlier `thread/start` hang was isolated to project-instruction discovery: setting `project_doc_max_bytes = 0` in the empty, isolated test workspace allowed startup in about 0.03 seconds. This was a test-environment configuration, not a change to the watcher or a recommended setting for user projects.

This Linux live test covers Continue recovery over localhost WebSocket; it does not establish Linux Unix-socket or Goal-resume coverage. The 15-minute policy has not been tested through a live five-hour quota reset. Treat compatibility with future Codex releases as unverified.

Threads that start and fail between polls can be missed. Summary outcomes reflect observed turns rather than proof that an entire Goal or task finished. Restarting clears all memory and retry timers; it does not automatically enroll already-failed threads. Ctrl+C prints a summary; forced termination may prevent one from being printed.
