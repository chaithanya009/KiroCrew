"""One backup cycle: what must be durable, and how each object is copied.

## What is in the set

Three kinds, and the set is a definition rather than a filter, so "was this file
backed up" has an answer that does not depend on what the directory happened to
hold:

1. **The two authority files**, ``session_map.json`` and ``open_slots.json``. They
   turn a slot id back into a conversation, so without them the transcripts are on
   disk and the conversation list is empty.
2. **Every live transcript**, the ``.jsonl`` files directly under the sessions
   directory. One per conversation this task has served.
3. **Every archived segment** under ``sessions/archive/``. Rotation moves the older
   part of a long conversation there and the container never reads it back -- the
   front's fetch may not list, and finding a segment requires listing -- so these are
   uploaded for the owner's control plane, which has credentials of its own. Leaving
   them behind would be silent loss of the older half of every long conversation.

Anything else under the sessions directory is not in the set. The front writes a
temporary file there while fetching and unlinks it itself, and a name that is
neither that nor a transcript is not a conversation.

## How one object is copied

``open_snapshot`` opens the file ONCE and records the length that descriptor's file
had at that moment. The upload then sends exactly that many bytes from that
descriptor. Three properties follow, and they are the three constraints this design
has to hold at the same time:

* **Consistent** without a lock. The backend publishes a transcript with a temporary
  file and a rename, so it never writes into the bytes behind an open descriptor --
  it swaps the directory entry to a different inode. A descriptor opened before the
  swap keeps addressing a whole, finished version, and a file that is appended to
  instead is uploaded as the prefix that existed at open time, which is also a
  version that was really on disk.
* **Bounded** on disk. Nothing is copied first. A cycle spends one descriptor and one
  fixed transport buffer per object, so an oversized artifact cannot fill the
  filesystem the app is writing to.
* **Nothing dropped.** There is no size at which an object is skipped. An entry that
  genuinely cannot be uploaded is recorded and the cycle ends by RAISING
  :class:`BackupIncomplete` -- after uploading everything it could, so one bad entry
  does not cost every other conversation its backup.

## Every shape an entry can have, and what happens to it

| entry                                      | verdict                                |
| ------------------------------------------ | -------------------------------------- |
| regular file, one link                     | uploaded                               |
| regular file, several links                | uploaded: the descriptor still         |
|                                            | addresses real bytes, and this side     |
|                                            | only reads them                        |
| regular file that grew since it was opened  | uploaded to its length at open         |
| regular file that shrank since it was opened| uploaded short, and the declared length |
|                                            | makes the transport fail rather than    |
|                                            | pad; recorded, so the cycle raises      |
| zero bytes                                 | uploaded: an empty conversation is a    |
|                                            | conversation                            |
| above the reader's ceiling                 | uploaded, with a warning naming it:     |
|                                            | backed up, and the front will refuse to |
|                                            | restore it, so an operator hears it     |
|                                            | before a customer does                  |
| symlink                                    | recorded; the cycle raises              |
| directory, FIFO or socket                  | recorded; the cycle raises              |
| gone between listing and opening           | counted as gone; the cycle continues,   |
|                                            | because a deleted conversation is not   |
|                                            | a backup failure                        |
| unchanged since its last upload            | not re-uploaded                         |

## What the fingerprint is for, and what it is not

Change detection is a COST decision, not a correctness one. The fingerprint is the
inode, the length and the modification time as they were at open, and an object is
re-uploaded whenever it differs from the one last uploaded successfully. It lives in
memory, so a restarted sidecar re-uploads everything once: paying for a full cycle is
the right way to be wrong here, and persisting the state would put a second authority
on disk to keep in agreement with the bucket.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from ..common import Settings, keys
from ..common.config import MAX_OBJECT_BYTES
from .store import ObjectStore

log = logging.getLogger("smc.sidecar.backup")

__all__ = [
    "Fingerprint",
    "Snapshot",
    "CycleResult",
    "BackupIncomplete",
    "open_snapshot",
    "objects_to_back_up",
    "run_cycle",
]

#: Flags for opening a file to be uploaded.
#:
#: ``O_NOFOLLOW`` refuses a symlink at the final component, so a link planted where a
#: transcript belongs is reported instead of followed to whatever it points at.
#: ``O_NONBLOCK`` is what keeps the open from hanging: opening a FIFO for reading blocks
#: until a writer arrives, and an entry planted as a FIFO would otherwise stall the cycle
#: indefinitely rather than be refused.
_OPEN_FLAGS: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class Fingerprint:
    """What an object looked like when it was last uploaded successfully."""

    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Snapshot:
    """An open descriptor and the length its file had when it was opened.

    The pair IS the snapshot. Neither half is a snapshot alone: the descriptor without
    the length would upload however much had arrived by the time the transport got
    there, and the length without the descriptor would have to re-open the name, which
    is a second resolution of one path with a window in between.
    """

    fh: BinaryIO
    fingerprint: Fingerprint

    @property
    def size(self) -> int:
        return self.fingerprint.size

    def close(self) -> None:
        self.fh.close()


class RefusedEntry(RuntimeError):
    """This entry is not a file whose bytes can be uploaded."""


@dataclass
class CycleResult:
    """What one cycle did, per object, for the log and for the tests."""

    uploaded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    above_ceiling: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.refused

    def summary(self) -> str:
        return (
            f"{len(self.uploaded)} uploaded, {len(self.unchanged)} unchanged, "
            f"{len(self.gone)} gone, {len(self.refused)} refused"
        )


class BackupIncomplete(RuntimeError):
    """At least one object in the set could not be uploaded.

    Raised at the END of the cycle, with everything that could be uploaded already
    uploaded. The distinction matters: refusing the whole cycle on the first bad entry
    would cost every other conversation its backup, and dropping the bad entry with a
    log line would be the silent loss this design exists to prevent. So the cycle does
    all the work it can and then cannot be ignored.
    """

    def __init__(self, result: CycleResult) -> None:
        self.result = result
        detail = "; ".join(f"{name}: {why}" for name, why in result.refused)
        super().__init__(
            f"{len(result.refused)} object(s) in the backup set could not be uploaded "
            f"({detail}). Everything else in this cycle was uploaded."
        )


def open_snapshot(path: Path) -> Snapshot | None:
    """Open *path* for upload, or ``None`` when it is not there any more.

    ``None`` means the file was listed and then removed, which is a conversation the
    owner deleted rather than a backup failure. Every other way this can fail RAISES
    :class:`RefusedEntry`, because those are entries that are present and are not
    files whose bytes belong in the bucket.

    Shape is decided on the DESCRIPTOR, never on the name: a check by name followed by
    an open by name is two resolutions of one path with a window in between. Opening
    first with the link refused and then reading ``fstat`` off the descriptor means the
    entry judged is exactly the entry that will be uploaded.
    """
    try:
        fd = os.open(str(path), _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RefusedEntry(
                "it is a symlink, not a file; a link where a transcript belongs points "
                "at bytes this task does not own"
            ) from exc
        raise RefusedEntry(f"it could not be opened ({exc})") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:  # pragma: no cover - fstat on a fresh descriptor
        os.close(fd)
        raise RefusedEntry(f"its shape could not be read ({exc})") from exc
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise RefusedEntry(
            f"it is not a regular file (mode {st.st_mode:#o}); a directory, socket or "
            "FIFO holds no transcript bytes to upload"
        )
    return Snapshot(
        fh=os.fdopen(fd, "rb"),
        fingerprint=Fingerprint(inode=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns),
    )


def _live_transcripts(settings: Settings) -> list[Path]:
    """The ``.jsonl`` files directly under the sessions directory.

    A missing directory yields nothing: a task that has served no turn has no sessions
    directory yet, and that is a first boot rather than a fault.
    """
    try:
        entries = sorted(os.scandir(settings.sessions_dir), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    found: list[Path] = []
    for entry in entries:
        if not entry.name.endswith(keys.TRANSCRIPT_SUFFIX):
            continue
        # ``follow_symlinks=False`` so a link to a directory is not read as one file;
        # the shape is decided again on the descriptor, and this only decides what to
        # put in the list.
        if entry.is_dir(follow_symlinks=False):
            continue
        found.append(Path(entry.path))
    return found


def _archived_segments(settings: Settings) -> list[Path]:
    """Every file under the archive directory, at any depth.

    Walked rather than globbed at one level because rotation is free to nest, and a
    segment missed here is the older half of a conversation lost at the next task
    replacement. Directory symlinks are not followed: a link out of the data home would
    put files with no key of their own into the set.
    """
    root = settings.archive_dir
    found: list[Path] = []
    for parent, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            found.append(Path(parent) / name)
    return found


def objects_to_back_up(settings: Settings) -> list[tuple[str, Path]]:
    """Every ``(key, path)`` in the backup set, authority files first.

    Ordered so the authority files are uploaded before the transcripts they index. The
    restore side needs both, and a cycle that fails partway is better off having
    written the index for transcripts already in the bucket than transcripts no index
    names.
    """
    items: list[tuple[str, Path]] = [
        (keys.authority_key(settings, name), settings.config_dir / name)
        for name in keys.AUTHORITY_NAMES
    ]
    for path in _live_transcripts(settings) + _archived_segments(settings):
        items.append((keys.data_key(settings, path), path))
    return items


def run_cycle(
    settings: Settings,
    store: ObjectStore,
    *,
    state: dict[str, Fingerprint],
) -> CycleResult:
    """Upload everything in the set that has changed. Raise if anything was refused.

    *state* is read and written in place, so the caller keeps one map across cycles and
    an object unchanged since its last successful upload is not sent again.
    """
    result = CycleResult()
    for key, path in objects_to_back_up(settings):
        try:
            snapshot = open_snapshot(path)
        except RefusedEntry as exc:
            log.error("backup: refusing %s -- %s", path.name, exc)
            result.refused.append((path.name, str(exc)))
            continue
        if snapshot is None:
            log.info("backup: %s is gone; nothing to upload for it", path.name)
            result.gone.append(path.name)
            continue
        try:
            if state.get(key) == snapshot.fingerprint:
                result.unchanged.append(key)
                continue
            if snapshot.size > MAX_OBJECT_BYTES:
                # Uploaded anyway: skipping it is the data loss this design exists to
                # prevent. The warning is the point -- the front refuses to restore an
                # object this large, so the pair is honest but incomplete for this one
                # conversation, and an operator has to hear that from the writer rather
                # than from a customer's failed turn.
                log.warning(
                    "backup: %s is %d B, above the %d B ceiling the restore side will "
                    "read. It is uploaded, and a turn continuing this conversation on a "
                    "replaced task will be refused rather than served an empty history.",
                    path.name,
                    snapshot.size,
                    MAX_OBJECT_BYTES,
                )
                result.above_ceiling.append(key)
            try:
                store.put(key, snapshot.fh, snapshot.size)
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                log.error("backup: PUT failed for %s -- %s", path.name, exc)
                result.refused.append((path.name, f"the upload failed ({exc})"))
                continue
            state[key] = snapshot.fingerprint
            result.uploaded.append(key)
        finally:
            snapshot.close()
    log.info("backup: cycle complete -- %s", result.summary())
    if not result.complete:
        raise BackupIncomplete(result)
    return result
