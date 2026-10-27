# Codex multi-thread recovery — experimental

Python 3.9+ and Codex CLI 0.164.0 are required. This uses the same shared local app-server daemon as `codex agents`; it does not require tmux. Threads on a private app-server or a different computer are not visible. Start `codex agents` at least once so the daemon and your working threads are running. Use the same user account and `CODEX_HOME` as Codex.

Run:

```sh
python3 codex_multi_recovery.py --dry-run
```

The monitor polls every five seconds and tracks root threads first observed active, then checks their latest turn after they become idle. It ignores inactive historical threads. Once a failed turn is detected, it logs one intended recovery per thread and turn. It refuses safety, permission, and quota-related failures. Use Ctrl-C to stop.

After inspecting dry-run output, restart without `--dry-run` to enable recovery:

```sh
python3 codex_multi_recovery.py
```

This is a prototype: simulated two-thread tests passed, but real multi-thread recovery has not been validated. A real app-server test in the build environment timed out starting a thread. Goal resume uses `thread/goal/set` with `status=active`; non-goal recovery uses `turn/start` with `Continue`. These are protocol actions, not keystrokes; they have not been confirmed equivalent to the UI on your setup. The monitor may miss a thread that starts and fails between polls or one that is already failed at startup. It only attempts a given turn once per process, including when the request times out, to avoid sending duplicates. Persistent repeated failures can still cause repeated new turns and API usage. Use dry-run first.
