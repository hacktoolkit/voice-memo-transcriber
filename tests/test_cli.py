from contextlib import closing, redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import memo_library as library
import voice_memos as cli


class TranscriberTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("HOLODECK", "~/.holodeck")).expanduser() / "voice-memo-transcriber"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.recordings = self.root / "Recordings"
        self.recordings.mkdir()
        self.audio = self.recordings / "recording.m4a"
        self.audio.write_bytes(b"synthetic audio fixture")
        with closing(sqlite3.connect(self.recordings / "CloudRecordings.db")) as conn, conn:
            conn.execute("CREATE TABLE ZCLOUDRECORDING (Z_PK INTEGER, ZPATH TEXT, "
                         "ZENCRYPTEDTITLE TEXT, ZCUSTOMLABEL TEXT, ZDURATION REAL, "
                         "ZMARKEDFORDELETION INTEGER)")
            conn.execute("INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?)",
                         (7, self.audio.name, "Project feedback from Alex", "2026-10-16", 65.2, 0))

    def run_cli(self, args, engine=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        engine = engine or SimpleNamespace(transcribe=lambda *args, **kwargs: (
            iter([SimpleNamespace(start=0.0, end=2.2, text=" Hello Alex. ")]),
            SimpleNamespace(language="en", language_probability=0.99, duration=3.0),
        ))
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=lambda *a, **k: engine)}), \
                patch.object(cli, "decode_audio", side_effect=lambda path: str(path)):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_modern_title_and_relative_path(self):
        memos = library.read_memos(self.recordings)
        memo = library.select_memo(memos, "project feedback from alex", None)
        self.assertEqual(memo.id, "7")
        self.assertEqual(memo.path, self.audio)
        self.assertTrue(memo.to_dict()["available_locally"])

    def test_legacy_title_and_file_url(self):
        with closing(sqlite3.connect(self.recordings / "CloudRecordings.db")) as conn, conn:
            conn.execute("DROP TABLE ZCLOUDRECORDING")
            conn.execute("CREATE TABLE ZCLOUDRECORDING (Z_PK INTEGER, ZPATH TEXT, ZCUSTOMLABEL TEXT)")
            conn.execute("INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?)", (1, self.audio.as_uri(), "Legacy memo"))
        self.assertEqual(library.read_memos(self.recordings)[0].path, self.audio)

    def test_ambiguous_title_requires_id(self):
        with closing(sqlite3.connect(self.recordings / "CloudRecordings.db")) as conn, conn:
            conn.execute("INSERT INTO ZCLOUDRECORDING SELECT 8, ZPATH, ZENCRYPTEDTITLE, "
                         "ZCUSTOMLABEL, ZDURATION, 0 FROM ZCLOUDRECORDING")
        memos = library.read_memos(self.recordings)
        with self.assertRaisesRegex(library.LibraryError, "Multiple memos"):
            library.select_memo(memos, "Project feedback from Alex", None)
        self.assertEqual(library.select_memo(memos, None, "8").id, "8")

    def test_deleted_and_cloud_only_memos(self):
        with closing(sqlite3.connect(self.recordings / "CloudRecordings.db")) as conn, conn:
            conn.execute("INSERT INTO ZCLOUDRECORDING VALUES (8, 'gone.m4a', 'Deleted', '', 0, 1)")
        self.audio.unlink()
        memos = library.read_memos(self.recordings)
        self.assertEqual(len(memos), 1)
        with self.assertRaisesRegex(library.LibraryError, "not downloaded"):
            library.select_memo(memos, None, "7")

    def test_unsupported_schema(self):
        with closing(sqlite3.connect(self.recordings / "CloudRecordings.db")) as conn, conn:
            conn.execute("DROP TABLE ZCLOUDRECORDING")
        with self.assertRaisesRegex(library.LibraryError, "schema"):
            library.read_memos(self.recordings)

    def test_reading_does_not_change_database(self):
        database = self.recordings / "CloudRecordings.db"
        before = database.read_bytes()
        library.read_memos(self.recordings)
        self.assertEqual(database.read_bytes(), before)

    def test_access_check_permission_failure_is_structured(self):
        with patch.object(Path, "open", side_effect=PermissionError()):
            result = library.check_access(self.recordings)
        self.assertFalse(result["accessible"])
        self.assertIn("Full Disk Access", result["error"])

    def test_access_request_does_not_claim_to_grant_permission(self):
        with patch.object(library, "check_access", return_value={"accessible": False}), \
                patch.object(library.sys, "platform", "darwin"), \
                patch.object(library.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            result = library.request_access()
        self.assertTrue(result["settings_opened"])
        self.assertFalse(result["accessible"])
        self.assertIn("explicit user action", result["next_step"])

    def test_discovery_recursion_and_deduplication(self):
        sub = self.recordings / "sub"
        sub.mkdir()
        second = sub / "Second.M4A"
        second.touch()
        found = cli.discover([str(self.recordings), str(self.audio)], True)
        self.assertEqual(len(found), 2)
        self.assertIn((second, Path("sub/Second.M4A")), found)
        self.assertEqual(len(cli.discover([str(self.recordings)], False)), 1)

    def test_output_collisions_rejected(self):
        with self.assertRaisesRegex(ValueError, "collision"):
            cli.plan_outputs([(self.audio, Path("same.m4a")),
                              (self.root / "different.m4a", Path("SAME.m4a"))], self.root, ["txt"])

    def test_output_collision_through_symlinked_directory(self):
        inputs = self.root / "in"
        for folder in ("a", "b"):
            (inputs / folder).mkdir(parents=True)
            (inputs / folder / "memo.m4a").touch()
        out = self.root / "out"
        (out / "a").mkdir(parents=True)
        (out / "b").symlink_to("a", target_is_directory=True)
        files = cli.discover([str(inputs)], True)
        with self.assertRaisesRegex(ValueError, "collision"):
            cli.plan_outputs(files, out, ["txt"])
        with self.assertRaises(SystemExit) as caught:
            self.run_cli([str(inputs), "--recursive", "--output-dir", str(out), "--overwrite"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(list(out.rglob("*.txt")), [])

    def test_exclusive_publish_without_hard_links(self):
        target = self.root / "no-links.txt"
        refusal = OSError(errno.EPERM, "Operation not permitted")
        with patch.object(cli.os, "link", side_effect=refusal):
            cli.write_output(target, "first", False)
            self.assertEqual(target.read_text(), "first")
            with self.assertRaises(FileExistsError):
                cli.write_output(target, "second", False)
        self.assertEqual(target.read_text(), "first")
        self.assertEqual(list(self.root.glob(".transcript-*")), [])
        with patch.object(cli.os, "link", side_effect=OSError(errno.EACCES, "denied")), \
                self.assertRaises(PermissionError):
            cli.write_output(self.root / "other.txt", "x", False)

    def test_interrupt_exit_code_from_main(self):
        with patch.object(cli, "run", side_effect=KeyboardInterrupt):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([]), 130)

    def test_library_dir_pointing_at_file_is_library_error(self):
        stray = self.root / "stray.txt"
        stray.write_text("not a directory")
        with self.assertRaisesRegex(library.LibraryError, "not found"):
            library.locate_database(stray)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--memo", "Anything", "--library-dir", str(stray)])
        self.assertEqual(code, 3)

    def test_select_memo_requires_a_selector(self):
        with self.assertRaisesRegex(ValueError, "title or a memo_id"):
            library.select_memo([], None, None)

    def test_atomic_write_preserves_existing(self):
        target = self.root / "transcript.txt"
        cli.write_output(target, "original", False)
        with self.assertRaises(FileExistsError):
            cli.write_output(target, "replacement", False)
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(list(self.root.glob(".transcript-*")), [])
        cli.write_output(target, "replacement", True)
        self.assertEqual(target.read_text(), "replacement")

    def test_timestamp_rounding_carries_minutes(self):
        self.assertEqual(cli.timestamp(59.9999), "00:01:00,000")
        self.assertEqual(cli.timestamp(3600.123), "01:00:00,123")

    def test_named_memo_pipeline_all_formats_and_skip(self):
        out = self.root / "outputs"
        args = ["--memo", "Project feedback from Alex", "--library-dir", str(self.recordings),
                "--output-dir", str(out), "--format", "all", "--jsonl"]
        code, stdout, _ = self.run_cli(args)
        self.assertEqual(code, 0)
        record = json.loads(stdout)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["text"], "Hello Alex.")
        self.assertEqual(record["memo"]["id"], "7")
        self.assertEqual(Path(record["outputs"]["txt"]).read_text(), "Hello Alex.\n")
        self.assertIn("00:00:00,000 --> 00:00:02,200", Path(record["outputs"]["srt"]).read_text())
        self.assertEqual(json.loads(Path(record["outputs"]["json"]).read_text())["text"], "Hello Alex.")
        self.assertEqual(list(self.recordings.glob("*.txt")), [])
        code, stdout, _ = self.run_cli(args)
        skipped = json.loads(stdout)
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["memo"]["id"], "7")

    def test_partial_output_requires_explicit_overwrite(self):
        self.audio.with_name(self.audio.name + ".txt").write_text("original")
        with self.assertRaises(SystemExit) as caught:
            self.run_cli([str(self.audio), "--format", "all"])
        self.assertEqual(caught.exception.code, 2)

    def test_bad_audio_does_not_stop_batch(self):
        bad = self.recordings / "bad.m4a"
        bad.touch()
        def engine_transcribe(path, **kwargs):
            if Path(path).name == "bad.m4a":
                raise ValueError("Invalid audio")
            return iter([]), SimpleNamespace(language="en", language_probability=1.0, duration=0)
        code, stdout, _ = self.run_cli([str(self.recordings), "--jsonl"],
                                     SimpleNamespace(transcribe=engine_transcribe))
        self.assertEqual(code, 1)
        self.assertEqual([json.loads(line)["status"] for line in stdout.splitlines()], ["error", "ok"])

    def test_diagnostics_without_engine(self):
        code, stdout, _ = self.run_cli(["--list-memos", "--library-dir", str(self.recordings)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)[0]["title"], "Project feedback from Alex")


if __name__ == "__main__":
    unittest.main()
