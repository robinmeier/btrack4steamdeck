# btrack4steamdeck

Real-time beat tracking to MIDI clock for the Steam Deck (and any PipeWire
Linux box), in **one self-contained Python script**.

Play music from YouTube, Qobuz, Spotify, anything — route its audio into the
`BTrack` node in qpwgraph — and get a MIDI clock out that syncs Reaper,
Bitwig, an OP-Z over USB, or any other MIDI-clock slave.

```
browser / qobuz ──► PipeWire ──► [BTrack:audio_in]
                                      │  beat tracking (BTrack algorithm)
                                      ▼
                       [BTrack:midi_clock_out] ──► Reaper / Bitwig / OP-Z
```

The beat tracker is a faithful numpy port of
[BTrack](https://github.com/adamstark/BTrack) by Adam Stark, Matthew Davies
and Mark Plumbley. The port is validated against the original C++
hop-by-hop (max relative error ~1e-14, identical beat decisions — see
`tests/`), so no C++ compiler, FFTW or libsamplerate is needed on the Deck.

## Quick start (Steam Deck)

In Desktop Mode, open Konsole. Everything is user-space — no
`steamos-readonly disable`, no pacman, no flatpak needed.

1. Install [uv](https://docs.astral.sh/uv/) (once):

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Run the script (the promised one-liner — uv fetches Python and all
   dependencies automatically on first run):

   ```sh
   uv run https://raw.githubusercontent.com/robinmeier/btrack4steamdeck/main/btrack_clock.py
   ```

   Or clone the repo and `uv run btrack_clock.py` / `./btrack_clock.py`.

3. Route things in **qpwgraph** (or Helvum):
   - connect your audio source's output (e.g. *Firefox*) to **BTrack:audio_in**.
     Tip: also leave the source connected to your speakers so you still hear it.
   - connect **BTrack:midi_clock_out** to your destination:
     - **OP-Z**: plug it in via USB; PipeWire bridges its ALSA MIDI port into
       the graph — connect directly to it.
     - **Reaper**: Options → Preferences → MIDI Inputs → enable the port with
       *control + clock sync*; then set the tempo source to MIDI clock
       (Project Settings) or use a control surface sync plugin.
     - **Bitwig**: Settings → Synchronization → MIDI Clock In on that port.

4. Watch the terminal: once the tracker locks (`[LOCKED]`), the clock is
   running and a MIDI Start is sent.

## What it does

- **Audio in / MIDI out via pipewire-jack** — appears as a `BTrack` client
  with an `audio_in` and a `midi_clock_out` port, with sample-accurate clock
  ticks generated in the JACK process callback. If libjack isn't available
  it falls back automatically to ALSA (`sounddevice` capture +
  `python-rtmidi` virtual MIDI port), which PipeWire bridges into the graph
  as well.
- **24 ppqn MIDI clock** with a PLL-style follower: the tempo is estimated
  from median inter-beat intervals (finer resolution than the tracker's
  internal quantized tempo), and tick phase is gently nudged onto detected
  beats, spread across the beat so receivers never see a jump.
- **MIDI Start/Stop**: Start is sent (aligned to a beat) once tracking locks,
  Stop after ~3 s of silence or on exit. Disable with `--no-start-stop`.
- **Octave guard**: detected tempo is folded into `--min-bpm`/`--max-bpm`
  (default 80–160) to avoid half/double-tempo locks.
- **Live display + keys**: current tracker BPM, clock BPM, lock state, input
  level. Keys: `t` tap tempo (re-seeds the tracker), `h`/`d`/`n`
  half/double/normal speed, `f` freeze tempo, `s` manual start/stop,
  `r` reset tracker, `q` quit.

## Options

```
--backend auto|jack|alsa   backend selection (default: auto)
--client-name NAME         node name in qpwgraph (default: BTrack)
--min-bpm / --max-bpm      tempo fold range, must span an octave (80–160)
--phase-gain G             beat-phase correction strength 0..1 (0.15)
--offset-ms MS             shift clock vs. detected beats (negative = earlier)
--no-start-stop            send only clock ticks, never Start/Stop
--no-keys                  disable keyboard controls
--input-device DEV         ALSA backend: capture device
--list-devices             list ALSA capture devices
--selftest                 run the tracker on synthetic audio, no sound card
```

## Latency and behavior notes

- Analysis hops are 512 samples (~11.6 ms at 44.1 kHz); BTrack predicts beats
  ahead of time, so once locked the clock ticks **on** the beat, not after it.
  Expect 2–6 seconds to lock onto new material.
- If your synth chain ends up consistently early/late (receiver latency,
  Bluetooth audio, etc.), trim with `--offset-ms`.
- Beat tracking works best on rhythmic material. For beatless ambient, tap
  `t` a few times to seed the tempo manually.
- The graph's sample rate doesn't matter — input is resampled internally to
  the 44.1 kHz the algorithm is defined at.

## Tests

```sh
uv run btrack_clock.py --selftest             # tracker on synthetic audio
uv run --with numpy python3 tests/test_clock_engine.py   # clock PLL behavior
uv run --with numpy python3 tests/compare_with_cpp.py    # vs. original C++ (needs g++)
```

## License

GPLv3, like the BTrack algorithm it ports. See `LICENSE`.
