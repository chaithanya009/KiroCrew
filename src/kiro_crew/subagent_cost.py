"""Append-only learned per-agent cost store for dynamic sub-agent sizing.

One JSONL line per completed run, written via atomic ``O_APPEND`` (race-free,
no lock). The cap is computed at startup from ``read_learned_cost`` =
``max(per-agent p90)`` over the last N samples; the log is FIFO-trimmed to the
last N per agent both at startup and periodically.

See ``dynamic-subagent-sizing.md`` §4.2 (storage) / §4.3 (aggregation).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from kiro_crew.config.paths import config_dir
from kiro_crew.jsonl_util import RECORD_CAP, UnreadableRecord, strict_records

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "kirocrew"  # key for unnamed/default-agent runs (§4.3 keying)
_DEFAULT_WINDOW = 50  # samples retained + considered per agent
_DEFAULT_MIN_SAMPLES = 3  # before trusting learned over the configured fallback
_DEFAULT_PERCENTILE = 0.90
# Samples older than this are left out of every percentile. A price learned
# under a workload that is gone (a heavy MCP roster removed, a backend switched)
# must be able to expire without an operator deleting the log, because deferred
# starts record no samples that could lower it. The horizon is long on purpose:
# a host idle for less than this keeps its learned figure, so its next fan-out
# is not priced at the first-boot fallback again; one idle longer re-learns
# from its next three runs.
_SAMPLE_MAX_AGE_SECS = 30 * 24 * 3600
# Bounds on what the agent-writable log can put into a manager-lifetime map:
# a bucket key longer than an agent name can be is dropped, and only the
# heaviest _MAX_BUCKETS buckets are kept.
_BUCKET_KEY_CAP = 128
_MAX_BUCKETS = 64

# Longest single RECORD the reader will materialise. This log is agent-writable,
# and its read feeds compact_cost_log's rewrite of the same file, so an over-cap
# record aborts the read rather than being skipped. Named here so a test can move
# the dial; a real sample is a tiny object (agent, mem_gb, cpu_cores, ts), so the
# shared cap has enormous headroom over anything legitimate.
_RECORD_CAP = RECORD_CAP


def _cost_log_path() -> Path:
    return config_dir() / "subagents" / "cost_samples.jsonl"


def append_cost_sample(
    agent: str, mem_gb: float, cpu_cores: float, *, shared: bool = False
) -> None:
    """Append one ``{agent, mem_gb, cpu_cores, ts[, shared]}`` line (atomic O_APPEND).

    ``shared`` marks a run that executed as a session inside a shared runtime:
    its figures are that runtime's readings divided by the sessions sharing it,
    a per-session share rather than a process, so a reader pricing a start that
    may run as its OWN process must be able to leave them out
    (:func:`read_learned_costs` ``dedicated_only``). Written only when true, so
    the record shape of a dedicated run is unchanged; a record without the field
    reads as dedicated.
    """
    if mem_gb <= 0 and cpu_cores <= 0:
        return  # nothing was measured
    rec: dict[str, object] = {
        "agent": agent or _DEFAULT_AGENT,  # normalize empty → default agent
        "mem_gb": round(float(mem_gb), 4),
        "cpu_cores": round(float(cpu_cores), 4),
        "ts": int(time.time()),
    }
    if shared:
        rec["shared"] = True
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    path = _cost_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND makes each small write atomic across concurrent agents.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        logger.debug("Failed to append cost sample", exc_info=True)


def _read_samples() -> list[dict]:
    """Read all valid JSONL records; skip corrupt lines; [] if missing.

    The degrading view of :func:`_read_samples_checked`, for the read-only
    percentile consumer. :func:`compact_cost_log` must NOT use this one -- it
    rewrites the log from what it parsed, so it needs the completeness flag.
    """
    rows, _complete = _read_samples_checked()
    return rows


def _read_samples_checked() -> tuple[list[dict], bool]:
    """Return the log's records, and whether they are ALL of them.

    The second element is False when a record exceeded :data:`_RECORD_CAP` and
    was therefore refused. That cannot be treated like a corrupt line: this log
    is agent-writable, so one crafted newline-free line would otherwise be
    materialised whole, and
    :func:`kiro_crew.jsonl_util.strict_records` stops the read instead of
    skipping it. The rows read before that point are kept -- they are real
    samples the percentile consumer can still use -- but the flag has to reach
    :func:`compact_cost_log`, which REPLACES the file with what it parsed and
    would otherwise delete the refused record permanently.
    """
    out: list[dict] = []
    complete = True
    try:
        path = _cost_log_path()
        try:
            with open(path, "rb") as fh:
                for raw in strict_records(fh, path, cap=_RECORD_CAP):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue  # skip corrupt line, keep going
                    if isinstance(rec, dict):
                        out.append(rec)
        except UnreadableRecord:
            complete = False
            # "unreadable", not "over-cap": UnreadableRecord also covers invalid
            # UTF-8, and this line is what an operator sees, so naming only the
            # cap would point them at a size problem that may not exist.
            logger.warning("cost log has a record that could not be read; read as incomplete")
    except (FileNotFoundError, OSError):
        return [], True
    return out, complete


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (nearest-rank for tiny lists)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = pct * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def _group_by_agent(
    samples: list[dict], key: str, *, dedicated_only: bool = False, now: float | None = None
) -> dict[str, list[float]]:
    horizon = (time.time() if now is None else now) - _SAMPLE_MAX_AGE_SECS
    by_agent: dict[str, list[float]] = {}
    for rec in samples:
        if dedicated_only and rec.get("shared") is True:
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts < horizon:
            continue  # expired: learned under a workload this host may not run today
        agent = str(rec.get("agent") or _DEFAULT_AGENT)
        if len(agent) > _BUCKET_KEY_CAP:
            continue  # not an agent name; the log is agent-writable
        v = rec.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            by_agent.setdefault(agent, []).append(float(v))
    return by_agent


def read_learned_costs(
    key: str,
    *,
    window: int = _DEFAULT_WINDOW,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    percentile: float = _DEFAULT_PERCENTILE,
    dedicated_only: bool = False,
) -> dict[str, float]:
    """Per-agent p90 of the last ``window`` samples for *key*, agents with fewer
    than ``min_samples`` omitted. Empty when nothing qualifies.

    ``dedicated_only`` leaves out samples recorded from session-shared runs (a
    per-session share of one runtime, see :func:`append_cost_sample`): the
    figure that prices a start which may run as its own process must come from
    runs that did. Session sharing is the default on the kiro backend and a
    model-pinned spawn is forced dedicated, so a bucket of divided shares
    pricing a dedicated fan-out is an ordinary case, not a corner.
    """
    by_agent = _group_by_agent(_read_samples(), key, dedicated_only=dedicated_only)
    out: dict[str, float] = {}
    for agent, vals in by_agent.items():
        recent = vals[-window:]
        if len(recent) < min_samples:
            continue
        out[agent] = _percentile(recent, percentile)
    return cap_buckets(out)


def cap_buckets(costs: Mapping[str, float]) -> dict[str, float]:
    """The heaviest ``_MAX_BUCKETS`` of *costs*: the bound a held map stays under."""
    if len(costs) <= _MAX_BUCKETS:
        return dict(costs)
    heaviest = sorted(costs.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_BUCKETS]
    return dict(heaviest)


def read_learned_cost(
    key: str,
    *,
    window: int = _DEFAULT_WINDOW,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    percentile: float = _DEFAULT_PERCENTILE,
) -> float | None:
    """Return ``max(per-agent p90)`` for *key* (``mem_gb``/``cpu_cores``), or None.

    Per agent, take the p90 of the last ``window`` samples (only if it has at
    least ``min_samples``), then the max across agents. Returns None when no
    agent qualifies — the caller falls back to the configured first-boot cost.
    A percentile is outlier-robust, so a single pathological run can't dominate.
    """
    costs = read_learned_costs(key, window=window, min_samples=min_samples, percentile=percentile)
    return max(costs.values()) if costs else None


def learned_cost_for(costs: Mapping[str, float], agent: str) -> float | None:
    """The learned figure for one spawn: *agent*'s own p90, or None.

    The cap is sized from the heaviest agent because it bounds the whole host;
    a single start is priced at what THAT agent's own dedicated runs have cost
    here, so one build-heavy agent's history does not hold every other spawn to
    its price. A bucket with no qualifying dedicated history answers None and
    the caller prices from the configured cost plus whatever live dedicated
    peaks it can see -- NOT from the heaviest known bucket: on a backend where
    session sharing is the default, a share-eligible agent records only shared
    samples, so its own bucket never forms, and a heaviest-known fallback would
    price every one of its spawns at an unrelated agent's figure for good.
    """
    if not costs:
        return None
    return costs.get(agent or _DEFAULT_AGENT)


def cost_log_identity() -> tuple[object, ...] | None:
    """The log's ``(device, inode, size)``, ``None`` when absent, ``("unknown",)`` when
    present but not inspectable.

    Lets a reader tell a log that was REPLACED -- deleted and re-created by the
    next sample within one sweep (``append_cost_sample`` re-creates the path at
    once), or rewritten by compaction -- apart from one that merely grew: a new
    inode, or a size that shrank, means the records it held before are gone and
    a figure learned from them must not be carried over. Only
    ``FileNotFoundError`` means absent; any other stat failure is reported as
    present-but-unknown, the conservative reading for a caller deciding whether
    to drop a figure it already holds.
    """
    try:
        st = os.stat(_cost_log_path())
    except FileNotFoundError:
        return None
    except OSError:
        return ("unknown",)
    return (st.st_dev, st.st_ino, st.st_size)


def compact_cost_log(window: int = _DEFAULT_WINDOW) -> None:
    """FIFO-trim the log to the last ``window`` samples per (agent, shared) (atomic).

    Safe to call anytime; a sample appended in the brief read→replace window
    may be dropped, which is harmless for an approximate p90. No-op when the
    log is already within bounds.

    Fails closed on an incomplete read. This function REPLACES the log with the
    records it parsed, so trimming from a partial read would permanently delete
    the over-cap record the reader refused -- the same reason
    ``session_storage``'s manifest reader aborts instead of skipping. Leaving
    the log untrimmed costs bounded disk; compacting would cost data.
    """
    samples, complete = _read_samples_checked()
    if not complete:
        logger.warning("cost log unreadable in full; skipping compaction to avoid data loss")
        return
    if not samples:
        return
    # One window per (agent, shared): the dedicated_only reader needs an agent's
    # dedicated samples to survive however many session-shared runs the same
    # agent records, so shared samples may never evict them from the window.
    by_agent: dict[tuple[str, bool], list[dict]] = {}
    for rec in samples:
        bucket = (str(rec.get("agent") or _DEFAULT_AGENT), rec.get("shared") is True)
        by_agent.setdefault(bucket, []).append(rec)
    kept: list[dict] = []
    for vals in by_agent.values():
        kept.extend(vals[-window:])
    if len(kept) >= len(samples):
        return  # nothing to trim
    kept.sort(key=lambda r: r.get("ts", 0))  # preserve chronological order
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
    path = _cost_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        logger.debug("Failed to compact cost log", exc_info=True)
