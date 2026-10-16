"""tools — read-only, offline-of-the-engine analysis utilities.

These live OUTSIDE relay_engine/ on purpose: nothing here is part of the trading
engine. Modules here perform GET-only API reads and never place/amend/cancel
orders or write to engine state.
"""
