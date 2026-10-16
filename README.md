# Voice Memo Transcriber

A free CLI for transcribing Apple Voice Memos on your Mac. Select a recording by
title or ID, or pass exported audio files and folders. Audio is processed locally.
The first run downloads a Whisper model; subsequent runs can use `--offline`.
No account or API key is needed.

## Grant library access

In Terminal:

```sh
cd ~/code/hacktoolkit/mac-tools/voice-memo-transcriber
./setup-access
```

The helper opens **System Settings → Privacy & Security → Full Disk Access**,
identifies common terminal apps, and checks access again when you press Return.
Enable the terminal app running the command, authenticate if asked, and quit/reopen
that app if macOS requests it. If the terminal closes, run `./setup-access` again.
This helper needs only Python; install the speech engine afterward.

macOS does not provide a programmatic Full Disk Access grant. The user must grant
it explicitly in System Settings ([Apple documentation](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox)).
An agent sandbox can separately block library reads or opening Settings even when
your normal Terminal has access. Run the helper in that Terminal in this case.

## Install and transcribe

Python 3.10+ is required. If dependency wheels are unavailable for your newest
Python, use Python 3.13 via `make install PYTHON=python3.13`.

```sh
make install
./transcribe --memo 'Project feedback from Alex' --format all --language en \
  --prompt 'Project Atlas, Alex'
```

Library transcripts go into `./transcripts/`, named using the memo title and its
library row ID. The audio and library database are never modified. All three
formats are produced with `--format all`: `.txt`, timestamped `.srt`, and `.json`.
Use `--model small` for a larger model; `base` is the default. Recognition quality
depends on the model, language, noise, and speaker clarity; this does not identify speakers.

```sh
# Discover available titles and IDs (JSON; no speech dependencies needed).
./transcribe --list-memos
./transcribe --memo-id 42 --format all

# Exported files and directories also work.
./transcribe '/path/to/Meeting.m4a'
./transcribe '/path/to/memos' --recursive --output-dir ./transcripts

# Reuse a downloaded model with networking disabled at the model loader.
./transcribe --memo 'Project feedback from Alex' --offline

# Access diagnostics for scripts; exit code 3 means unavailable.
./transcribe --check-access
./transcribe --request-access
```

The app must have downloaded the recording locally. A metadata entry in iCloud
alone is insufficient. Duplicate titles are rejected with IDs to choose from.
Title matching is exact and case-insensitive. Apple's database is a private
interface; schema changes may require adapter updates. The reader recognizes
several known title columns and checks the schema before querying.

For custom library locations, pass `--library-dir /path/to/Recordings`.
For audio paths, outputs default to the same folder as the input, with names like
`Meeting.m4a.txt`. A batch output directory preserves paths relative to each input
folder. Output name collisions are rejected before transcription.

Existing complete output sets are skipped. Partial output sets require a different
directory or `--overwrite`; existing transcripts are never replaced implicitly.
Each output is published atomically, but the set of three formats is not a single
transaction. If a write fails midway, rerun with `--overwrite` after fixing the cause.

## Pipeline interface

```sh
./transcribe --memo 'Project feedback from Alex' --format all --jsonl > result.jsonl
```

`--jsonl` writes one JSON object per completed, skipped, or failed audio file to
stdout. Successful objects include `status`, `source`, `model`, `text`, detected
`language`, `language_probability`, `duration_seconds`, timestamped `segments`,
`outputs`, and library `memo` metadata when applicable. Skips have `status`, `source`,
and `outputs`; file failures have `status`, `source`, and `error`.
Progress and diagnostics go to stderr. Startup failures (invalid arguments,
inaccessible library, missing dependencies/model) produce no JSONL records;
always check the process exit code. Without `--jsonl`, stdout lists written paths.

Exit codes: `0` success or all skipped; `1` model/dependency/transcription/output
failure; `2` invalid arguments/input/output conflicts; `3` library access,
lookup, schema, or permission failure; `130` interrupted (module/launcher invocation).

Python callers can use `memo_library.read_memos`, `select_memo`, and
`voice_memos.transcribe` independently. The latter accepts a loaded engine so a
long-running pipeline can reuse it across recordings.

## Development

```sh
make test
make package
```

After installation, run `make test PYTHON=.venv/bin/python` to include the real
PyAV decoder tests (generated mono/stereo audio, resampling, silence, and errors).
They skip explicitly if the speech dependencies are not installed.

Tests use synthetic SQLite databases and mocked transcription responses under
`HOLODECK` (default `~/.holodeck`). To use a writable alternate sandbox:
`HOLODECK=/private/tmp/mac-tools-holodeck make test`.
These verify library lookup, pipeline behavior, and safe output handling; they
do not measure speech recognition accuracy. A real-audio smoke test additionally
requires the engine, downloaded model, and access to the chosen memo.

Speech recognition uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
on the CPU with int8 computation. Its PyAV dependency supplies audio decoding;
a separate system FFmpeg installation is unnecessary. Models are cached in
`.models` beside the Python module by default; override with `--model-dir`.

The tool decodes audio to 16 kHz mono samples before calling Whisper. This avoids
the [faster-whisper 1.2.1 / PyAV 19 incompatibility](https://github.com/SYSTRAN/faster-whisper/issues/1589)
in its bundled file decoder. Apple library `.qta` audio files are also accepted.

MIT licensed; dependencies and model weights retain their own licenses.
