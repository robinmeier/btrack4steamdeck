#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = [
#   "numpy>=1.24",
#   "JACK-Client>=0.5.4",
#   "sounddevice>=0.4.6",
#   "python-rtmidi>=1.5",
#   "LinkPython-extern>=1.2.1",
# ]
# ///
#
# btrack_clock.py - real-time beat tracking to MIDI clock for PipeWire
#
# A single-file, faithful numpy port of the BTrack real-time beat tracker
# by Adam Stark, Matthew Davies and Mark Plumbley
# (https://github.com/adamstark/BTrack, GPLv3), wrapped in a PipeWire-aware
# audio-to-MIDI-clock bridge:
#
#   audio in (JACK/pipewire or ALSA)  ->  BTrack  ->  MIDI clock out (24 ppqn)
#                                                 ->  Ableton Link (--link)
#
# Run it with uv (https://docs.astral.sh/uv/) - dependencies are resolved
# automatically:
#
#   uv run btrack_clock.py
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import math
import queue
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

import numpy as np

MIDI_CLOCK = b"\xf8"
MIDI_START = b"\xfa"
MIDI_STOP = b"\xfc"

BTRACK_SR = 44100.0  # the BTrack algorithm is defined at 44.1 kHz


def c_round(x):
    """Round half away from zero, like C's round(). Python's round() rounds
    half to even, which would diverge from the original implementation."""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


# ======================================================================
# Onset detection function
# (port of OnsetDetectionFunction.cpp, ComplexSpectralDifferenceHWR only,
#  Hanning window - BTrack's defaults)
# ======================================================================
class OnsetDetectionFunction:
    def __init__(self, hop_size=512, frame_size=1024):
        self.hop_size = hop_size
        self.frame_size = frame_size
        self.frame = np.zeros(frame_size)
        n = np.arange(frame_size, dtype=np.float64)
        self.window = 0.5 * (1.0 - np.cos(2.0 * np.pi * n / (frame_size - 1)))
        self.prev_mag = np.zeros(frame_size)
        self.prev_phase = np.zeros(frame_size)
        self.prev_phase2 = np.zeros(frame_size)

    def calculate_sample(self, hop_buffer):
        f = self.frame
        f[: -self.hop_size] = f[self.hop_size:]
        f[-self.hop_size:] = hop_buffer

        # window the frame and swap its halves before the FFT, as the C++ does
        fw = f * self.window
        half = self.frame_size // 2
        spectrum = np.fft.fft(np.concatenate((fw[half:], fw[:half])))

        mag = np.abs(spectrum)
        phase = np.angle(spectrum)
        phase_deviation = phase - 2.0 * self.prev_phase + self.prev_phase2

        # complex spectral difference, half-wave rectified on magnitude change
        csd = np.sqrt(np.maximum(
            mag * mag + self.prev_mag * self.prev_mag
            - 2.0 * mag * self.prev_mag * np.cos(phase_deviation), 0.0))
        sample = float(np.sum(csd[(mag - self.prev_mag) > 0]))

        self.prev_phase2[:] = self.prev_phase
        self.prev_phase[:] = phase
        self.prev_mag[:] = mag
        return sample


# ======================================================================
# Beat tracker (port of BTrack.cpp)
# ======================================================================
class BTrack:
    def __init__(self, hop_size=512):
        self.odf = OnsetDetectionFunction(hop_size, 2 * hop_size)

        rayleigh = 43.0
        self.tightness = 5.0
        self.alpha = 0.9
        self.estimated_tempo = 120.0
        self.time_to_next_prediction = 10
        self.time_to_next_beat = -1
        self.beat_due_in_frame = False

        n = np.arange(128, dtype=np.float64)
        self.weighting_vector = (n / rayleigh ** 2) * np.exp(-(n * n) / (2.0 * rayleigh ** 2))

        self.prev_delta = np.ones(41)
        self.prev_delta_fixed = np.zeros(41)

        m_sig = 41 / 8
        t_mu = np.arange(1, 42, dtype=np.float64)[:, None]
        x = np.arange(1, 42, dtype=np.float64)[None, :]
        self.tempo_transition_matrix = (
            1.0 / (m_sig * math.sqrt(2.0 * math.pi))
        ) * np.exp(-((x - t_mu) ** 2) / (2.0 * m_sig ** 2))

        self.tempo_fixed = False
        self.acf = np.zeros(512)
        self.comb_filter_bank_output = np.zeros(128)
        self.resampled_odf = np.zeros(512)
        self.set_hop_size(hop_size)

    def set_hop_size(self, hop_size):
        self.hop_size = hop_size
        self.onset_df_buffer_size = (512 * 512) // hop_size
        self.beat_period = float(c_round(60.0 / ((hop_size / BTRACK_SR) * 120.0)))
        self.onset_df = np.zeros(self.onset_df_buffer_size)
        self.cumulative_score = np.zeros(self.onset_df_buffer_size)
        self.onset_df[:: int(c_round(self.beat_period))] = 1.0

    def process_audio_frame(self, hop_buffer):
        sample = self.odf.calculate_sample(hop_buffer)
        self.process_onset_detection_function_sample(sample)
        return sample

    def process_onset_detection_function_sample(self, new_sample):
        # keep the sample strictly positive to avoid problems downstream
        new_sample = abs(new_sample) + 0.0001

        self.time_to_next_prediction -= 1
        self.time_to_next_beat -= 1
        self.beat_due_in_frame = False

        buf = self.onset_df
        buf[:-1] = buf[1:]
        buf[-1] = new_sample

        self._update_cumulative_score(new_sample)

        if self.time_to_next_prediction == 0:
            self._predict_beat()

        if self.time_to_next_beat == 0:
            self.beat_due_in_frame = True
            self._resample_onset_detection_function()
            self._calculate_tempo()

    def set_tempo(self, tempo):
        """Reset the tracker state to a known tempo (e.g. from tap tempo)."""
        while tempo > 160:
            tempo /= 2.0
        while tempo < 80:
            tempo *= 2.0
        tempo_index = int(c_round((tempo - 80.0) / 2.0))
        self.prev_delta[:] = 0.0
        self.prev_delta[tempo_index] = 1.0

        new_beat_period = int(c_round(60.0 / ((self.hop_size / BTRACK_SR) * tempo)))
        k = 1
        for i in range(self.onset_df_buffer_size - 1, -1, -1):
            value = 150.0 if k == 1 else 10.0
            self.cumulative_score[i] = value
            self.onset_df[i] = value
            k += 1
            if k > new_beat_period:
                k = 1

        self.time_to_next_beat = 0
        self.time_to_next_prediction = int(c_round(new_beat_period / 2.0))

    def fix_tempo(self, tempo):
        while tempo > 160:
            tempo /= 2.0
        while tempo < 80:
            tempo *= 2.0
        tempo_index = int(c_round((tempo - 80.0) / 2.0))
        self.prev_delta_fixed[:] = 0.0
        self.prev_delta_fixed[tempo_index] = 1.0
        self.tempo_fixed = True

    def do_not_fix_tempo(self):
        self.tempo_fixed = False

    # ------------------------------------------------------------------
    def _log_gaussian_transition_weighting(self, num_samples, beat_period):
        # W1 in Adam Stark's PhD thesis, equation 3.2, page 60
        v = -2.0 * beat_period + np.arange(num_samples, dtype=np.float64)
        a = self.tightness * np.log(-v / beat_period)
        return np.exp(-(a * a) / 2.0)

    def _update_cumulative_score(self, odf_sample):
        n = self.onset_df_buffer_size
        window_start = n - c_round(2.0 * self.beat_period)
        window_end = n - c_round(self.beat_period / 2.0)
        w1 = self._log_gaussian_transition_weighting(
            window_end - window_start + 1, self.beat_period)

        # equation 3.4 on page 60 of Adam Stark's PhD thesis
        max_value = float(np.max(self.cumulative_score[window_start:window_end + 1] * w1))
        score = (1.0 - self.alpha) * odf_sample + self.alpha * max_value

        cs = self.cumulative_score
        cs[:-1] = cs[1:]
        cs[-1] = score

    def _predict_beat(self):
        n = self.onset_df_buffer_size
        bp = self.beat_period
        window_size = int(bp)  # truncation, as in the C++

        future = np.zeros(n + window_size)
        future[:n] = self.cumulative_score

        # W2 in Adam Stark's PhD thesis, equation 3.6, page 62
        v = np.arange(1, window_size + 1, dtype=np.float64)
        w2 = np.exp(-((v - bp / 2.0) ** 2) / (2.0 * (bp / 2.0) ** 2))

        start = n - c_round(2.0 * bp)
        end = n - c_round(bp / 2.0)
        w1 = self._log_gaussian_transition_weighting(end - start + 1, bp)

        # synthesize the cumulative score into the future using its momentum
        for i in range(n, n + window_size):
            future[i] = float(np.max(future[start:end + 1] * w1))
            start += 1
            end += 1

        self.time_to_next_beat = int(np.argmax(future[n:] * w2))
        self.time_to_next_prediction = self.time_to_next_beat + c_round(bp / 2.0)

    def _resample_onset_detection_function(self):
        n = self.onset_df_buffer_size
        if n == 512:
            # at the canonical 512 hop size the resample ratio is exactly 1
            self.resampled_odf = self.onset_df.copy()
        else:
            # the C++ uses libsamplerate here; linear interpolation is an
            # adequate stand-in for non-default hop sizes
            positions = np.arange(512, dtype=np.float64) * (n / 512.0)
            self.resampled_odf = np.interp(positions, np.arange(n), self.onset_df)

    def _calculate_tempo(self):
        tempo_to_lag_factor = 60.0 * BTRACK_SR / 512.0

        x = self.resampled_odf.copy()
        self._adaptive_threshold(x)
        acf = self._calculate_balanced_acf(x)

        comb = np.zeros(128)
        wv = self.weighting_vector
        for i in range(2, 128):
            for a in range(1, 5):
                for b in range(1 - a, a):
                    comb[i - 1] += (acf[(a * i + b) - 1] * wv[i - 1]) / (2 * a - 1)
        self._adaptive_threshold(comb)
        self.comb_filter_bank_output = comb

        tempo_observation = np.zeros(41)
        for i in range(41):
            index1 = int(c_round(tempo_to_lag_factor / float(2 * i + 80)))
            index2 = int(c_round(tempo_to_lag_factor / float(4 * i + 160)))
            tempo_observation[i] = comb[index1 - 1] + comb[index2 - 1]

        prev = self.prev_delta_fixed if self.tempo_fixed else self.prev_delta
        delta = (prev[:, None] * self.tempo_transition_matrix).max(axis=0) * tempo_observation

        total = delta.sum()
        if total > 0:
            delta = delta / total

        max_index = int(np.argmax(delta))
        self.prev_delta = delta.copy()

        self.beat_period = float(c_round(
            (60.0 * BTRACK_SR) / ((2 * max_index + 80) * float(self.hop_size))))
        if self.beat_period > 0:
            self.estimated_tempo = 60.0 / ((self.hop_size / BTRACK_SR) * self.beat_period)

    @staticmethod
    def _mean_of_range(v, start, end):
        # mean of v[start:end], like calculateMeanOfVector in the C++
        # (note: callers deliberately pass start=1 at the edges, as upstream does)
        if end - start > 0:
            return float(np.mean(v[start:end]))
        return 0.0

    def _adaptive_threshold(self, x):
        n = len(x)
        p_post, p_pre = 7, 8
        threshold = np.zeros(n)

        t = min(n, p_post)
        for i in range(0, t + 1):
            k = min(i + p_pre, n)
            threshold[i] = self._mean_of_range(x, 1, k)

        # moving average over [i - p_pre, i + p_post) for the bulk, via cumsum
        if n - p_post > t + 1:
            csum = np.concatenate(([0.0], np.cumsum(x)))
            idx = np.arange(t + 1, n - p_post)
            threshold[idx] = (csum[idx + p_post] - csum[idx - p_pre]) / (p_post + p_pre)

        for i in range(n - p_post, n):
            k = max(i - p_post, 1)
            threshold[i] = self._mean_of_range(x, k, n)

        np.maximum(x - threshold, 0.0, out=x)

    def _calculate_balanced_acf(self, onset_df_512):
        spectrum = np.fft.fft(onset_df_512, 1024)
        power = spectrum.real ** 2 + spectrum.imag ** 2
        # kiss_fft's inverse transform is unnormalised and the C++ divides by
        # 1024 afterwards, which together equal numpy's normalised ifft
        v = np.fft.ifft(power)
        acf = np.abs(v[:512]) / np.arange(512, 0, -1, dtype=np.float64)
        self.acf = acf
        return acf


# ======================================================================
# Streaming linear resampler (native rate -> 44100 Hz, mono)
# ======================================================================
class StreamResampler:
    def __init__(self, in_rate, out_rate=BTRACK_SR):
        self.ratio = in_rate / out_rate
        self.pos = 0.0
        self.tail = np.zeros(0)

    def process(self, samples):
        if not len(samples):
            return np.zeros(0)
        data = np.concatenate((self.tail, samples))
        last = len(data) - 1
        if self.pos > last:
            self.tail = data
            return np.zeros(0)
        n_out = int((last - self.pos) // self.ratio) + 1
        positions = self.pos + np.arange(n_out) * self.ratio
        out = np.interp(positions, np.arange(len(data)), data)
        next_pos = self.pos + n_out * self.ratio
        keep = min(int(next_pos), last)  # keep data[keep:] for interpolation
        self.tail = data[keep:].copy()
        self.pos = next_pos - keep
        return out


# ======================================================================
# Clock engine: smoothed tempo + phase-nudged 24 ppqn MIDI clock
# ======================================================================
class ClockEngine:
    PPQN = 24

    def __init__(self, min_bpm=80.0, max_bpm=160.0, phase_gain=0.15,
                 offset_s=0.0, send_start_stop=True, lock_beats=8):
        self.lock = threading.Lock()
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.phase_gain = phase_gain
        self.offset_s = offset_s
        self.send_start_stop = send_start_stop
        self.lock_beats = lock_beats

        self.clock_bpm = 120.0
        self.multiplier = 1.0
        self.freeze = False
        self.tracker_bpm = 120.0

        self.locked = False
        self.ticking = False
        self.transport_running = False
        self.pending_start = False
        self.pending_stop = False
        self.manual_stop = False

        self.next_tick_time = None
        self.tick_count = 0
        self.nudge_per_tick = 0.0
        self.nudge_ticks_left = 0
        self.beat_anchor = None  # (time, beat number) of the last beat tick

        self.beat_times = deque(maxlen=16)
        self.last_beat_time = 0.0
        self.last_tick_sent = 0.0
        self.silent_since = None
        self.input_db = -120.0

    # ---- called from the DSP thread -----------------------------------
    def fold_bpm(self, bpm):
        if bpm <= 0:
            return self.min_bpm
        while bpm < self.min_bpm:
            bpm *= 2.0
        while bpm > self.max_bpm:
            bpm /= 2.0
        return bpm

    def on_beat(self, beat_time, tracker_tempo):
        with self.lock:
            self.tracker_bpm = tracker_tempo
            self.last_beat_time = beat_time
            self.beat_times.append(beat_time)

            bpm = self._estimate_bpm(tracker_tempo)
            self._update_lock_state()

            if self.freeze:
                return

            out_bpm = self.fold_bpm(bpm) * self.multiplier
            # smooth tempo changes a little so receivers don't see jumps
            self.clock_bpm += 0.4 * (out_bpm - self.clock_bpm)

            if self.locked and not self.ticking:
                self._start_clock(beat_time)
            elif self.ticking:
                self._nudge_phase(beat_time)

            if (self.locked and self.send_start_stop and not self.manual_stop
                    and not self.transport_running and not self.pending_start):
                self.pending_start = True

    def on_level(self, db, now):
        with self.lock:
            self.input_db = db
            if db < -50.0:
                if self.silent_since is None:
                    self.silent_since = now
                elif now - self.silent_since > 3.0:
                    if self.transport_running and self.send_start_stop:
                        self.pending_stop = True
                    self.locked = False
                    self.beat_times.clear()
            else:
                self.silent_since = None

    def _estimate_bpm(self, tracker_tempo):
        # prefer the measured inter-beat intervals: they resolve tempo more
        # finely than BTrack's integer beat period (~3 bpm steps near 120)
        if len(self.beat_times) >= 4:
            intervals = [b - a for a, b in zip(self.beat_times, list(self.beat_times)[1:])]
            expected = 60.0 / max(tracker_tempo, 1.0)
            good = [i for i in intervals if 0.8 * expected < i < 1.2 * expected]
            if len(good) >= 3:
                return 60.0 / statistics.median(good[-8:])
        return tracker_tempo

    def _update_lock_state(self):
        if len(self.beat_times) < self.lock_beats:
            self.locked = False
            return
        intervals = [b - a for a, b in zip(self.beat_times, list(self.beat_times)[1:])]
        recent = intervals[-(self.lock_beats - 1):]
        med = statistics.median(recent)
        # allow intervals at half/double of the median (octave ambiguity)
        deviations = []
        for iv in recent:
            candidates = (abs(iv - med), abs(iv - 2 * med), abs(iv - med / 2))
            deviations.append(min(candidates) / med)
        self.locked = max(deviations) < 0.08

    def _start_clock(self, beat_time):
        period = 60.0 / self.clock_bpm
        first = beat_time + self.offset_s + period
        self.next_tick_time = first
        self.tick_count = 0
        self.beat_anchor = None
        self.ticking = True
        self.nudge_ticks_left = 0
        self.nudge_per_tick = 0.0

    def _nudge_phase(self, beat_time):
        period = 60.0 / self.clock_bpm
        tick_interval = period / self.PPQN
        # time of the beat boundary (tick_count % 24 == 0) nearest to the beat
        ticks_to_boundary = (-self.tick_count) % self.PPQN
        next_boundary = self.next_tick_time + ticks_to_boundary * tick_interval
        target = beat_time + self.offset_s
        error = target - next_boundary
        error = (error + period / 2.0) % period - period / 2.0  # wrap to +-half beat
        nudge = max(-0.25 * period, min(0.25 * period, self.phase_gain * error))
        # spread the correction over the next beat to keep tick spacing smooth
        self.nudge_per_tick = nudge / self.PPQN
        self.nudge_ticks_left = self.PPQN

    # ---- called from the output backend --------------------------------
    def poll(self, now, until):
        """Return [(time, midi_bytes)] for events due in [now, until)."""
        events = []
        with self.lock:
            if self.pending_stop:
                self.pending_stop = False
                self.transport_running = False
                events.append((now, MIDI_STOP))
            if not self.ticking or self.next_tick_time is None:
                return events
            # the clock fell badly behind (suspend, xrun): re-anchor it
            if self.next_tick_time < now - 0.5:
                self.next_tick_time = now
            tick_interval = (60.0 / self.clock_bpm) / self.PPQN
            while self.next_tick_time < until:
                t = self.next_tick_time
                if self.tick_count % self.PPQN == 0:
                    self.beat_anchor = (t, self.tick_count // self.PPQN)
                    if self.pending_start:
                        self.pending_start = False
                        self.transport_running = True
                        events.append((max(now, t - 0.001), MIDI_START))
                events.append((t, MIDI_CLOCK))
                self.last_tick_sent = t
                step = tick_interval
                if self.nudge_ticks_left > 0:
                    step += self.nudge_per_tick
                    self.nudge_ticks_left -= 1
                self.next_tick_time += step
                self.tick_count += 1
        return events

    # ---- user controls --------------------------------------------------
    def set_multiplier(self, factor):
        with self.lock:
            self.multiplier = factor

    def toggle_freeze(self):
        with self.lock:
            self.freeze = not self.freeze
            return self.freeze

    def toggle_transport(self):
        with self.lock:
            if self.transport_running:
                self.pending_stop = True
                self.manual_stop = True
            else:
                self.manual_stop = False
                self.pending_start = True

    def force_bpm(self, bpm, anchor_time):
        with self.lock:
            self.clock_bpm = self.fold_bpm(bpm) * self.multiplier
            self.beat_times.clear()
            if self.ticking:
                self._nudge_phase(anchor_time)
            else:
                self.locked = True
                self._start_clock(anchor_time)

    def snapshot(self):
        with self.lock:
            return {
                "tracker_bpm": self.tracker_bpm,
                "clock_bpm": self.clock_bpm,
                "locked": self.locked,
                "ticking": self.ticking,
                "running": self.transport_running,
                "freeze": self.freeze,
                "multiplier": self.multiplier,
                "input_db": self.input_db,
                "last_beat": self.last_beat_time,
                "beat_anchor": self.beat_anchor,
            }


# ======================================================================
# Ableton Link publisher (--link): bridges the clock engine into a Link
# session so DAWs with native Link support (Reaper, Bitwig, ...) can sync
# without MIDI. Periodically re-forcing the Link beat/time mapping from an
# external clock is the use Link's own forceBeatAtTime documentation
# sanctions for bridging an outside clock source into a session.
# ======================================================================
class LinkPublisher(threading.Thread):
    def __init__(self, engine, clock_now, quantum, stop_event):
        super().__init__(daemon=True, name="link-publisher")
        import link  # LinkPython-extern
        self.engine = engine
        self.clock_now = clock_now
        self.quantum = float(quantum)
        self.stop_event = stop_event
        self.link = link.Link(engine.clock_bpm)
        self.link.startStopSyncEnabled = True
        self.link.enabled = True
        self.peers = 0
        self.tempo = engine.clock_bpm
        self._was_running = False

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._publish()
            except Exception:
                pass  # never let a Link hiccup take the clock down
            self.stop_event.wait(0.25)
        self.link.enabled = False

    def _publish(self):
        snap = self.engine.snapshot()
        now_us = self.link.clock().micros()
        # bridge between the audio backend's timebase and Link's clock;
        # re-sampled every iteration so slow drift between the two is absorbed
        offset_us = now_us - self.clock_now() * 1e6

        state = self.link.captureAppSessionState()
        dirty = False

        if snap["ticking"]:
            bpm = snap["clock_bpm"]
            if abs(state.tempo() - bpm) > 0.01:
                state.setTempo(bpm, now_us)
                dirty = True

            anchor = snap["beat_anchor"]
            if anchor is not None:
                anchor_time, anchor_beat = anchor
                anchor_us = int(anchor_time * 1e6 + offset_us)
                error = state.beatAtTime(anchor_us, self.quantum) - anchor_beat
                error = (error + 0.5) % 1.0 - 0.5  # beat-phase error
                # only re-force the mapping when actually drifted, so Link
                # peers aren't disturbed every iteration
                if abs(error) > 0.05:
                    state.forceBeatAtTime(float(anchor_beat), anchor_us, self.quantum)
                    dirty = True

        if self.engine.send_start_stop and snap["running"] != self._was_running:
            state.setIsPlaying(snap["running"], now_us)
            self._was_running = snap["running"]
            dirty = True

        if dirty:
            self.link.commitAppSessionState(state)

        self.peers = self.link.numPeers()
        self.tempo = state.tempo()


# ======================================================================
# DSP thread: hops audio through BTrack and feeds the clock engine
# ======================================================================
class BeatWorker(threading.Thread):
    def __init__(self, engine, sample_rate, audio_queue, stop_event):
        super().__init__(daemon=True, name="btrack-dsp")
        self.engine = engine
        self.sample_rate = sample_rate
        self.audio_queue = audio_queue
        self.stop_event = stop_event
        self.btrack = BTrack(512)
        self.resampler = (StreamResampler(sample_rate)
                          if abs(sample_rate - BTRACK_SR) > 1e-6 else None)
        self.pending = np.zeros(0)
        self.hops_done = 0
        self.tap_times = deque(maxlen=5)
        self.tempo_request = None  # set from the UI thread, applied here
        self.reset_request = False

    def run(self):
        while not self.stop_event.is_set():
            try:
                block_time, block = self.audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.reset_request:
                self.reset_request = False
                self.btrack = BTrack(512)
            if self.tempo_request is not None:
                self.btrack.set_tempo(self.tempo_request)
                self.tempo_request = None
            samples = self.resampler.process(block) if self.resampler else block
            if len(self.pending):
                samples = np.concatenate((self.pending, samples))
            n_hops = len(samples) // 512
            # timestamp bookkeeping: time of the END of each hop, in the
            # backend's timebase, derived from the block-start time
            consumed_before = len(self.pending)
            for h in range(n_hops):
                hop = samples[h * 512:(h + 1) * 512]
                self.btrack.process_audio_frame(hop)
                self.hops_done += 1
                produced = (h + 1) * 512 - consumed_before
                hop_end_time = block_time + (produced / BTRACK_SR
                                             if self.resampler else produced / self.sample_rate)
                rms = float(np.sqrt(np.mean(hop * hop)))
                db = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
                self.engine.on_level(db, hop_end_time)
                if self.btrack.beat_due_in_frame:
                    self.engine.on_beat(hop_end_time, self.btrack.estimated_tempo)
            self.pending = samples[n_hops * 512:]

    def tap(self, now):
        self.tap_times.append(now)
        taps = list(self.tap_times)
        intervals = [b - a for a, b in zip(taps, taps[1:]) if 0.2 < b - a < 2.0]
        if intervals:
            bpm = 60.0 / (sum(intervals) / len(intervals))
            self.tempo_request = bpm
            self.engine.force_bpm(bpm, now)
            return bpm
        return None


# ======================================================================
# JACK (pipewire-jack) backend: audio in + sample-accurate MIDI clock out
# ======================================================================
def run_jack(args, make_worker):
    import jack

    client = jack.Client(args.client_name)
    audio_in = client.inports.register("audio_in")
    midi_out = client.midi_outports.register("midi_clock_out")
    sample_rate = float(client.samplerate)

    stop_event = threading.Event()
    audio_queue = queue.Queue(maxsize=64)
    engine, worker = make_worker(sample_rate, audio_queue, stop_event)

    @client.set_process_callback
    def process(nframes):
        midi_out.clear_buffer()
        block_start = client.last_frame_time
        t0 = block_start / sample_rate
        t1 = (block_start + nframes) / sample_rate
        try:
            audio_queue.put_nowait((t0, np.array(audio_in.get_array(), dtype=np.float64)))
        except queue.Full:
            pass
        for t, msg in engine.poll(t0, t1):
            offset = min(max(int((t - t0) * sample_rate), 0), nframes - 1)
            midi_out.write_midi_event(offset, msg)

    @client.set_shutdown_callback
    def shutdown(status, reason):
        stop_event.set()

    clock_now = lambda: client.frame_time / sample_rate  # noqa: E731

    with client:
        worker.start()
        link_pub = None
        if args.link:
            link_pub = LinkPublisher(engine, clock_now, args.link_quantum, stop_event)
            link_pub.start()
        run_ui(args, engine, worker, stop_event,
               backend=f"jack/pipewire {int(sample_rate)} Hz",
               clock_now=clock_now, link_pub=link_pub)
    return 0


# ======================================================================
# Shared ALSA-sequencer MIDI clock output (used by the pipewire and alsa
# backends). PipeWire's Midi-Bridge exposes the port in the graph.
# ======================================================================
def start_rtmidi_clock(engine, stop_event, client_name):
    import rtmidi

    midi = rtmidi.MidiOut(rtapi=rtmidi.API_LINUX_ALSA, name=client_name)
    midi.open_virtual_port("midi_clock_out")

    def clock_thread():
        while not stop_event.is_set():
            now = time.perf_counter()
            events = engine.poll(now, now + 0.010)
            for t, msg in events:
                wait = t - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                midi.send_message(list(msg))
            time.sleep(0.002)

    threading.Thread(target=clock_thread, daemon=True, name="midi-clock").start()
    return midi


# ======================================================================
# Native PipeWire backend: audio in through a pw-record stream node
# (part of the stock pipewire package) + ALSA-seq MIDI clock out. Avoids
# both the pipewire-jack and PortAudio layers.
# ======================================================================
PW_HOP = 512


def pw_record_command(args):
    cmd = [
        "pw-record", "--raw",
        "--format", "f32",
        "--rate", str(int(BTRACK_SR)),
        "--channels", "1",
        "--latency", f"{PW_HOP}/{int(BTRACK_SR)}",
        "-P", '{ node.name = "%s" }' % args.client_name,
    ]
    if args.pw_target is not None:
        cmd += ["--target", args.pw_target]
    cmd.append("-")
    return cmd


def pipewire_reader(stdout, audio_queue, stop_event, on_eof):
    hop_bytes = PW_HOP * 4  # f32 mono
    samples_read = 0
    anchor = None  # estimated stream start, in perf_counter terms
    while not stop_event.is_set():
        data = stdout.read(hop_bytes)
        if not data or len(data) < hop_bytes:
            on_eof()
            return
        samples_read += PW_HOP
        # smooth timestamps: pipe batching only ever delays reads, so the
        # stream start is the minimum of (read time - samples elapsed); a
        # slight upward creep absorbs drift between the audio clock and
        # perf_counter
        estimate = time.perf_counter() - samples_read / BTRACK_SR
        if anchor is None:
            anchor = estimate
        else:
            anchor = min(anchor, estimate) + 0.0005 * max(estimate - anchor, 0.0)
        block_time = anchor + (samples_read - PW_HOP) / BTRACK_SR
        block = np.frombuffer(data, dtype="<f4").astype(np.float64)
        try:
            audio_queue.put_nowait((block_time, block))
        except queue.Full:
            pass


def run_pipewire(args, make_worker):
    if shutil.which("pw-record") is None:
        raise RuntimeError("pw-record not found (pipewire package)")

    stderr_file = tempfile.TemporaryFile()
    proc = subprocess.Popen(pw_record_command(args),
                            stdout=subprocess.PIPE, stderr=stderr_file)

    # let pw-record fail fast (no daemon, unsupported option, ...)
    time.sleep(0.4)
    if proc.poll() is not None:
        stderr_file.seek(0)
        message = stderr_file.read().decode(errors="replace").strip()
        raise RuntimeError(f"pw-record exited: {message or proc.returncode}")

    stop_event = threading.Event()
    audio_queue = queue.Queue(maxsize=64)
    engine, worker = make_worker(BTRACK_SR, audio_queue, stop_event)

    def on_eof():
        if not stop_event.is_set():
            print("\nerror: pw-record stream ended", file=sys.stderr)
            stop_event.set()

    reader = threading.Thread(
        target=pipewire_reader,
        args=(proc.stdout, audio_queue, stop_event, on_eof),
        daemon=True, name="pw-reader")

    midi = start_rtmidi_clock(engine, stop_event, args.client_name)
    try:
        worker.start()
        reader.start()
        link_pub = None
        if args.link:
            link_pub = LinkPublisher(engine, time.perf_counter,
                                     args.link_quantum, stop_event)
            link_pub.start()
        run_ui(args, engine, worker, stop_event,
               backend=f"pipewire (pw-record) {int(BTRACK_SR)} Hz",
               clock_now=time.perf_counter, link_pub=link_pub)
    finally:
        proc.terminate()
        midi.close_port()
        stderr_file.close()
    return 0


# ======================================================================
# ALSA fallback backend: sounddevice in + python-rtmidi clock out
# ======================================================================
def run_alsa(args, make_worker):
    import sounddevice as sd

    if args.input_device is not None:
        sd.default.device = (args.input_device, None)

    device_info = sd.query_devices(kind="input")
    sample_rate = float(device_info["default_samplerate"])

    stop_event = threading.Event()
    audio_queue = queue.Queue(maxsize=64)
    engine, worker = make_worker(sample_rate, audio_queue, stop_event)

    def audio_callback(indata, frames, time_info, status):
        now = time.perf_counter() - frames / sample_rate
        try:
            audio_queue.put_nowait((now, indata[:, 0].astype(np.float64)))
        except queue.Full:
            pass

    stream = sd.InputStream(channels=1, samplerate=sample_rate,
                            blocksize=512, callback=audio_callback)
    midi = start_rtmidi_clock(engine, stop_event, args.client_name)
    with stream:
        worker.start()
        link_pub = None
        if args.link:
            link_pub = LinkPublisher(engine, time.perf_counter,
                                     args.link_quantum, stop_event)
            link_pub.start()
        run_ui(args, engine, worker, stop_event,
               backend=f"alsa ({device_info['name']}) {int(sample_rate)} Hz",
               clock_now=time.perf_counter, link_pub=link_pub)
    midi.close_port()
    return 0


# ======================================================================
# Terminal UI: status line + keyboard controls
# ======================================================================
def run_ui(args, engine, worker, stop_event, backend, clock_now, link_pub=None):
    interactive = sys.stdin.isatty() and not args.no_keys
    old_attrs = None
    if interactive:
        import termios
        import tty
        old_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())

    print(f"btrack_clock | backend: {backend}")
    print(f"route audio into the '{args.client_name}' node and the MIDI clock "
          f"out of it in qpwgraph / helvum / aconnect")
    if link_pub is not None:
        print(f"ableton link: enabled (quantum {link_pub.quantum:g}) - "
              f"turn on Link in Reaper/Bitwig and they will find this session")
    if interactive:
        print("keys: [t]ap tempo  [h]alf  [d]ouble  [n]ormal  [f]reeze  "
              "[s]tart/stop  [r]eset  [q]uit")

    spinner = "|/-\\"
    n = 0
    try:
        while not stop_event.is_set():
            if interactive and select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1).lower()
                now = clock_now()
                if key == "q":
                    break
                elif key == "t":
                    bpm = worker.tap(now)
                    if bpm:
                        print(f"\n  tap tempo -> {bpm:.1f} bpm")
                elif key == "h":
                    engine.set_multiplier(0.5)
                elif key == "d":
                    engine.set_multiplier(2.0)
                elif key == "n":
                    engine.set_multiplier(1.0)
                elif key == "f":
                    state = engine.toggle_freeze()
                    print(f"\n  clock tempo {'frozen' if state else 'following again'}")
                elif key == "s":
                    engine.toggle_transport()
                elif key == "r":
                    worker.reset_request = True
                    print("\n  beat tracker reset")

            s = engine.snapshot()
            beat_flash = "*" if clock_now() - s["last_beat"] < 0.12 else " "
            lock = "LOCKED" if s["locked"] else "......"
            flags = []
            if s["freeze"]:
                flags.append("freeze")
            if s["multiplier"] != 1.0:
                flags.append(f"x{s['multiplier']:g}")
            flags.append("run" if s["running"] else "stop")
            link_part = (f"link {link_pub.peers}p | " if link_pub is not None else "")
            line = (f"\r{spinner[n % 4]} beat {beat_flash} "
                    f"track {s['tracker_bpm']:6.1f} bpm | "
                    f"clock {s['clock_bpm']:6.1f} bpm [{lock}] | "
                    f"{link_part}in {s['input_db']:6.1f} dB | {' '.join(flags)}   ")
            sys.stdout.write(line)
            sys.stdout.flush()
            n += 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        # give the backend a moment to deliver a final MIDI Stop
        if engine.snapshot()["running"]:
            with engine.lock:
                engine.pending_stop = True
            time.sleep(0.2)
        stop_event.set()
        if old_attrs is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        print("\nbye")


# ======================================================================
# Self test: synthetic audio through the whole tracker, no sound card
# ======================================================================
def run_selftest():
    print("self test: synthesizing 40 s of 125 bpm audio at 44.1 kHz ...")
    sr = int(BTRACK_SR)
    bpm_true = 125.0
    duration = 40.0
    n_samples = int(sr * duration)
    rng = np.random.default_rng(42)
    audio = 0.002 * rng.standard_normal(n_samples)

    beat_interval = 60.0 / bpm_true
    t_click = np.arange(int(0.03 * sr)) / sr
    click = (np.exp(-t_click * 60.0)
             * (0.7 * np.sin(2 * np.pi * 180.0 * t_click)
                + 0.5 * rng.standard_normal(len(t_click))))
    pos = 0.0
    k = 0
    while pos < duration - 0.1:
        i = int(pos * sr)
        amp = 1.0 if k % 4 == 0 else 0.6
        audio[i:i + len(click)] += amp * click[: n_samples - i]
        pos += beat_interval
        k += 1

    bt = BTrack(512)
    beats = []
    t0 = time.perf_counter()
    hops = n_samples // 512
    for h in range(hops):
        bt.process_audio_frame(audio[h * 512:(h + 1) * 512])
        if bt.beat_due_in_frame:
            beats.append(h * 512 / sr)
    elapsed = time.perf_counter() - t0

    tempo = bt.estimated_tempo
    intervals = np.diff([b for b in beats if b > 10.0])
    median_ibi = float(np.median(intervals)) if len(intervals) else 0.0
    ibi_bpm = 60.0 / median_ibi if median_ibi else 0.0
    speed = hops / elapsed
    realtime_factor = speed / (sr / 512)

    print(f"  detected beats:        {len(beats)}")
    print(f"  estimated tempo:       {tempo:.2f} bpm (true {bpm_true})")
    print(f"  median beat interval:  {median_ibi * 1000:.1f} ms -> {ibi_bpm:.2f} bpm")
    print(f"  processing speed:      {speed:.0f} hops/s "
          f"({realtime_factor:.0f}x realtime)")

    ok_tempo = abs(tempo - bpm_true) < 4.0
    ok_ibi = abs(ibi_bpm - bpm_true) < 2.5
    ok_speed = realtime_factor > 3.0
    for label, ok in (("tempo estimate", ok_tempo),
                      ("beat intervals", ok_ibi),
                      ("realtime headroom", ok_speed)):
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
    return 0 if (ok_tempo and ok_ibi and ok_speed) else 1


# ======================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Real-time beat tracker (BTrack) to MIDI clock for PipeWire.",
        epilog="Route any audio source into '<client>:audio_in' with qpwgraph, "
               "and '<client>:midi_clock_out' into Reaper/Bitwig/OP-Z.")
    parser.add_argument("--backend", choices=("auto", "pipewire", "jack", "alsa"),
                        default="auto",
                        help="audio/midi backend (default: try pipewire, then "
                             "jack, then alsa)")
    parser.add_argument("--pw-target", default=None,
                        help="pipewire backend: node to capture from (default: "
                             "the default source; re-route in qpwgraph anytime)")
    parser.add_argument("--client-name", default="BTrack",
                        help="client name shown in qpwgraph (default: BTrack)")
    parser.add_argument("--min-bpm", type=float, default=80.0,
                        help="fold detected tempo up to at least this (default: 80)")
    parser.add_argument("--max-bpm", type=float, default=160.0,
                        help="fold detected tempo down to at most this (default: 160)")
    parser.add_argument("--phase-gain", type=float, default=0.15,
                        help="how strongly ticks are pulled onto detected beats, "
                             "0..1 (default: 0.15)")
    parser.add_argument("--offset-ms", type=float, default=0.0,
                        help="shift the clock relative to detected beats, in ms "
                             "(negative = clock earlier)")
    parser.add_argument("--link", action="store_true",
                        help="also publish the clock as an Ableton Link session "
                             "(for Reaper/Bitwig and other Link-capable apps)")
    parser.add_argument("--link-quantum", type=float, default=4.0,
                        help="Ableton Link quantum in beats (default: 4)")
    parser.add_argument("--no-start-stop", action="store_true",
                        help="never send MIDI Start/Stop or Link transport, "
                             "only tempo/clock")
    parser.add_argument("--no-keys", action="store_true",
                        help="disable keyboard controls")
    parser.add_argument("--input-device", default=None,
                        help="ALSA backend: sounddevice input device name/index")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio devices (ALSA backend) and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run the beat tracker on synthetic audio and exit")
    args = parser.parse_args()

    if args.max_bpm < args.min_bpm * 2:
        parser.error("--max-bpm must be at least twice --min-bpm "
                     "(the fold range must span an octave)")

    if args.selftest:
        sys.exit(run_selftest())

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        sys.exit(0)

    if args.link:
        try:
            import link  # noqa: F401
        except ImportError as exc:
            print(f"error: --link needs the LinkPython-extern package ({exc}).\n"
                  "Run this script with uv ('uv run btrack_clock.py') so all "
                  "dependencies are installed automatically.", file=sys.stderr)
            sys.exit(1)

    def make_worker(sample_rate, audio_queue, stop_event):
        engine = ClockEngine(
            min_bpm=args.min_bpm, max_bpm=args.max_bpm,
            phase_gain=args.phase_gain, offset_s=args.offset_ms / 1000.0,
            send_start_stop=not args.no_start_stop)
        worker = BeatWorker(engine, sample_rate, audio_queue, stop_event)
        return engine, worker

    backends = {"pipewire": run_pipewire, "jack": run_jack, "alsa": run_alsa}
    order = (["pipewire", "jack", "alsa"] if args.backend == "auto"
             else [args.backend])
    for i, name in enumerate(order):
        try:
            sys.exit(backends[name](args, make_worker))
        except Exception as exc:
            if i < len(order) - 1:
                print(f"{name} backend unavailable ({exc}), "
                      f"trying {order[i + 1]} ...")
            else:
                print(f"error: {name} backend failed: {exc}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
