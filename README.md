# btrack4steamdeck

Real-time beat tracking to MIDI clock for the Steam Deck (and any PipeWire
Linux box), in **one self-contained Python script**.

Play music from YouTube, Qobuz, Spotify, anything — route its audio into the
`BTrack` node in qpwgraph — and get a MIDI clock (for the OP-Z and other
hardware) plus, with `--link`, an Ableton Link session (for Reaper, Bitwig
and anything else Link-capable).

```
browser / qobuz ──► PipeWire ──► [BTrack:audio_in]
                                      │  beat tracking (BTrack algorithm)
                                      ├──► [BTrack:midi_clock_out] ──► OP-Z / hardware
                                      └──► Ableton Link (--link)    ──► Reaper / Bitwig
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
   - **OP-Z / hardware**: plug it in via USB; PipeWire bridges its ALSA MIDI
     port into the graph — connect **BTrack:midi_clock_out** directly to it.
   - **Reaper / Bitwig**: use Ableton Link instead of MIDI clock — run with
     `--link` and enable Link in the DAW (Reaper: Link toggle in the menu /
     preferences; Bitwig: Dashboard → Settings → Synchronization → Ableton
     Link). The session is discovered automatically, no routing needed.

4. Watch the terminal: once the tracker locks (`[LOCKED]`), the clock is
   running, a MIDI Start is sent and the Link transport starts.

### Why Link for the DAWs?

MIDI clock slaving in DAWs is fragile: Bitwig only reads ALSA MIDI devices,
so it never sees a JACK MIDI port (workaround: `--backend alsa`), and
Reaper's MIDI-clock tempo follower is jittery by design (it chases raw tick
timing). Ableton Link carries tempo *and* beat phase on the network with
proper smoothing on the receiving side, and both Reaper and Bitwig support
it natively — so `--link` is the recommended way to sync them, while the
MIDI clock keeps driving hardware like the OP-Z. Both outputs run from the
same internal clock and stay in step with each other.

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
- **Ableton Link** (`--link`): publishes the same clock as a Link session
  using the official Link library (via `LinkPython-extern`, prebuilt wheels).
  Tempo and beat phase are pushed into the session the way Link's API
  documents for bridging an external clock (periodic `forceBeatAtTime`),
  and transport start/stop is mirrored. The status line shows the number of
  connected Link peers.
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
--link                     also publish an Ableton Link session
--link-quantum N           Link quantum in beats (default 4)
--no-start-stop            never send MIDI Start/Stop or Link transport
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
uv run --with numpy --with LinkPython-extern python3 tests/test_link_publisher.py  # Link session
```

## License

GPLv3, like the BTrack algorithm it ports. See `LICENSE`.
