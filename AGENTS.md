# Voice Memo Transcriber

- Python 3.10+, standard library except for the optional-at-import transcription engine.
- Run `make test`; library tests use synthetic SQLite/audio fixtures under `HOLODECK`.
- Keep Apple's library read-only. Never write transcripts into it.
- The database format is private/version-dependent: validate columns and fail clearly.
- Diagnostics and discovery must work before installing faster-whisper.
- Preserve CLI exit codes and JSON stdout contracts documented in README.md.
- Never claim real transcription was verified using only a mocked engine.
