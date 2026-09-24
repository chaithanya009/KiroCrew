"""The bucket, as the two narrowest operations the durability pair needs.

``put`` and ``get``, and nothing else. There is no ``list`` and no ``delete``, for
the same reason the front's reader has neither: a capability that exists is a
capability a later edit can reach for, and both of those change what the pair
means. A ``list`` on the restore side turns "bring back this task's own authority
files" into "enumerate the bucket", and a ``delete`` puts retention -- deciding
that a customer's history may go -- inside the process whose job is to keep it.

## Why ``put`` takes a descriptor and a size

Both halves of the consistency property live in that signature. The caller opens
the file ONCE and hands over the open descriptor, so the bytes uploaded are the
bytes that descriptor addresses, whatever later happens to the name. And the size
is the caller's, measured on the same descriptor at the same moment, so the upload
sends exactly the length that was true then.

Together they make a coherent copy without a lock and without a staged duplicate:

* The backend publishes a transcript with a temporary file and a rename, so it
  never writes into the bytes behind an open descriptor -- it swaps the directory
  entry to a different inode. A descriptor opened before the swap therefore keeps
  addressing a whole, finished version.
* A file that is instead appended to grows behind the descriptor. Sending exactly
  the recorded length uploads the prefix that existed at open time, which is a
  version that was really on disk. The next cycle sees a newer size and sends the
  longer one.

Neither case copies the file first, so there is nothing to bound: the upload spends
one descriptor and one fixed buffer, not a second copy of the data.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol, runtime_checkable

__all__ = [
    "ObjectStore",
    "ObjectAbsent",
    "ObjectTooLarge",
    "S3ObjectStore",
    "BoundedReader",
    "read_bounded",
    "GET_CHUNK_BYTES",
]

#: How much is read per chunk when a GET's ceiling is enforced. Small enough that the
#: overshoot before a refusal is bounded by this rather than by the object's size.
GET_CHUNK_BYTES: int = 1024 * 1024


class ObjectAbsent(Exception):
    """The key is not in the bucket.

    Distinct from a failure to read it. On a task's first boot the authority objects
    are genuinely not there yet, and that has to be told apart from a denial: reading
    a denial as absence is how a task boots with an empty slot table and then
    overwrites the real one.
    """


class ObjectTooLarge(RuntimeError):
    """The stored object is larger than the caller is willing to hold."""


@runtime_checkable
class ObjectStore(Protocol):
    """Put one object from a descriptor; get one object by key."""

    def put(self, key: str, body: BinaryIO, size: int) -> None: ...

    def get(self, key: str, *, limit: int) -> bytes: ...


class BoundedReader:
    """A read-only view of *fh* that stops after *limit* bytes.

    The bound belongs here rather than to the transport because it is a property of
    the SNAPSHOT: *limit* is the length the descriptor's file had when it was opened,
    so stopping there is what makes an appended file's upload a coherent prefix
    instead of a race with its writer. Leaving the transport to read to end-of-file
    would send however much had arrived by the time it got there, which is a length
    nothing observed.

    Only ``read`` is offered. A transport that seeks or tells would be reading the
    descriptor by a route this bound does not cover.
    """

    def __init__(self, fh: BinaryIO, limit: int) -> None:
        self._fh = fh
        self._remaining = max(0, limit)

    def read(self, amt: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if amt is None or amt < 0 else min(amt, self._remaining)
        chunk = self._fh.read(want)
        self._remaining -= len(chunk)
        return chunk


def read_bounded(body, key: str, *, limit: int) -> bytes:
    """Read at most *limit* bytes from a streaming body, refusing more.

    Streamed rather than read whole: a single read materialises whatever the object
    happens to be, so a check afterwards is a check on memory already spent. Reading
    one chunk PAST the limit is what makes the refusal decidable, since stopping
    exactly at the limit cannot tell a file of that size from a larger one.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = body.read(GET_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ObjectTooLarge(
                f"the stored object {key} exceeds {limit} bytes and was not read. It is "
                "held in memory to be validated and written, so an object this size is "
                "refused rather than loaded."
            )
        chunks.append(chunk)
    return b"".join(chunks)


#: Error codes S3 uses for "that key is not here". Anything else, ``AccessDenied``
#: included, is a FAILURE: absence and denial are different answers and only one of
#: them may be read as "there is nothing to restore".
_ABSENT_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})


def _error_code(exc: Exception) -> str:
    """The S3 error code of a botocore ``ClientError``, or ``""``.

    Read off the response dict rather than the exception type so a stub client in a
    test can produce a genuine absence without botocore installed.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict) and meta.get("HTTPStatusCode") == 404:
            return "404"
    return ""


class S3ObjectStore:
    """The real store: ``PutObject`` and ``GetObject``, one key at a time.

    boto3 is imported and the client built lazily, so this module is importable, and
    every test runnable, with no AWS present.
    """

    def __init__(self, bucket: str, *, client=None) -> None:
        self._bucket = bucket
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import boto3  # local import: keep the package importable without AWS

            self._client = boto3.client("s3")
        return self._client

    def put(self, key: str, body: BinaryIO, size: int) -> None:
        """Replace the object at *key* with *size* bytes read from *body*.

        ``ContentLength`` is passed explicitly and the body is wrapped, so the length
        declared to S3 and the length actually sent are the same number and both are
        the one the caller measured. Without the wrapper a file that grew during the
        upload would send more bytes than the header promised; without the header the
        transport would buffer to find the length, which is the staged copy this
        design exists to avoid.
        """
        self._ensure_client().put_object(
            Bucket=self._bucket,
            Key=key,
            Body=BoundedReader(body, size),
            ContentLength=size,
        )

    def get(self, key: str, *, limit: int) -> bytes:
        """Fetch one object, bounded twice, or raise :class:`ObjectAbsent`.

        ``ContentLength`` is a CLAIM by the source, so it is checked and then not
        trusted: an object that declares itself too large is refused before a byte is
        read, and the streaming read enforces the same ceiling on what actually
        arrives. A header is not a bound.
        """
        try:
            resp = self._ensure_client().get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            if _error_code(exc) in _ABSENT_CODES:
                raise ObjectAbsent(key) from exc
            raise
        declared = resp.get("ContentLength")
        if isinstance(declared, int) and declared > limit:
            raise ObjectTooLarge(
                f"the stored object {key} declares {declared} bytes, above the "
                f"{limit}-byte ceiling, and was not read."
            )
        return read_bounded(resp["Body"], key, limit=limit)
