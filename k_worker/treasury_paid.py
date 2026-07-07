"""One-shot CLI: mark treasury payout as completed after manual withdrawal."""

from . import store, notify, treasury


def main():
    store.init_db()
    try:
        notify.init()
    except Exception:
        pass
    treasury.init_book()
    t = treasury.get_totals()
    total = t["accrued_tax"] + t["accrued_fee"]
    if total < 0.01:
        print(f"Nothing accrued (tax=${t['accrued_tax']:.3f} fee=${t['accrued_fee']:.3f})")
        return
    print(f"Marking payout: tax=${t['accrued_tax']:.3f} fee=${t['accrued_fee']:.3f} total=${total:.3f}")
    treasury.mark_paid()
    print("Done. Accrued moved to paid.")


if __name__ == "__main__":
    main()
