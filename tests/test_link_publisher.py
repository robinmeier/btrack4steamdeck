#!/usr/bin/env python3
"""Test the Ableton Link bridge in btrack_clock.py.

Drives a real ClockEngine with synthetic 125 bpm beats in real time, runs
LinkPublisher against it, and observes the session with an independent
link.Link instance. If multicast peer discovery is unavailable (common in
containers), falls back to asserting on the publisher's own committed
session state.

Usage:  uv run --with numpy --with LinkPython-extern python3 tests/test_link_publisher.py
"""

import os
import sys
import threading
import time

import link

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btrack_clock import ClockEngine, LinkPublisher  # noqa: E402

BPM_TRUE = 125.0
TRACKER_BPM = 123.05  # BTrack's quantized estimate near 125


def drive_engine(engine, stop_event):
    beat_interval = 60.0 / BPM_TRUE
    next_beat = time.perf_counter() + 0.5
    last_poll = time.perf_counter()
    while not stop_event.is_set():
        now = time.perf_counter()
        if now >= next_beat:
            engine.on_beat(next_beat, TRACKER_BPM)
            next_beat += beat_interval
        engine.poll(last_poll, now)  # generate ticks / beat anchors
        last_poll = now
        time.sleep(0.005)


def beat_phase_error(state, engine, quantum):
    anchor = engine.snapshot()["beat_anchor"]
    if anchor is None:
        return None
    anchor_time, anchor_beat = anchor
    offset_us = link.Link(120).clock().micros() - time.perf_counter() * 1e6
    anchor_us = int(anchor_time * 1e6 + offset_us)
    err = state.beatAtTime(anchor_us, quantum) - anchor_beat
    return (err + 0.5) % 1.0 - 0.5


def main():
    quantum = 4.0
    stop_event = threading.Event()
    engine = ClockEngine(min_bpm=80, max_bpm=160)

    driver = threading.Thread(target=drive_engine, args=(engine, stop_event),
                              daemon=True)
    driver.start()

    publisher = LinkPublisher(engine, time.perf_counter, quantum, stop_event)
    publisher.start()

    observer = link.Link(120.0)
    observer.enabled = True

    print("driving 125 bpm beats into the engine for 12 s ...")
    deadline = time.perf_counter() + 12.0
    peers_seen = 0
    while time.perf_counter() < deadline:
        peers_seen = max(peers_seen, observer.numPeers())
        time.sleep(0.25)

    snap = engine.snapshot()
    print(f"engine clock: {snap['clock_bpm']:.2f} bpm, ticking={snap['ticking']}")

    if peers_seen > 0:
        state = observer.captureAppSessionState()
        tempo = state.tempo()
        err = beat_phase_error(state, engine, quantum)
        print(f"observer peer: tempo {tempo:.2f} bpm, "
              f"beat phase error {err * 1000 / (tempo / 60):.1f} ms"
              if err is not None else f"observer peer: tempo {tempo:.2f} bpm")
        ok = abs(tempo - BPM_TRUE) < 0.5 and err is not None and abs(err) < 0.1
    else:
        print("note: no Link peer discovered (no multicast here?) - "
              "checking the publisher's own session state instead")
        state = publisher.link.captureAppSessionState()
        tempo = state.tempo()
        err = beat_phase_error(state, engine, quantum)
        print(f"published state: tempo {tempo:.2f} bpm, "
              f"beat phase error {abs(err):.3f} beats")
        ok = abs(tempo - BPM_TRUE) < 0.5 and err is not None and abs(err) < 0.1

    stop_event.set()
    driver.join(timeout=2)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
