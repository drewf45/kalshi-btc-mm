"""Operator reset — clear halt state. Run as: python -m k_worker.reset"""

import logging

from . import store, discipline, notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)

def main():
    store.init_db()
    try:
        notify.init()
    except Exception:
        pass
    reason = discipline.halt_reason()
    if reason:
        print(f"Engine is halted: {reason}")
        discipline.reset()
        print("Engine reset complete.")
    else:
        print("Engine is not halted — nothing to do.")


if __name__ == "__main__":
    main()
