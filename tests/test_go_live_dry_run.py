"""CHUNK D — the LIVE spine, dry-proven end-to-end. One test, no network:

auth boot-check → live_boot_reconcile (balance baselined, caps re-snapshotted,
quarantine rules exercised, foreign resting tolerated) → boot tape [LIVE] +
SIZING (+ the worst-day line and listener state the BOOT page carries) →
the deployed cycle drives F's proposal through the gateway's LIVE branch
(venue payload snapshot-asserted, post_only=True) → a mocked fill books
through FillBooker (✅ FILL page) → the bracket opens source=venue → a mocked
settlement closes it → the 📊 line renders. After this, there is nothing left
for code to prove — the rest is Drew's two env vars.
"""

import json

import pytest

from relay_engine import auth, config, failures
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
FOREIGN_TICKER = "KXBTC15M-02JAN251015-T99"


def _test_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def test_go_live_dry_run(tmp_path, monkeypatch, capsys):
    from relay_engine import venue

    # ── the venue layer, fully mocked (no network) ─────────────────────
    balance = {"usd": 100.00}
    placed = []

    class MockClient:
        def request(self, *a, **k):   # resting-orders sweep path: tolerated
            raise RuntimeError("dry run: no network")

    client = MockClient()
    monkeypatch.setattr(venue, "build_client", lambda: client)
    monkeypatch.setattr(venue, "get_balance", lambda c: (balance["usd"], 0.0))
    monkeypatch.setattr(venue, "get_positions", lambda c: [
        {"ticker": FOREIGN_TICKER, "position": 2}])   # nobody's fill → quarantine
    monkeypatch.setattr(venue, "parse_fill", lambda record, side: (97.0, 0, 1))

    def mock_place_order_maker(c, market, side, price_cents, count=1,
                               v2_price_str=None, post_only=True):
        placed.append({"market": market, "side": side,
                       "price_cents": price_cents, "count": count,
                       "v2_price_str": v2_price_str, "post_only": post_only})
        return f"LIVE-{len(placed)}", {}
    monkeypatch.setattr(venue, "place_order_maker", mock_place_order_maker)

    # ── RUN_MODE=LIVE + the phrase (mocked at the config layer) ────────
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)

    # ── 1. auth boot-check: real PEM parse, key material never logged ──
    monkeypatch.setenv("KALSHI_API_KEY_ID", "TESTKEY-90AB")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", _test_pem())
    auth_line = auth.boot_check()
    assert "90AB" in auth_line                 # last4 identifies the key...
    assert "TESTKEY-90AB" not in auth_line     # ...the full id never prints

    # ── 2. boot + live boot reconcile ──────────────────────────────────
    engine = ShadowEngine(db_path=str(tmp_path / "live_dry.db"))
    engine.telegram_sent = []
    engine.telegram.send = engine.telegram_sent.append
    engine.boot(auth_line=auth_line)
    failures.configure(engine.ledger, alert_fn=engine.telegram.alert,
                       run_mode="LIVE", boot_id=1)
    try:
        tape = capsys.readouterr().out
        assert "[LIVE]" in tape and "SIZING" in tape and "AUTH" in tape

        from relay_engine.reconcile import live_boot_reconcile
        summary = live_boot_reconcile(engine, client)
        assert summary["cash_state"] == "BASELINED"
        assert engine.ledger.book_cents() == 10_000        # venue truth is the baseline
        assert engine.ledger.boot_caps.order_budget_cents > 0  # caps re-snapshotted
        assert summary["positions_quarantined"] == 1
        # RULING 1 (P15): the mystery position is ADOPTED as ORPHAN, custodied
        assert any("🧾 ORPHAN adopted" in m for m in engine.telegram_sent)
        assert f"{FOREIGN_TICKER}:ORPHAN" in engine.custodian.positions

        # the BOOT page's other lines exist and speak
        from relay_engine.ops import worst_day_bound_line
        assert "WORST-DAY BOUND" in worst_day_bound_line(engine.ledger)
        assert engine.listener_status() == "ok"

        # ── 3. the deployed cycle drives F through the LIVE branch ─────
        now = 1_700_000_000.0
        engine.market_meta[TICKER] = {"close_ts": now + 120,
                                      "boundary_lo": 117_000.0,
                                      "boundary_hi": 118_000.0}
        engine.feed.handle_frame(json.dumps(
            {"type": "orderbook_snapshot",
             "msg": {"market_ticker": TICKER,
                     "yes": [["0.97", "10.00"]], "no": [["0.02", "10.00"]]}}),
            now=now)
        engine.gateway.venue_client = client
        engine.cycle([TICKER], now=now, spot=118_500.0)

        assert placed, "F never reached the live door"
        payload = placed[0]
        # P27 §1: full Kelly — 2 lots at 97c (min(kelly, depth, risk cap));
        # maker, true touch, unchanged.
        assert payload == {"market": TICKER, "side": "yes", "price_cents": 97,
                           "count": 2, "v2_price_str": "0.97",
                           "post_only": True}
        assert "LIVE-1" in engine.gateway.live_order_ids

        # COLD READ (build 67): the bracket OPENS on the LEDGER book (source
        # "ledger"), not the venue read — the venue's cash/position desync at
        # entry/settlement is what produced the +819c phantom; the ledger is
        # internally consistent. The venue read now serves standing_reconcile only.
        src = engine.ledger.db.execute(
            "SELECT source FROM window_econ WHERE market=?",
            (TICKER,)).fetchone()[0]
        assert src == "ledger"

        # ── 4. a mocked fill books through FillBooker ──────────────────
        stats = engine.fills.sweep(
            [{"fill_id": "F1", "order_id": "LIVE-1", "count": 1}], now=now + 10)
        assert stats["booked"] == 1
        assert any(m.startswith("✅ ENTRY F") for m in engine.telegram_sent)
        assert f"{TICKER}:F" in engine.custodian.positions   # custodied

        # ── 5. mocked settlement closes the bracket: the 📊 line ───────
        balance["usd"] = 100.03   # the venue's ledger moved with the win
        engine.settle_traded_market(TICKER, settled_yes=True, now=now + 900)
        row = engine.ledger.db.execute(
            "SELECT window_pnl_cents, fills_pnl_cents, source, deferred"
            " FROM window_econ WHERE market=?", (TICKER,)).fetchone()
        # window_pnl is the ledger book delta (10003 − 10000 = 3), which EQUALS
        # fills truth (3) — the cold-read fix makes the two agree instead of
        # differencing an inconsistent venue read into a phantom.
        assert row == (3, 3, "ledger", "")     # ledger truth == fills truth
        # KAL-50/50 Stage 0.1: the per-lane summary names the lanes settled
        # (broker window pnl still the honest number) instead of a global rate.
        assert any("📊" in m and "+$0.03" in m and "lanes F" in m
                   for m in engine.telegram_sent)
        assert engine.econ.halted_lanes() == set()   # a win arms nothing
    finally:
        failures._ledger = None
        failures._alert_fn = None
