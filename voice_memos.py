"""Local transcription of exported Voice Memos, individually or in batches."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from memo_library import LibraryError, check_access, read_memos, request_access, select_memo
from audio_decoder import decode_audio

AUDIO_EXTENSIONS = {".m4a", ".qta", ".mp3", ".wav", ".aiff", ".aif", ".caf", ".flac", ".ogg", ".mp4"}


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def discover(inputs: list[str], recursive: bool) -> list[tuple[Path, Path]]:
    """Return unique audio paths and relative names for output organization."""
    found: dict[Path, Path] = {}
    for value in inputs:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Input does not exist: {path}")
        if path.is_file():
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                raise ValueError(f"Unsupported audio extension: {path}")
            found.setdefault(path, Path(path.name))
        elif path.is_dir():
            candidates = path.rglob("*") if recursive else path.iterdir()
            for candidate in sorted(candidates):
                if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
                    found.setdefault(candidate.resolve(), candidate.relative_to(path))
        else:
            raise ValueError(f"Input is not a file or directory: {path}")
    if not found:
        raise ValueError("No supported audio files found. Export your memo as an audio file first.")
    return list(found.items())


def plan_outputs(files, output_dir: Path | None, formats: list[str]):
    plans = []
    owners = {}
    for source, relative in files:
        base = output_dir / relative if output_dir else source
        targets = {fmt: base.with_name(base.name + "." + fmt) for fmt in formats}
        for target in targets.values():
            # casefold also catches collisions on default macOS filesystems.
            key = str(target).casefold()
            if key in owners and owners[key] != source:
                raise ValueError(f"Output name collision: {source} and {owners[key]} → {target}")
            owners[key] = source
        plans.append((source, targets))
    return plans


def render(result: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if fmt == "txt":
        return result["text"] + "\n"
    if fmt == "srt":
        return "".join(
            f"{i}\n{timestamp(segment['start'])} --> {timestamp(segment['end'])}\n"
            f"{segment['text']}\n\n"
            for i, segment in enumerate(result["segments"], 1)
        )
    raise ValueError(f"Unknown output format: {fmt}")


def write_output(path: Path, content: str, overwrite: bool):
    """Publish a complete UTF-8 file; never clobber an existing file by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".transcript-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def transcribe(model, source: Path, language: str | None, prompt: str | None,
               model_name: str) -> dict:
    segments, info = model.transcribe(
        decode_audio(source), language=language, initial_prompt=prompt,
        beam_size=5, vad_filter=True,
    )
    records = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            records.append({"start": segment.start, "end": segment.end, "text": text})
        print(f"\r  Processed {segment.end:.0f} / {info.duration:.0f} seconds",
              end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return {
        "source": str(source), "model": model_name,
        "language": info.language, "language_probability": info.language_probability,
        "duration_seconds": info.duration,
        "text": " ".join(record["text"] for record in records), "segments": records,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Transcribe Apple Voice Memos locally, by title or audio path.",
        epilog="Audio stays on this computer. A model is downloaded on first use.",
    )
    result.add_argument("inputs", nargs="*", help="Audio files or folders (quote paths containing spaces)")
    source = result.add_mutually_exclusive_group()
    source.add_argument("--memo", help="Exact Voice Memos title (case-insensitive)")
    source.add_argument("--memo-id", help="Library row ID from --list-memos")
    source.add_argument("--list-memos", action="store_true", help="List library recordings as JSON")
    source.add_argument("--check-access", action="store_true", help="Check library access; output JSON")
    source.add_argument("--request-access", action="store_true", help="Check access and open macOS permission settings if needed")
    result.add_argument("--library-dir", type=Path, help="Override the Voice Memos Recordings directory")
    result.add_argument("--jsonl", action="store_true", help="Emit one JSON status record per file on stdout for pipelines")
    result.add_argument("-o", "--output-dir", type=Path, help="Write here instead of next to each audio file")
    result.add_argument("-r", "--recursive", action="store_true", help="Include subfolders")
    result.add_argument("--format", choices=["txt", "srt", "json", "all"], default="txt")
    result.add_argument("--model", default="base", help="Whisper model name or local model directory (default: base)")
    result.add_argument("--language", help="Language code, such as en; default: detect automatically")
    result.add_argument("--prompt", help="Optional vocabulary/context to help recognize names")
    result.add_argument("--model-dir", type=Path,
                        default=Path(__file__).resolve().parent / ".models", help="Model download/cache directory")
    result.add_argument("--offline", action="store_true", help="Only use an already downloaded/local model")
    result.add_argument("--overwrite", action="store_true", help="Replace existing transcript files")
    return result


def main(argv: list[str] | None = None) -> int:
    args_parser = parser()
    args = args_parser.parse_args(argv)
    formats = ["txt", "srt", "json"] if args.format == "all" else [args.format]
    library_mode = args.memo is not None or args.memo_id is not None
    if args.inputs and (library_mode or args.list_memos or args.check_access or args.request_access):
        args_parser.error("Audio paths cannot be combined with a library action.")
    if args.check_access or args.request_access:
        status = (request_access if args.request_access else check_access)(args.library_dir)
        print(json.dumps(status, indent=2))
        return 0 if status["accessible"] else 3
    if args.list_memos:
        try:
            print(json.dumps([memo.to_dict() for memo in read_memos(args.library_dir)], indent=2))
            return 0
        except (LibraryError, OSError) as error:
            print(json.dumps({"error": str(error)}))
            return 3
    if not args.inputs and not library_mode:
        args_parser.error("Provide audio paths, --memo TITLE, or a library action such as --list-memos.")
    memo = None
    try:
        if library_mode:
            memo = select_memo(read_memos(args.library_dir), args.memo, args.memo_id)
            safe_title = re.sub(r'[^\w .-]', '_', memo.title).strip(' .')[:150] or "Memo"
            safe_id = re.sub(r'[^\w-]', '_', memo.id)[:80]
            files = [(memo.path, Path(f"{safe_title}--{safe_id}{memo.path.suffix}"))]
        else:
            files = discover(args.inputs, args.recursive)
        output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
        if library_mode and output_dir is None:
            output_dir = Path.cwd() / "transcripts"
        plans = plan_outputs(files, output_dir, formats)
    except LibraryError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, OSError) as error:
        args_parser.error(str(error))

    pending = []
    for source, targets in plans:
        existing = [p for p in targets.values() if p.exists() or p.is_symlink()]
        if existing and not args.overwrite:
            if len(existing) == len(targets) and all(p.is_file() for p in existing):
                print(f"Skipping {source.name}: transcripts already exist.", file=sys.stderr)
                if args.jsonl:
                    print(json.dumps({"status": "skipped", "source": str(source),
                                      "outputs": {fmt: str(path) for fmt, path in targets.items()}}))
                continue
            args_parser.error(f"Some output paths already exist for {source.name}. "
                              "Choose another output directory or use --overwrite.")
        pending.append((source, targets))
    if not pending:
        return 0

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Missing transcription dependencies. Run make install in the tool directory.", file=sys.stderr)
        return 1

    print(f"Loading model {args.model!r} on CPU. "
          + ("Offline mode." if args.offline else "First use may download the model."), file=sys.stderr)
    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             download_root=str(args.model_dir.expanduser()),
                             local_files_only=args.offline)
    except Exception as error:
        print(f"Could not load model: {error}\n"
              "For a first download, check your connection and omit --offline. "
              "You can also pass a local converted model directory with --model.", file=sys.stderr)
        return 1

    failures = 0
    for index, (source, targets) in enumerate(pending, 1):
        print(f"[{index}/{len(pending)}] Transcribing {source.name}", file=sys.stderr)
        try:
            result = transcribe(model, source, args.language, args.prompt, args.model)
            if memo:
                result["memo"] = memo.to_dict()
            for fmt, target in targets.items():
                write_output(target, render(result, fmt), args.overwrite)
                if not args.jsonl:
                    print(target)
            if args.jsonl:
                print(json.dumps({"status": "ok", **result,
                                  "outputs": {fmt: str(path) for fmt, path in targets.items()}},
                                 ensure_ascii=False), flush=True)
            if not result["segments"]:
                print("  No speech detected; transcript is empty.", file=sys.stderr)
        except Exception as error:
            failures += 1
            print(f"Failed {source.name}: {error}", file=sys.stderr)
            if args.jsonl:
                print(json.dumps({"status": "error", "source": str(source), "error": str(error)}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nTranscription cancelled.", file=sys.stderr)
        sys.exit(130)
