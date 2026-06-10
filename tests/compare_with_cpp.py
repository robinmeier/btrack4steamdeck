#!/usr/bin/env python3
"""Validate the Python port in btrack_clock.py against the original C++.

Builds the upstream BTrack sources (kiss_fft variant) with a small test
harness, runs both implementations over the same synthetic audio, and
compares onset detection function samples, beat positions and tempo
estimates hop by hop.

Usage:  python3 tests/compare_with_cpp.py
Needs:  g++, curl (to fetch the upstream sources), numpy
"""

import os
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UPSTREAM = "https://codeload.github.com/adamstark/BTrack/tar.gz/refs/heads/master"

sys.path.insert(0, ROOT)
from btrack_clock import BTrack  # noqa: E402


def fetch_upstream(work):
    tarball = os.path.join(work, "btrack.tar.gz")
    urllib.request.urlretrieve(UPSTREAM, tarball)
    subprocess.run(["tar", "xzf", tarball, "-C", work], check=True)
    return os.path.join(work, "BTrack-master")


def build_reference(work):
    src = os.path.join(fetch_upstream(work), "src")
    kiss = os.path.join(os.path.dirname(src), "libs", "kiss_fft130")
    exe = os.path.join(work, "btrack_ref")
    subprocess.run(
        ["g++", "-O2", "-DUSE_KISS_FFT", "-Dkiss_fft_scalar=double",
         "-I", src, "-I", kiss, "-I", os.path.join(HERE, "cpp_reference"),
         os.path.join(HERE, "cpp_reference", "main.cpp"),
         os.path.join(src, "BTrack.cpp"),
         os.path.join(src, "OnsetDetectionFunction.cpp"),
         os.path.join(kiss, "kiss_fft.c"),
         "-o", exe],
        check=True)
    return exe


def synth_audio(seconds=30.0, bpm=125.0, sr=44100):
    rng = np.random.default_rng(7)
    n = int(seconds * sr)
    audio = 0.002 * rng.standard_normal(n)
    t = np.arange(int(0.03 * sr)) / sr
    click = np.exp(-t * 60.0) * (0.7 * np.sin(2 * np.pi * 180.0 * t)
                                 + 0.5 * rng.standard_normal(len(t)))
    pos = 0.0
    while pos < seconds - 0.1:
        i = int(pos * sr)
        audio[i:i + len(click)] += click[: n - i]
        pos += 60.0 / bpm
    return audio


def main():
    with tempfile.TemporaryDirectory() as work:
        print("building C++ reference ...")
        exe = build_reference(work)

        print("synthesizing test audio ...")
        audio = synth_audio()
        raw = os.path.join(work, "audio.f64")
        audio.astype("<f8").tofile(raw)

        print("running C++ reference ...")
        out = subprocess.run([exe, raw], check=True, capture_output=True, text=True)
        ref = [line.split() for line in out.stdout.strip().splitlines()]

        print("running Python port ...")
        bt = BTrack(512)
        max_odf_rel_err = 0.0
        max_tempo_err = 0.0
        beat_mismatches = 0
        for hop_index, (_, odf_s, beat_s, tempo_s) in enumerate(ref):
            hop = audio[hop_index * 512:(hop_index + 1) * 512]
            odf_py = bt.process_audio_frame(hop)
            odf_ref = float(odf_s)
            denom = max(abs(odf_ref), 1e-12)
            max_odf_rel_err = max(max_odf_rel_err, abs(odf_py - odf_ref) / denom)
            if int(bt.beat_due_in_frame) != int(beat_s):
                beat_mismatches += 1
            max_tempo_err = max(max_tempo_err,
                                abs(bt.estimated_tempo - float(tempo_s)))

        hops = len(ref)
        print(f"compared {hops} hops")
        print(f"  max relative ODF error : {max_odf_rel_err:.3e}")
        print(f"  beat flag mismatches   : {beat_mismatches}")
        print(f"  max tempo difference   : {max_tempo_err:.3e} bpm")

        ok = max_odf_rel_err < 1e-9 and beat_mismatches == 0 and max_tempo_err < 1e-6
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
