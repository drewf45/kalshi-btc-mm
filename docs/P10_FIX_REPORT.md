# P10 FIX REPORT — the 01:25 crossed book (yes 97 + no 55 = 152)

## §1.2 EVIDENCE STATEMENT (read-rule: an honest gap beats a false conviction)

The recorder banked the 01:25 window's frames into `book_snapshots` — in the
**deployed worker's database** (`RELAY_DB_PATH`, `/var/data/relay_shadow.db` on
Render). That disk is not in this repo checkout, so the VERBATIM corrupting
frame cannot be cited from here. The conviction tool is committed and ready:

    python -m scripts.autopsy_replay /var/data/relay_shadow.db KXBTC15M-26JUL172130-30

Run it in the Render shell. It starts at the window's banked snapshot (A1 —
refusing to replay mid-delta-stream), replays every frame through BOTH the
deployed v7 applier and the fixed pipeline, flags the first frame where the
v7 sum exceeds 101, and prints that frame VERBATIM for §1.4's permanent unit
test. If no snapshot was banked for the window, it states that evidence gap
and refuses a false conviction.

## §1.3 THE CANDIDATE CLASSES, PROSECUTED AGAINST THE v7 CODE

Three real holes were found by inspection. Each deterministically produces
book corruption; each is now closed and carries a reconstructed-frame
regression test (`tests/test_p10_book_truth.py`).

**HOLE 1 — the defaulted side (v7 feed.py: `m.get("side", "yes")`). LEADING
SUSPECT.** The delta path pulled price and delta through no-default key
ladders (P6 law) but defaulted a missing/unrecognized side to `"yes"`. One
side-less (or side-key-variant) delta at 0.97 lands on the YES book of an
honest yes45/no55 market and produces EXACTLY the observed lie: yes_bid=97
AND no_bid=55, sum 152. One frame, deterministic, matches the tape shape.
FIX: `SIDE_KEYS` ladder + `_parse_side` (yes/no, case-insensitive, NOTHING
else); a side-less delta is a banked `FRAME_SHAPE_UNKNOWN`, book dropped,
resync forced — the no-defaults law now covers all three fields.

**HOLE 2 — delta-built books without a snapshot foundation (v7 `drop_book`
path).** After any shape failure dropped a market's book, the next delta
recreated it EMPTY and kept applying — a book reconstructed from deltas alone
is fiction (it is missing all pre-drop state) and can cross arbitrarily. The
v5 crash tape proves shape failures fired live, so this path RAN live.
Worse: `snapshot_resynced()` fires on ANY market's snapshot and
`clean_frame()` counts ALL frames, so entries resumed while the dropped
market's book was still delta-built fiction.
FIX: `OrderBook.has_snapshot`; a delta with no snapshot foundation is
refused, banked once per episode (`DELTA_WITHOUT_SNAPSHOT`), and the market
is queued for snapshot resubscribe.

**HOLE 3 — no sequence checking.** The venue stamps `seq` on delta frames;
v7 ignored it. A missed delta (send-queue drop, slow consumer) silently
drifts the book — the crossed state is one missed removal away.
FIX: seq tracked per book; a gap drops the book, banks `BOOK_SEQ_GAP`, and
forces resync. (Frames without seq skip the check — dialect-tolerant.)

**Ruled out by inspection:** fp/dollar-string mishandling on the REMOVE path
(removal pops by integer-cents key; the fp map is popped alongside —
`test_true_touch_fp.py` covers it); ticker-frame leakage (only
`orderbook_snapshot`/`orderbook_delta` types reach book state); stale deltas
across a reconnect (reconnect clears `subscribed` and re-snapshots every
market; within a connection the seq guard now covers it).

## CHUNK A STATUS (0718) — the gap, recorded

No autopsy output has been delivered yet (neither a flagged frame nor a
reported evidence gap). Per A(3)/the read-rule the gap is RECORDED here: the
standing conviction remains the three reconstructed-frame tests. The
dual-direction harness is in place —
`test_p10_book_truth.py::test_corrupting_frame_teeth_both_directions` proves
its tape corrupts the v7-shim applier (yes97+no55=152 exactly) AND stays
coherent through the fixed pipeline. When the Render-shell autopsy names the
verbatim frame, it drops into that test's `FRAMES` list and this section
gains the frame verbatim plus which HOLE (1/2/3) it convicts.

## VERDICT

Which hole was THE 01:25 liar: **UNPROVEN until the autopsy runs on the
deployed DB** (the tool is committed; the answer is one Render-shell command
away). Hole 1 is the leading suspect — it is the only class that produces
the exact observed shape from a single frame. All three are closed
regardless, and §2's coherence invariant makes any FUTURE lie self-announcing
within one frame: poison → lanes refuse → custodian on REST marks → resync →
(3×) quarantine to window close.
