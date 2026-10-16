"""Read-only adapter for the private, version-dependent Voice Memos database."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import unquote, urlparse


class LibraryError(Exception):
    """Actionable library access or schema error."""


@dataclass(frozen=True)
class Memo:
    id: str
    title: str
    path: Path | None
    duration_seconds: float | None

    def to_dict(self):
        return {**asdict(self), "path": str(self.path) if self.path else None,
                "available_locally": self.path is not None}


def locate_database(directory: Path | None = None) -> Path:
    roots = [directory.expanduser()] if directory else [
        Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
        Path.home() / "Library/Application Support/com.apple.voicememos/Recordings",
    ]
    denied = []
    for root in roots:
        for database in [root / "CloudRecordings.db", root.parent / "CloudRecordings.db"]:
            try:
                # Opening explicitly avoids exists() silently hiding access failures.
                with database.open("rb"):
                    pass
                return database.resolve()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except PermissionError:
                denied.append(str(database))
            except OSError as error:
                raise LibraryError(f"Cannot open Voice Memos database {database}: {error}") from error
    if denied:
        raise LibraryError(
            "Access denied to the Voice Memos library. Run --request-access from your "
            "Terminal, grant that terminal app Full Disk Access in System Settings, "
            "then quit/reopen it and retry. An agent sandbox can independently block access."
        )
    raise LibraryError(
        "Voice Memos database not found. Open Voice Memos and sync your recordings, "
        "or specify --library-dir pointing to the Recordings folder."
    )


def audio_path(value, database: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlparse(value)
    raw = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(value)
    roots = [database.parent, database.parent / "Recordings"]
    candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
    candidates += [root / raw.name for root in roots]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def read_memos(directory: Path | None = None) -> list[Memo]:
    database = locate_database(directory)
    try:
        # mode=ro preserves SQLite WAL visibility and never writes Apple's database.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
            title_columns = [name for name in (
                "ZENCRYPTEDTITLE", "ZCUSTOMLABELFORSORTING", "ZCUSTOMLABEL", "ZTITLE"
            ) if name in columns]
            if "ZPATH" not in columns or not title_columns:
                raise LibraryError("Unsupported Voice Memos database schema: expected ZPATH and a title column.")
            selected = ["ZPATH", *title_columns]
            selected += [name for name in ("Z_PK", "ZDURATION", "ZMARKEDFORDELETION") if name in columns]
            # All identifiers come from a fixed allowlist above.
            rows = conn.execute("SELECT " + ", ".join(selected) + " FROM ZCLOUDRECORDING").fetchall()
    except sqlite3.Error as error:
        raise LibraryError(f"Cannot read Voice Memos database: {error}. Check --check-access.") from error
    memos = []
    for row in rows:
        if "ZMARKEDFORDELETION" in columns and row["ZMARKEDFORDELETION"]:
            continue
        title = next((row[col].strip() for col in title_columns
                      if isinstance(row[col], str) and row[col].strip()), None)
        if not title:
            title = str(row["ZPATH"] or "Untitled")
        memos.append(Memo(
            id=(str(row["Z_PK"]) if "Z_PK" in columns else
                hashlib.sha256(str(row["ZPATH"]).encode()).hexdigest()[:16]),
            title=title, path=audio_path(row["ZPATH"], database),
            duration_seconds=row["ZDURATION"] if "ZDURATION" in columns else None,
        ))
    return sorted(memos, key=lambda memo: (memo.title.casefold(), memo.id))


def select_memo(memos: list[Memo], title: str | None, memo_id: str | None) -> Memo:
    if title is None and memo_id is None:
        raise ValueError("select_memo requires a title or a memo_id.")
    matches = [memo for memo in memos if (
        memo.id == memo_id if memo_id is not None else memo.title.casefold() == title.casefold()
    )]
    if not matches:
        raise LibraryError(f"No memo matches {memo_id or title!r}. Use --list-memos to inspect titles and IDs.")
    if len(matches) > 1:
        raise LibraryError(f"Multiple memos match {title!r}; select with --memo-id: "
                           + ", ".join(memo.id for memo in matches))
    if matches[0].path is None:
        raise LibraryError("The memo exists, but its audio is not downloaded locally. "
                           "Download it in Voice Memos and retry.")
    return matches[0]


def check_access(directory: Path | None = None) -> dict:
    try:
        database = locate_database(directory)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return {"accessible": True, "database": str(database)}
    except (LibraryError, OSError, sqlite3.Error) as error:
        return {"accessible": False, "error": str(error)}


def request_access(directory: Path | None = None) -> dict:
    status = check_access(directory)
    if status["accessible"]:
        return status
    status["settings_opened"] = False
    if sys.platform == "darwin":
        try:
            process = subprocess.run(
                ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            status["settings_opened"] = process.returncode == 0
            if process.returncode:
                status["settings_error"] = process.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            status["settings_error"] = str(error)
    status["next_step"] = (
        "In System Settings > Privacy & Security > Full Disk Access, enable the app "
        "running this CLI (for example Terminal or iTerm). Quit/reopen that app, then "
        "rerun --check-access. macOS requires this explicit user action; code cannot grant it."
    )
    return status
