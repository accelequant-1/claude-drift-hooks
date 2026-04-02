"""
Drift Ledger — SQLite backend for instruction drift tracking.

Stores claim hashes (NOT raw text) with redacted display summaries.
DB permissions: 0600. Journal mode: DELETE (no WAL files on disk).
Auto-truncates claims older than 7 days on context-manager exit.
"""

import hashlib
import logging
import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "drift.db"
ERROR_LOG = Path(__file__).parent / "drift_errors.log"
MAX_AGE_DAYS = 7

# Module-level logger that writes to drift_errors.log
logging.basicConfig()
_log = logging.getLogger("drift_db")
_log.setLevel(logging.ERROR)
_fh = logging.FileHandler(str(ERROR_LOG))
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_fh)
_log.propagate = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    turn INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    claim_hash TEXT NOT NULL,
    claim_display TEXT NOT NULL,
    has_evidence INTEGER DEFAULT 0,
    pattern TEXT,
    commit_sha TEXT,
    verified INTEGER DEFAULT 0,
    verification_cmd TEXT,
    detection_tier INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    turn INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    total_claims INTEGER,
    evidenced_claims INTEGER,
    drift_score REAL
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    drift_score REAL,
    override INTEGER DEFAULT 0,
    total_claims INTEGER,
    unverified_claims INTEGER
);

CREATE TABLE IF NOT EXISTS compaction_events (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    trigger TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    citation_type TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    byte_offset INTEGER,
    snippet TEXT,
    verification_cmd TEXT,
    cmd_output_hash TEXT,
    FOREIGN KEY (claim_id) REFERENCES claims(id)
);

CREATE INDEX IF NOT EXISTS idx_claims_commit ON claims(commit_sha);
CREATE INDEX IF NOT EXISTS idx_claims_turn ON claims(turn);
CREATE INDEX IF NOT EXISTS idx_claims_timestamp ON claims(timestamp);
CREATE INDEX IF NOT EXISTS idx_verifications_claim ON verifications(claim_id);
CREATE INDEX IF NOT EXISTS idx_claims_unverified ON claims(has_evidence, commit_sha);
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hash_claim(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _redact_display(text: str, pattern: str = None) -> str:
    """Create a redacted display: first 5 words + [...type] + last word."""
    words = text.split()
    if len(words) <= 6:
        display = text[:80]
    else:
        display = " ".join(words[:5]) + " ... " + words[-1]
    if pattern:
        display += f" [{pattern}]"
    return display[:120]


def _suggest_verify_cmd(text: str) -> str:
    """Suggest a bash command to verify a claim."""
    suggestions = [
        (r"(\d+)\s*lines?\b.*`([^`]+)`", "wc -l {0}"),
        (r"`([^`]+\.(?:py|sh|cfg|cpp))`.*(\d+)\s*lines?", "wc -l {0}"),
        (r"(\d+[.,]?\d*)\s*[KMG]?\s*params?", "python3 -c \"import torch; print(sum(p.numel() for p in torch.load('model.ckpt',map_location='cpu')['model'].values()))\""),
        (r"(\d+\.?\d*)\s*%.*(?:win|WR)", "find basedir/eval -name '*.json' -exec cat {} \\; | python3 -c \"import sys,json; [print(json.loads(l)) for l in sys.stdin]\""),
        (r"`([^`]+\.(?:py|sh|cfg|cpp))`", "ls -lh {0} && head -3 {0}"),
        (r"(\d+)\s*epochs?", "tail -1 **/epoch_metrics.jsonl 2>/dev/null"),
        (r"(\d+)\s*(?:GB|MB|KB)", "du -sh {file}"),
        (r"(\d+)\s*files?", "find . -type f | wc -l"),
    ]
    for regex, cmd_template in suggestions:
        m = re.search(regex, text, re.I)
        if m:
            try:
                return cmd_template.format(*m.groups())
            except (IndexError, KeyError):
                return cmd_template
    return ""


def _connect_with_retry() -> sqlite3.Connection:
    """Open sqlite3.connect() with 3 attempts and exponential backoff on lock."""
    delays = [0.1, 0.3, 1.0]
    last_exc = None
    for attempt, delay in enumerate(delays):
        try:
            is_new = not DB_PATH.exists()
            conn = sqlite3.connect(str(DB_PATH), timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            if is_new:
                os.chmod(str(DB_PATH), stat.S_IRUSR | stat.S_IWUSR)  # 0600
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA foreign_keys=ON")
            # executescript() manages its own transactions internally
            # (issues implicit COMMIT). Do NOT wrap it in BEGIN/COMMIT.
            conn.executescript(SCHEMA)
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            _log.error("drift_db: connect attempt %d failed: %s", attempt + 1, exc)
            if attempt < len(delays) - 1:
                time.sleep(delay)
    raise last_exc


def _safe_execute(conn, sql: str, params=(), default=None):
    """Execute SQL and return cursor; on any error log and return default."""
    try:
        return conn.execute(sql, params)
    except Exception as exc:
        _log.error("drift_db _safe_execute error [%s]: %s", sql[:80], exc)
        return default


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class DriftDB:
    """Context manager for the drift SQLite database.

    Usage::

        with DriftDB() as conn:
            insert_claims(conn, turn, claims)
    """

    def __enter__(self) -> sqlite3.Connection:
        try:
            self._conn = _connect_with_retry()
        except Exception as exc:
            _log.error("drift_db: failed to open DB: %s", exc)
            # Create an in-memory fallback so callers always get a conn
            self._conn = sqlite3.connect(":memory:", isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            # Auto-truncate old data on every exit (even after failures)
            cutoff = (datetime.utcnow() - timedelta(days=MAX_AGE_DAYS)).isoformat()
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("DELETE FROM claims WHERE timestamp < ?", (cutoff,))
            self._conn.execute("DELETE FROM turns WHERE timestamp < ?", (cutoff,))
            self._conn.execute("COMMIT")
        except Exception as exc:
            _log.error("drift_db: truncation failed: %s", exc)
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
        finally:
            try:
                self._conn.close()
            except Exception:
                pass
        return False  # Do not suppress exceptions


def open_db() -> sqlite3.Connection:
    """Open or create the drift DB with security settings.

    Backward-compatible function. The returned connection is the raw
    sqlite3.Connection; callers are responsible for closing it.
    Prefer ``with DriftDB() as conn:`` for automatic cleanup.
    """
    try:
        return _connect_with_retry()
    except Exception as exc:
        _log.error("drift_db: open_db failed: %s", exc)
        # Fallback to in-memory so callers always get a working connection
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn


# ---------------------------------------------------------------------------
# Public write functions — all use explicit BEGIN IMMEDIATE transactions
# ---------------------------------------------------------------------------

def insert_claims(conn, turn: int, claims: list[dict]):
    """Insert claim records from analyze_response output.

    Each claim dict: {"text": str, "has_evidence": bool, "pattern": str|None, "tier": int}
    """
    try:
        ts = datetime.utcnow().isoformat()
        rows = []
        for c in claims:
            rows.append((
                turn, ts,
                _hash_claim(c["text"]),
                _redact_display(c["text"], c.get("pattern")),
                1 if c["has_evidence"] else 0,
                c.get("pattern"),
                _suggest_verify_cmd(c["text"]) if not c["has_evidence"] else "",
                c.get("tier", 1),
            ))
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO claims (turn, timestamp, claim_hash, claim_display, has_evidence, pattern, verification_cmd, detection_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception as exc:
        _log.error("drift_db: insert_claims failed: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


def insert_turn(conn, turn: int, total: int, evidenced: int, drift: float):
    """Insert a turn summary record."""
    try:
        ts = datetime.utcnow().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO turns (turn, timestamp, total_claims, evidenced_claims, drift_score) VALUES (?, ?, ?, ?, ?)",
            (turn, ts, total, evidenced, drift),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        _log.error("drift_db: insert_turn failed: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


def mark_committed(conn, sha: str, drift_score: float, override: bool = False):
    """Mark all uncommitted claims as belonging to this commit.

    All SELECTs and the UPDATE+INSERT run inside a single BEGIN IMMEDIATE
    transaction to prevent races between the count and the update.
    """
    try:
        ts = datetime.utcnow().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        unverified = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE commit_sha IS NULL AND has_evidence = 0"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE commit_sha IS NULL"
        ).fetchone()[0]
        conn.execute(
            "UPDATE claims SET commit_sha = ? WHERE commit_sha IS NULL",
            (sha,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO commits (sha, timestamp, drift_score, override, total_claims, unverified_claims) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sha, ts, drift_score, 1 if override else 0, total, unverified),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        _log.error("drift_db: mark_committed failed: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Public read functions — safe_execute wrappers
# ---------------------------------------------------------------------------

def get_uncommitted_unverified(conn) -> list[dict]:
    """Get claims that have no evidence AND haven't been committed yet."""
    try:
        rows = conn.execute(
            "SELECT id, claim_display, pattern, verification_cmd FROM claims "
            "WHERE has_evidence = 0 AND commit_sha IS NULL "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        _log.error("drift_db: get_uncommitted_unverified failed: %s", exc)
        return []


def get_session_drift(conn) -> dict:
    """Get aggregate drift for the current session."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) as total, SUM(has_evidence) as evidenced, "
            "SUM(CASE WHEN pattern='A' THEN 1 ELSE 0 END) as pa, "
            "SUM(CASE WHEN pattern='B' THEN 1 ELSE 0 END) as pb, "
            "SUM(CASE WHEN pattern='C' THEN 1 ELSE 0 END) as pc, "
            "SUM(CASE WHEN pattern='D' THEN 1 ELSE 0 END) as pd "
            "FROM claims"
        ).fetchone()
        # last_turn from turns table (not claims) so claim-free turns are counted
        turn_row = conn.execute("SELECT MAX(turn) as last_turn FROM turns").fetchone()
        total = row["total"] or 0
        evidenced = row["evidenced"] or 0
        return {
            "total": total,
            "evidenced": evidenced,
            "unverified": total - evidenced,
            "drift": (total - evidenced) / total if total > 0 else 0.0,
            "pattern_a": row["pa"] or 0,
            "pattern_b": row["pb"] or 0,
            "pattern_c": row["pc"] or 0,
            "pattern_d": row["pd"] or 0,
            "last_turn": (turn_row["last_turn"] or 0) if turn_row else 0,
        }
    except Exception as exc:
        _log.error("drift_db: get_session_drift failed: %s", exc)
        return {"total": 0, "evidenced": 0, "unverified": 0, "drift": 0.0,
                "pattern_a": 0, "pattern_b": 0, "pattern_c": 0, "pattern_d": 0, "last_turn": 0}


def record_compaction(conn, trigger: str = "unknown"):
    """Record a context compaction event."""
    try:
        ts = datetime.utcnow().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO compaction_events (timestamp, trigger) VALUES (?, ?)",
            (ts, trigger),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        _log.error("drift_db: record_compaction failed: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def get_recent_compaction(conn, minutes: int = 5) -> bool:
    """Check if a compaction event happened in the last N minutes."""
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM compaction_events WHERE timestamp > ?",
            (cutoff,),
        ).fetchone()
        return (row[0] or 0) > 0
    except Exception:
        return False


def record_verification(conn, claim_id: int, citation: dict):
    """Record a verification for a claim and mark it as verified.

    citation dict: {
        "type": "file"|"command"|"grep"|"git_log",
        "file_path": str|None, "line_number": int|None,
        "byte_offset": int|None, "snippet": str|None,
        "verification_cmd": str|None, "cmd_output_hash": str|None,
    }
    """
    try:
        ts = datetime.utcnow().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO verifications (claim_id, timestamp, citation_type, "
            "file_path, line_number, byte_offset, snippet, verification_cmd, cmd_output_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (claim_id, ts, citation.get("type", "unknown"),
             citation.get("file_path"), citation.get("line_number"),
             citation.get("byte_offset"), (citation.get("snippet") or "")[:120],
             citation.get("verification_cmd"), citation.get("cmd_output_hash")),
        )
        conn.execute("UPDATE claims SET verified = 1 WHERE id = ?", (claim_id,))
        conn.execute("COMMIT")

        # Append to ledger file (human-readable audit trail)
        _append_ledger(claim_id, ts, citation)
    except Exception as exc:
        _log.error("drift_db: record_verification failed: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def _append_ledger(claim_id: int, timestamp: str, citation: dict):
    """Append a verification event to the JSONL ledger file."""
    import json as _json
    ledger_path = Path(__file__).parent / "verification_ledger.jsonl"
    try:
        entry = {
            "timestamp": timestamp,
            "claim_id": claim_id,
            "citation": {
                "type": citation.get("type"),
                "file": citation.get("file_path"),
                "line": citation.get("line_number"),
                "byte": citation.get("byte_offset"),
                "snippet": (citation.get("snippet") or "")[:80],
            },
            "status": "verified",
        }
        with open(ledger_path, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception as exc:
        _log.error("drift_db: ledger write failed: %s", exc)


def get_unverified_claims(conn, limit: int = 10) -> list[dict]:
    """Get claims that have no evidence AND are not yet verified."""
    try:
        rows = conn.execute(
            "SELECT id, turn, claim_display, claim_hash, pattern, verification_cmd "
            "FROM claims WHERE has_evidence = 0 AND verified = 0 "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        _log.error("drift_db: get_unverified_claims failed: %s", exc)
        return []


def get_verification_stats(conn) -> dict:
    """Get verification statistics for the session."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) as verified, "
            "SUM(CASE WHEN has_evidence = 1 THEN 1 ELSE 0 END) as auto_evidenced, "
            "SUM(CASE WHEN has_evidence = 0 AND verified = 0 THEN 1 ELSE 0 END) as pending "
            "FROM claims"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "verified": row["verified"] or 0,
            "auto_evidenced": row["auto_evidenced"] or 0,
            "pending": row["pending"] or 0,
        }
    except Exception:
        return {"total": 0, "verified": 0, "auto_evidenced": 0, "pending": 0}


def get_recent_verifications(conn, limit: int = 10) -> list[dict]:
    """Get recent verification events with citation details."""
    try:
        rows = conn.execute(
            "SELECT v.timestamp, v.citation_type, v.file_path, v.line_number, "
            "v.byte_offset, v.snippet, c.claim_display, c.turn "
            "FROM verifications v JOIN claims c ON v.claim_id = c.id "
            "ORDER BY v.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_unverified_commits(conn) -> list[dict]:
    """Get commits that contain unverified claims."""
    try:
        rows = conn.execute(
            "SELECT sha, drift_score, override, total_claims, unverified_claims "
            "FROM commits WHERE unverified_claims > 0 ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        _log.error("drift_db: get_unverified_commits failed: %s", exc)
        return []
