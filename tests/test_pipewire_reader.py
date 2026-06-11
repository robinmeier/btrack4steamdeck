#!/usr/bin/env python3
"""Test the PipeWire backend's reader path in btrack_clock.py.

A real pw-record needs a PipeWire daemon, so this substitutes it with a
subprocess that emits the same thing pw-record would: raw f32 mono audio
at 44.1 kHz on stdout, in real time — synthetic clicks at 125 bpm. The
test runs `pipewire_reader` against it, feeding the regular
BeatWorker/ClockEngine chain, and checks that the clock locks near
125 bpm. It also sanity-checks the pw-record command line builder.

Usage:  uv run --with numpy python3 tests/test_pipewire_reader.py
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btrack_clock import (  # noqa: E402
    BeatWorker, ClockEngine, pipewire_reader, pw_record_command,
)

FAKE_PW_RECORD = r"""
import sys, time
import numpy as np
sr, bpm, dur = 44100, 125.0, 16.0
rng = np.random.default_rng(3)
n = int(sr * dur)
audio = 0.002 * rng.standard_normal(n)
t = np.arange(int(0.03 * sr)) / sr
click = np.exp(-t * 60.0) * (0.7 * np.sin(2 * np.pi * 180.0 * t)
                             + 0.5 * rng.standard_normal(len(t)))
pos = 0.0
while pos < dur - 0.1:
    i = int(pos * sr)
    audio[i:i + len(click)] += click[: n - i]
    pos += 60.0 / bpm
chunk = 2048
start = time.perf_counter()
for i in range(0, n, chunk):
    block = audio[i:i + chunk].astype('<f4')
    sys.stdout.buffer.write(block.tobytes())
    sys.stdout.buffer.flush()
    target = start + (i + chunk) / sr
    delay = target - time.perf_counter()
    if delay > 0:
        time.sleep(delay)
"""


def test_command_builder():
    args = argparse.Namespace(client_name="BTrack", pw_target=None)
    cmd = pw_record_command(args)
    assert cmd[0] == "pw-record" and cmd[-1] == "-" and "--raw" in cmd
    assert '{ node.name = "BTrack" }' in cmd
    assert "--target" not in cmd
    args = argparse.Namespace(client_name="X", pw_target="spotify")
    cmd = pw_record_command(args)
    assert cmd[cmd.index("--target") + 1] == "spotify"
    print("pw-record command builder: ok")


def main():
    test_command_builder()

    proc = subprocess.Popen([sys.executable, "-c", FAKE_PW_RECORD],
                            stdout=subprocess.PIPE)

    stop_event = threading.Event()
    audio_queue = queue.Queue(maxsize=64)
    engine = ClockEngine(min_bpm=80, max_bpm=160)
    worker = BeatWorker(engine, 44100.0, audio_queue, stop_event)

    eof_seen = threading.Event()
    reader = threading.Thread(
        target=pipewire_reader,
        args=(proc.stdout, audio_queue, stop_event, eof_seen.set),
        daemon=True)

    print("streaming 16 s of fake pw-record audio (125 bpm clicks) ...")
    worker.start()
    reader.start()
    proc.wait(timeout=30)
    eof_seen.wait(timeout=5)
    time.sleep(0.5)  # let the worker drain the queue
    stop_event.set()
    worker.join(timeout=2)

    snap = engine.snapshot()
    print(f"hops processed: {worker.hops_done}")
    print(f"clock: {snap['clock_bpm']:.2f} bpm, locked={snap['locked']}, "
          f"ticking={snap['ticking']}")

    ok = (worker.hops_done > 1000
          and eof_seen.is_set()
          and snap["locked"]
          and abs(snap["clock_bpm"] - 125.0) < 1.5)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
