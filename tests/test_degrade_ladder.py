"""VERIFY GATE 3: WS degrade ladder fire-drill — kill the socket mid-shadow,
watch halt -> REST custodian -> snapshot resync -> resume after 10 clean frames."""

import json

import pytest

from relay_engine import config
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder, Feed, FeedState


def snapshot_frame(market="KXBTC15M-TEST", yes=((45, 100),), no=((52, 80),)):
    return json.dumps({"type": "orderbook_snapshot",
                       "msg": {"market_ticker": market, "yes": list(yes), "no": list(no)}})


def delta_frame(market="KXBTC15M-TEST", side="yes", price=46, delta=10):
    return json.dumps({"type": "orderbook_delta",
                       "msg": {"market_ticker": market, "side": side,
                               "price": price, "delta": delta}})


def test_fire_drill_full_ladder_walk():
    transitions = []
    ladder = DegradeLadder(on_transition=lambda o, n: transitions.append((o, n)))
    feed = Feed(ladder)

    # mid-shadow: live and clean
    feed.handle_frame(snapshot_frame(), now=1.0)
    assert ladder.entries_allowed()
    assert ladder.custodian_transport() == "WS"

    # KILL THE SOCKET
    feed.socket_died("fire drill")
    assert ladder.state is FeedState.WS_LOST
    assert not ladder.entries_allowed()            # ALL-lane entry halt
    assert ladder.custodian_transport() == "REST"  # custodian on REST

    # frames without a snapshot do NOT start the clean count
    feed.handle_frame(delta_frame(), now=2.0)
    assert ladder.state is FeedState.WS_LOST

    # snapshot resync
    feed.handle_frame(snapshot_frame(), now=3.0)
    assert ladder.state is FeedState.RESYNCING
    assert not ladder.entries_allowed()  # still halted while counting

    # 9 clean frames: not enough (snapshot itself counted as frame 1)
    for i in range(config.WS_RESUME_CLEAN_FRAMES - 2):
        feed.handle_frame(delta_frame(price=40 + i), now=4.0 + i)
    assert ladder.state is FeedState.RESYNCING

    # 10th clean frame: resume
    feed.handle_frame(delta_frame(price=30), now=20.0)
    assert ladder.state is FeedState.WS_LIVE
    assert ladder.entries_allowed()
    assert ladder.custodian_transport() == "WS"

    assert transitions == [("WS_LIVE", "WS_LOST"), ("WS_LOST", "RESYNCING"),
                           ("RESYNCING", "WS_LIVE")]


def test_risk_reduction_allowed_in_every_state():
    ladder = DegradeLadder()
    for state in FeedState:
        ladder.state = state
        assert ladder.custodian_transport() in ("WS", "REST")  # never None/forbidden


def test_garbled_frame_is_transport_damage():
    ladder = DegradeLadder()
    feed = Feed(ladder)
    feed.handle_frame("{not json", now=1.0)
    assert ladder.state is FeedState.WS_LOST


def test_venue_error_frame_fails_loud():
    feed = Feed(DegradeLadder())
    with pytest.raises(FatalIntegrityError):
        feed.handle_frame(json.dumps({"type": "error", "msg": {"code": 6, "msg": "bad"}}))


def test_staleness_stamp():
    ladder = DegradeLadder()
    feed = Feed(ladder)
    feed.handle_frame(snapshot_frame(), now=100.0)
    book = feed.book("KXBTC15M-TEST")
    assert not book.is_stale(now=101.0, limit_seconds=config.STALENESS_LIMIT_SECONDS)
    assert book.is_stale(now=106.0, limit_seconds=config.STALENESS_LIMIT_SECONDS)
