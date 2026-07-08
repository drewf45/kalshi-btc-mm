"""One-shot CLI: confirm a treasury payout after manual withdrawal.

Usage:
  python -m k_worker.treasury_paid 0.64    (confirm withdrawal of $0.64)
  python -m k_worker.treasury_paid          (confirm full accrued amount)
"""

import sys
from . import store, notify, treasury


def main():
    store.init_db()
    try:
        notify.init()
    except Exception:
        pass
    treasury.init_book()

    t = treasury.get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]

    amount = None
    if len(sys.argv) > 1:
        try:
            amount = float(sys.argv[1])
        except ValueError:
            print(f"Invalid amount: {sys.argv[1]}")
            sys.exit(1)

    if owed < 0.01 and amount is None:
        print(f"Nothing accrued (tax=${t['accrued_tax']:.3f} fee=${t['accrued_fee']:.3f})")
        return

    if amount is not None:
        print(f"Confirming payout: ${amount:.2f} (accrued: tax=${t['accrued_tax']:.3f} "
              f"fee=${t['accrued_fee']:.3f} total=${owed:.3f})")
    else:
        print(f"Confirming full accrued: tax=${t['accrued_tax']:.3f} "
              f"fee=${t['accrued_fee']:.3f} total=${owed:.3f}")

    treasury.mark_paid(amount)
    print("Done. Accrued moved to lifetime paid. Invariant should return green.")


if __name__ == "__main__":
    main()
