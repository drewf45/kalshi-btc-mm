# f_worker/__main__.py
# Boot: envcheck (FATAL loud) -> instance lock -> ledger init -> config echo -> threads.
#   desk loop      : discover + run each 15M window (LIVE FROM BOOT)
#   pack scheduler : hourly pack (§5)
#   halt listener  : Telegram /fhalt + /fstatus ONLY (W5 — the phone can halt, never excite)
#
# One package, one job, no other option on the box: `python -m f_worker`.

import os
import sys
import threading
import time

from .config import load_config, fenvcheck, FatalConfigError
from .ledger import Ledger
from .notify import Notifier
from .fgateway import FGateway
from .pricebrain import PriceBrain
from .manager import Manager
from .desk import Desk
from .fpack import PackScheduler, render_pack


def _acquire_lock(path: str):
    """Single-instance lock (borrow pattern). Returns the held file handle or exits."""
    try:
        import fcntl
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except Exception as e:
        print(f"FATAL: could not acquire instance lock {path}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


class Supervisor:
    """3-strikes thread supervisor (borrow pattern from d_worker/__main__.py)."""

    def __init__(self, notifier):
        self.notifier = notifier
        self.threads = {}

    def add(self, name, target):
        self.threads[name] = {"target": target, "thread": None, "strikes": 0}

    def _spawn(self, name):
        t = threading.Thread(target=self.threads[name]["target"], name=name, daemon=True)
        self.threads[name]["thread"] = t
        t.start()

    def run(self):
        for name in self.threads:
            self._spawn(name)
        while True:
            time.sleep(5)
            for name, rec in self.threads.items():
                t = rec["thread"]
                if t is not None and not t.is_alive():
                    rec["strikes"] += 1
                    msg = f"thread '{name}' died (strike {rec['strikes']}/3)"
                    print(f"[supervisor] {msg}", flush=True)
                    if self.notifier:
                        self.notifier.alert(msg)
                    if rec["strikes"] >= 3:
                        if self.notifier:
                            self.notifier.alert(f"FATAL: thread '{name}' exceeded 3 strikes; exiting")
                        print(f"FATAL: thread '{name}' exceeded 3 strikes", file=sys.stderr, flush=True)
                        os._exit(1)
                    self._spawn(name)


def main() -> None:
    print("BOOT: f_worker (flipdesk) starting", flush=True)
    cfg = load_config()
    try:
        fenvcheck(cfg)
    except FatalConfigError:
        sys.exit(1)

    _acquire_lock(cfg.lock_path)
    ledger = Ledger(cfg.db_path)

    # W7 clearance: DW_CLEAR_HALT=1 boot clears a standing flip_halt.
    if cfg.clear_halt:
        ledger.set_halt(False, "cleared by DW_CLEAR_HALT=1 boot")
        print("BOOT: flip_halt cleared by DW_CLEAR_HALT=1", flush=True)

    notifier = Notifier(cfg.tg_token, cfg.tg_chat_id)
    for line in cfg.echo_lines():
        notifier.send(line)

    # The ONLY place the crypto-signing client is constructed.
    from .lib.kalshi import KalshiClient
    client = KalshiClient(cfg.api_base, cfg.api_prefix, cfg.api_key_id, cfg.private_key_pem_b64)

    gateway = FGateway(client, ledger, cfg, notifier)
    pricebrain = PriceBrain(cfg)
    manager = Manager(gateway, ledger, pricebrain, cfg, notifier)
    desk = Desk(client, gateway, manager, pricebrain, ledger, cfg, notifier)

    pack = PackScheduler(ledger, cfg, notifier, client)
    pack.start()

    def halt_listener():
        notifier.listen_loop(
            on_halt=lambda: ledger.set_halt(True, "phone /fhalt"),
            on_status=lambda: render_pack(ledger, cfg, client),
            stop_flag=lambda: False,
        )

    sup = Supervisor(notifier)
    sup.add("desk", desk.run_forever)
    sup.add("halt_listener", halt_listener)
    sup.run()


if __name__ == "__main__":
    main()
