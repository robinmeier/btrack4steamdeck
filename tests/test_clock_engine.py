#!/usr/bin/env python3
"""Simulated test of the MIDI clock engine in btrack_clock.py.

Feeds the engine perfectly periodic beats at 125 bpm (with the coarse
123.05 bpm tracker estimate BTrack actually reports at that tempo) and
checks that the emitted 24 ppqn clock converges to the true tempo and
that beat-boundary ticks line up with the beats.

Usage:  python3 tests/test_clock_engine.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btrack_clock import MIDI_CLOCK, MIDI_START, ClockEngine  # noqa: E402


def main():
    bpm_true = 125.0
    tracker_bpm = 123.05  # BTrack's quantized estimate near 125
    beat_interval = 60.0 / bpm_true

    engine = ClockEngine(min_bpm=80, max_bpm=160)
    engine.on_level(-20.0, 0.0)

    ticks = []
    starts = []
    next_beat = 1.0
    step = 0.005
    t = 0.0
    duration = 30.0
    while t < duration:
        if next_beat <= t:
            engine.on_beat(next_beat, tracker_bpm)
            next_beat += beat_interval
        for when, msg in engine.poll(t, t + step):
            if msg == MIDI_CLOCK:
                ticks.append(when)
            elif msg == MIDI_START:
                starts.append(when)
        t += step

    # measure the steady-state clock over the last 10 seconds
    tail = [x for x in ticks if x > duration - 10.0]
    rate = (len(tail) - 1) / (tail[-1] - tail[0])
    clock_bpm = rate * 60.0 / ClockEngine.PPQN

    # beat boundaries are every 24th tick, counted from the first tick
    boundaries = ticks[::ClockEngine.PPQN]
    errors = []
    for b in boundaries:
        if b > duration - 10.0:
            k = round((b - 1.0) / beat_interval)
            errors.append(abs(b - (1.0 + k * beat_interval)))
    max_phase_error_ms = max(errors) * 1000.0

    # tick spacing must stay smooth (no hard resyncs)
    deltas = [b - a for a, b in zip(tail, tail[1:])]
    jitter_ms = (max(deltas) - min(deltas)) * 1000.0

    print(f"steady-state clock: {clock_bpm:.2f} bpm (true {bpm_true})")
    print(f"start messages:     {len(starts)}")
    print(f"max phase error:    {max_phase_error_ms:.1f} ms")
    print(f"tick spacing range: {jitter_ms:.2f} ms")

    ok = (abs(clock_bpm - bpm_true) < 0.5
          and len(starts) == 1
          and max_phase_error_ms < 10.0
          and jitter_ms < 6.0)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
