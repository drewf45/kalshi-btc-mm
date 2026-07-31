"""VERIFY GATE 2 (part 1): boot tape prints EPOCH 2 header, ratification
assumptions, DREW-DEFAULT constants, SHADOW mode, recorder confirmation."""

from relay_engine import config
from relay_engine.boot import boot_tape
from relay_engine.ops import Recorder


def test_boot_tape_contents(ledger):
    caps = ledger.snapshot_caps_at_boot()
    rec = Recorder(ledger)
    rec.record("KXBTC15M-TEST", "{}", ts=1.0)
    tape = "\n".join(boot_tape(recorder=rec, boot_caps=caps))

    assert "EPOCH 2" in tape
    assert "RUN_MODE=SHADOW" in tape
    assert "PAPER SHADOW: zero capital, zero orders" in tape
    for ratification in config.RATIFICATIONS_ASSUMED:
        assert f"ASSUMED: {ratification}" in tape
    assert tape.count("ASSUMED:") == 5
    # All three A3 defaults, tagged
    assert "DREW-DEFAULT at_risk_cap_per_settlement_event = 3x one-lot max loss" in tape
    assert "DREW-DEFAULT lane_d_floor = 60c" in tape
    assert "DREW-DEFAULT depth_fraction = 25%" in tape
    # Printed number IS the enforced number
    assert f"bucket={config.RATE_BUCKET_CAPACITY} tokens" in tape
    assert f"refill={config.RATE_REFILL_PER_SECOND}/s" in tape
    assert "RECORDER: ON" in tape and "[confirmed writing]" in tape
    assert "replay harness + shadow verdicts" in tape  # streams name their readers
    assert "single-writer" in tape


def test_live_submit_hard_disabled_by_default():
    assert config.RUN_MODE == "SHADOW"
    assert not config.live_submit_enabled()
