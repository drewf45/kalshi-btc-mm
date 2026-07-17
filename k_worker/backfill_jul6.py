"""One-time backfill for Jul 6-7 tape (18 settlements: 17W/1L).

Run as: python -m k_worker.backfill_jul6

Applies aggregate treasury waterfall for the 17 wins (+$0.59 net) and
1 loss (-$0.97) that were invisible to the engine due to fill-blindness.
Expected post-state: tax $0.177, fee $0.030, book $11.79.
Idempotent: refuses to run if accrued_tax > 0 (already applied).
"""

import logging

from . import store, treasury, discipline, notify

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
    treasury.init_book()

    t = treasury.get_totals()
    if t["accrued_tax"] > 0.001:
        print(f"Backfill already applied (accrued_tax=${t['accrued_tax']:.3f}). Skipping.")
        return

    WIN_TOTAL_PNL = 0.59
    LOSS_PNL = -0.97

    print(f"Pre-backfill: book=${t['engine_book']:.2f} tax=${t['accrued_tax']:.3f} fee=${t['accrued_fee']:.3f}")

    split = treasury.waterfall(WIN_TOTAL_PNL)
    print(f"Applied 17 wins (aggregate pnl=${WIN_TOTAL_PNL}): {split['treasury_line']}")

    treasury.record_loss(LOSS_PNL)
    print(f"Applied 1 loss (pnl=${LOSS_PNL})")

    for _ in range(17):
        discipline.record_win()
    discipline.record_loss()

    t = treasury.get_totals()
    print(f"\nPost-backfill state:")
    print(f"  Engine book:  ${t['engine_book']:.2f}")
    print(f"  Accrued tax:  ${t['accrued_tax']:.3f}")
    print(f"  Accrued fee:  ${t['accrued_fee']:.3f}")
    print(f"  Owed to Drew: ${t['accrued_tax'] + t['accrued_fee']:.3f}")

    notify.send(
        f"📋 BACKFILL: Jul 6-7 tape (18 settlements: 17W/1L)\n"
        f"tax ${t['accrued_tax']:.3f} · fee ${t['accrued_fee']:.3f} · "
        f"book ${t['engine_book']:.2f} · owed ${t['accrued_tax'] + t['accrued_fee']:.3f}"
    )


if __name__ == "__main__":
    main()
