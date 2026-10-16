PYTHON ?= python3
HOLODECK ?= $(HOME)/.holodeck
export HOLODECK

.PHONY: help install access test package

help:
	@echo 'make access    Open macOS permissions and verify library access'
	@echo 'make install   Create .venv and install local transcription dependencies'
	@echo 'make test      Run isolated tests; no model or network required'
	@echo 'make package   Build a source archive in dist/'
	@echo './transcribe --help   Show CLI options'

access:
	./setup-access

install:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -e .

test:
	$(PYTHON) -m unittest discover -s tests -v

package:
	@mkdir -p dist
	tar --exclude='__pycache__' -czf dist/voice-memo-transcriber.tar.gz README.md LICENSE AGENTS.md Makefile pyproject.toml voice_memos.py memo_library.py audio_decoder.py setup-access transcribe tests
