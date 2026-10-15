# Kal — the Rotation Desk on your Mac

WO-ROT-1 Chunk 6. The desk runs as a macOS launch agent: it starts when you
log in, restarts if it ever exits, and keeps the Mac awake while it runs.

## Install (one line, in Terminal, from the repo folder)

```bash
bash mac/install.sh
```

The first run creates `~/Library/Application Support/Kal/.env` (mode 600) and
stops. Open it, paste your Kalshi key, Telegram token and chat id, then run the
same line again. It starts the desk.

## Where things live

| What | Where |
| --- | --- |
| Secrets | `~/Library/Application Support/Kal/.env` (must stay `chmod 600`; never committed) |
| Database | `~/Library/Application Support/Kal/kal.db` |
| Heartbeat | `~/Library/Application Support/Kal/heartbeat.json` |
| Logs (daily, 14 kept) | `~/Library/Logs/Kal/kal.log` |

## Everyday commands

```bash
# is it alive? (exit 0 fresh, 1 stale > 10 s, 2 missing)
.venv/bin/python -m relay_engine.deadman

# follow the log
tail -f ~/Library/Logs/Kal/kal.log

# stop and remove the agent (data kept)
bash mac/install.sh uninstall
```

## Keep it awake — read this

- The agent runs under `caffeinate -i -s`. **`-s` only holds on AC power**, and
  **nothing stops a closed laptop lid from sleeping the Mac.** Keep it plugged
  in with the lid open (or a desktop / clamshell with an external display).
- The boot tape prints the power source. **On battery the desk pages you and
  stops new entries**; back on AC, entries resume.
- After any sleep (or a stall of 30 s or more) the desk treats the wake as a
  boot: it halts entries, logs the gap and every position whose stops went
  unwatched, reconciles from Kalshi, and only then resumes entries.

## Going live

The desk is born in SHADOW (nothing is placed). Going live is your act:
`RUN_MODE=LIVE` plus the `I_UNDERSTAND_LIVE` phrase in `.env`, after the shadow
day's ROT sheet is above break-even (WO-ROT-1 Chunk 8).
