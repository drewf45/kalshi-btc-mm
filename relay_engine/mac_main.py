"""WO-ROT-1 Chunk 6 — the Mac host entrypoint (launchd runs this).

    caffeinate -i -s python -m relay_engine.mac_main

Order matters: the environment is settled BEFORE relay_engine.config is
imported (config reads the environment once, at import).

  1. Secrets come from ~/Library/Application Support/Kal/.env, which must be
     mode 600 (owner read/write only). Anything looser is a boot-stop: a key
     file other users can read is a leaked key.
  2. Defaults for the Mac: RELAY_DB_PATH = .../Kal/kal.db, HEARTBEAT_PATH =
     .../Kal/heartbeat.json (an explicit value in .env or the environment wins).
  3. Logs: ~/Library/Logs/Kal/kal.log, rotated daily, 14 days kept; stdout and
     stderr (the boot tape, print()) ride the same rotating file.
  4. Then the runner — whose boot prints the power source, reconciles from
     the venue before any entry, and treats every sleep/wake gap as a boot.
"""

import logging
import logging.handlers
import os
import stat
import sys

APP_DIR = os.path.expanduser("~/Library/Application Support/Kal")
LOG_DIR = os.path.expanduser("~/Library/Logs/Kal")


def load_env_file(path: str) -> int:
    """KEY=VALUE lines into os.environ (existing variables win). Refuses a
    file readable by group/other. Returns the number of keys loaded."""
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"FATAL: {path} is mode {oct(st.st_mode & 0o777)} — "
                         f"secrets must be 600: chmod 600 '{path}'")
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v   # auth normalizes a literal \\n PEM
                n += 1
    return n


class _LogStream:
    """print() -> the rotating log (the boot tape lands next to the logs)."""

    def __init__(self, logger, level):
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self.logger.log(self.level, line)

    def flush(self):
        if self._buf:
            self.logger.log(self.level, self._buf)
            self._buf = ""


def setup_logging(log_dir: str = LOG_DIR) -> None:
    os.makedirs(log_dir, exist_ok=True)
    h = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "kal.log"), when="midnight", backupCount=14)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    sys.stdout = _LogStream(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR)


def prepare_environment(app_dir: str = APP_DIR) -> None:
    os.makedirs(app_dir, exist_ok=True)
    env_path = os.path.join(app_dir, ".env")
    if os.path.exists(env_path):
        load_env_file(env_path)
    os.environ.setdefault("RELAY_DB_PATH", os.path.join(app_dir, "kal.db"))
    os.environ.setdefault("HEARTBEAT_PATH", os.path.join(app_dir, "heartbeat.json"))


def main() -> None:
    prepare_environment()
    setup_logging()
    import asyncio
    from relay_engine import shadow_runner      # config imports now, env settled
    asyncio.run(shadow_runner.run())


if __name__ == "__main__":
    main()
