# f_worker — FLIPDESK.
# One package, one job, one shot per window, one dollar until the ledger says two.
# Live from the first bell because the floor — not courage — carries the tuition.
#
# Entry point: `python -m f_worker` (see __main__.py). The weather desk (d_worker) is
# permanently retired for now and does not run on this branch.

__all__ = [
    "config", "feemath", "ledger", "notify", "fgateway",
    "pricebrain", "window", "manager", "desk", "fpack",
    "reconcile", "settlement", "feewatch",
]
