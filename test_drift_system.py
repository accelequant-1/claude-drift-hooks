#!/usr/bin/env python3
"""
End-to-end integration test for the drift hook system.
Tests all components: DriftDB, analyze_response, drift-metric.py, drift_analysis.py,
git-commit-gate.sh, git-push-gate.sh.

Exit 0 if all tests pass, exit 1 if any fail.
"""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

# ── Setup path so we can import hook modules directly ──
HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

import drift_db
from drift_db import DriftDB, insert_claims, insert_turn, get_session_drift, _safe_execute

# drift-metric.py has a hyphen in its name — load it with importlib
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("drift_metric", HOOKS_DIR / "drift-metric.py")
_drift_metric_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_drift_metric_mod)
analyze_response = _drift_metric_mod.analyze_response

DB_PATH = HOOKS_DIR / "drift.db"
DRIFT_METRIC_PY = HOOKS_DIR / "drift-metric.py"
DRIFT_ANALYSIS_PY = HOOKS_DIR / "drift_analysis.py"
GIT_COMMIT_GATE = HOOKS_DIR / "git-commit-gate.sh"
GIT_PUSH_GATE = HOOKS_DIR / "git-push-gate.sh"


# ── Test harness ──

_results = []


def _pass(name):
    print(f"PASS  {name}")
    _results.append((name, True, ""))


def _fail(name, reason=""):
    print(f"FAIL  {name}" + (f": {reason}" if reason else ""))
    _results.append((name, False, reason))


def _remove_db():
    """Remove drift.db if it exists."""
    if DB_PATH.exists():
        DB_PATH.unlink()


# ── Test 1: Fresh DB — DriftDB context manager creates drift.db, 0600 perms ──

def test_fresh_db():
    name = "Fresh DB: DriftDB creates drift.db with 0600 permissions"
    _remove_db()
    try:
        with DriftDB() as conn:
            exists = DB_PATH.exists()
            mode = stat.S_IMODE(os.stat(DB_PATH).st_mode)
        if not exists:
            _fail(name, "drift.db was not created")
            return
        if mode != 0o600:
            _fail(name, f"permissions are {oct(mode)}, expected 0o600")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 2: Insert + persist ──

def test_insert_and_persist():
    name = "Insert + persist: data survives across connections"
    _remove_db()
    try:
        claims = [
            {"text": "There are 100 lines in main.py", "has_evidence": False, "pattern": "B", "tier": 1},
            {"text": "wc -l confirms 200 rows (verified)", "has_evidence": True, "pattern": None, "tier": 1},
        ]
        # Write in one connection
        with DriftDB() as conn:
            insert_claims(conn, turn=1, claims=claims)
            insert_turn(conn, turn=1, total=2, evidenced=1, drift=0.5)

        # Read in a fresh connection
        with DriftDB() as conn:
            stats = get_session_drift(conn)

        if stats["total"] != 2:
            _fail(name, f"expected total=2, got {stats['total']}")
            return
        if stats["evidenced"] != 1:
            _fail(name, f"expected evidenced=1, got {stats['evidenced']}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 3: Tier 1 detection ──

def test_tier1_detection():
    name = "Tier 1 detection: evidenced claim detected"
    try:
        response = "The file has 620 lines (verified from wc -l)."
        claims = analyze_response(response)
        tier1 = [c for c in claims if c["tier"] == 1]
        if not tier1:
            _fail(name, "no Tier 1 claims detected")
            return
        evidenced = [c for c in tier1 if c["has_evidence"]]
        if not evidenced:
            _fail(name, f"no evidenced claim found; claims={tier1}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 4: Tier 2 detection ──

def test_tier2_detection():
    name = "Tier 2 detection: 'The model outperforms baseline' detected as Tier 2"
    try:
        response = "The model outperforms baseline by a notable margin."
        claims = analyze_response(response)
        tier2 = [c for c in claims if c["tier"] == 2]
        if not tier2:
            _fail(name, f"no Tier 2 claims detected; all claims={claims}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 5: Pattern A ──

def test_pattern_a():
    name = "Pattern A: 'docstring says 3M rows' classified as pattern A"
    try:
        # This line has a number + units (Tier 1 claim) AND "says" (pattern A trigger)
        response = "The docstring says 3M rows are processed in main.py."
        claims = analyze_response(response)
        pattern_a = [c for c in claims if c.get("pattern") == "A"]
        if not pattern_a:
            _fail(name, f"no Pattern A claims; all claims={claims}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 6: Pattern B ──

def test_pattern_b():
    name = "Pattern B: 'approximately 55 hours' classified as pattern B"
    try:
        response = "Training takes approximately 55 hours total."
        claims = analyze_response(response)
        pattern_b = [c for c in claims if c.get("pattern") == "B"]
        if not pattern_b:
            _fail(name, f"no Pattern B claims; all claims={claims}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 7: Pattern C ──

def test_pattern_c():
    name = "Pattern C/D: prior-reference language classified as C or D"
    try:
        # "as we established" triggers both Pattern C and Pattern D.
        # Pattern D (post-compaction stale) overrides C when both match,
        # since D is higher severity. Either is correct.
        response = "As we established earlier, the model uses 81 qubits."
        claims = analyze_response(response)
        pattern_cd = [c for c in claims if c.get("pattern") in ("C", "D")]
        if not pattern_cd:
            _fail(name, f"no Pattern C or D claims; all claims={claims}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 8: Drift metric output via subprocess ──

def test_drift_metric_output():
    name = "Drift metric output: valid JSON with systemMessage"
    _remove_db()
    try:
        response_text = (
            "The model has 192 channels. "
            "It uses approximately 15 blocks. "
            "As we established earlier, this runs for 50 epochs. "
            "wc -l confirms 620 lines (verified from wc -l)."
        )
        hook_input = json.dumps({"last_assistant_message": response_text})
        result = subprocess.run(
            [sys.executable, str(DRIFT_METRIC_PY)],
            input=hook_input,
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        if not stdout:
            _fail(name, f"empty stdout; stderr={result.stderr[:200]}")
            return
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return
        if "systemMessage" not in data:
            _fail(name, f"no 'systemMessage' key; keys={list(data.keys())}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 9: Drift analysis panel via subprocess ──

def test_drift_analysis_panel():
    name = "Drift analysis panel: prints all 6 sections"
    # Ensure DB exists with some data first (reuse state from test 8)
    try:
        if not DB_PATH.exists():
            # Create minimal DB with data so drift_analysis.py has something to show
            with DriftDB() as conn:
                insert_claims(conn, 1, [
                    {"text": "Model uses 192 channels", "has_evidence": False, "pattern": None, "tier": 1},
                ])
                insert_turn(conn, 1, 1, 0, 1.0)

        result = subprocess.run(
            [sys.executable, str(DRIFT_ANALYSIS_PY)],
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        output = result.stdout
        required_sections = [
            "INSTRUCTION DRIFT ANALYSIS",
            "VELOCITY & STREAK",
            "TOP UNCHECKED CLAIMS",
            "PATTERN COMPOSITION",
            "PHASE TRANSITIONS",
            "STRATEGIC NEXT STEPS",
        ]
        missing = [s for s in required_sections if s not in output]
        if missing:
            _fail(name, f"missing sections: {missing}; output snippet={output[:300]}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 10: Error recovery — missing DB ──

def test_missing_db_recovery():
    name = "Error recovery — missing DB: drift-metric.py outputs valid JSON"
    _remove_db()
    try:
        hook_input = json.dumps({"last_assistant_message": "The model has 100 params."})
        result = subprocess.run(
            [sys.executable, str(DRIFT_METRIC_PY)],
            input=hook_input,
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        if not stdout:
            _fail(name, f"empty stdout; stderr={result.stderr[:200]}")
            return
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return
        # Accept either systemMessage (continue) or decision (block) as valid
        if "systemMessage" not in data and "decision" not in data:
            _fail(name, f"no 'systemMessage' or 'decision' key; keys={list(data.keys())}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))
    finally:
        _remove_db()


# ── Test 11: Error recovery — corrupt DB ──

def test_corrupt_db_recovery():
    name = "Error recovery — corrupt DB: drift-metric.py outputs valid JSON"
    _remove_db()
    try:
        # Write garbage to drift.db
        DB_PATH.write_bytes(b"THIS IS NOT A SQLITE DATABASE \x00\xff\x00\xab\xcd")
        hook_input = json.dumps({"last_assistant_message": "There are 50 epochs in training."})
        result = subprocess.run(
            [sys.executable, str(DRIFT_METRIC_PY)],
            input=hook_input,
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        if not stdout:
            _fail(name, f"empty stdout; stderr={result.stderr[:200]}")
            return
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return
        if "systemMessage" not in data and "decision" not in data:
            _fail(name, f"no 'systemMessage' or 'decision' key; keys={list(data.keys())}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))
    finally:
        _remove_db()


# ── Test 12: _safe_execute with bad SQL ──

def test_safe_execute():
    name = "Safe execute: _safe_execute with bad SQL returns default, doesn't crash"
    _remove_db()
    try:
        with DriftDB() as conn:
            result = _safe_execute(conn, "SELECT * FROM nonexistent_table_xyz", default=None)
        if result is not None:
            _fail(name, f"expected None default, got {result}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))
    finally:
        _remove_db()


# ── Test 13: Dynamic funnel — high drift triggers DRIFT CRITICAL warning ──

def test_dynamic_funnel_high_drift():
    name = "Dynamic funnel: high drift (>60%) triggers DRIFT CRITICAL or funnel warning"
    _remove_db()
    try:
        # Insert enough unverified claims to get drift > 60%
        claims_batch = [
            {"text": f"Model layer {i} uses {(i+1)*10} channels in trunk.py", "has_evidence": False, "pattern": "B", "tier": 1}
            for i in range(8)
        ] + [
            {"text": "The docstring says 3M rows are processed", "has_evidence": False, "pattern": "A", "tier": 1},
            {"text": "As we established earlier, this runs 50 epochs", "has_evidence": False, "pattern": "C", "tier": 1},
        ]
        with DriftDB() as conn:
            insert_claims(conn, 1, claims_batch)
            insert_turn(conn, 1, len(claims_batch), 0, 1.0)
            insert_claims(conn, 2, claims_batch[:5])
            insert_turn(conn, 2, 5, 0, 1.0)
            insert_claims(conn, 3, claims_batch[:5])
            insert_turn(conn, 3, 5, 0, 1.0)

        hook_input = json.dumps({"last_assistant_message": "There are 500 rows in the dataset and model uses 192 channels."})
        result = subprocess.run(
            [sys.executable, str(DRIFT_METRIC_PY)],
            input=hook_input,
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return

        msg = data.get("systemMessage", "")
        # Check for any drift warning in the message — DRIFT CRITICAL, WARNING, funnel, or >60%
        has_warning = any(kw in msg for kw in [
            "DRIFT CRITICAL", "DRIFT WARNING", "DRIFT > 60%", "DRIFT > 30%",
            "drift advisory", "!! DRIFT", "! DRIFT",
        ])
        if not has_warning:
            _fail(name, f"no funnel warning in message; msg={msg[:300]}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))
    finally:
        _remove_db()


# ── Test 14: Commit gate — empty stdin ──

def test_commit_gate_empty_stdin():
    name = "Commit gate — empty stdin: outputs {\"decision\":\"allow\"}"
    try:
        result = subprocess.run(
            [str(GIT_COMMIT_GATE)],
            input="",
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        if not stdout:
            _fail(name, f"empty stdout; stderr={result.stderr[:200]}")
            return
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return
        if data.get("decision") != "allow":
            _fail(name, f"expected decision=allow, got {data}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 15: Push gate — empty stdin ──

def test_push_gate_empty_stdin():
    name = "Push gate — empty stdin: outputs {\"decision\":\"allow\"}"
    try:
        result = subprocess.run(
            [str(GIT_PUSH_GATE)],
            input="",
            capture_output=True,
            text=True,
            cwd=str(HOOKS_DIR),
            timeout=30,
        )
        stdout = result.stdout.strip()
        if not stdout:
            _fail(name, f"empty stdout; stderr={result.stderr[:200]}")
            return
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            _fail(name, f"invalid JSON: {e}; stdout={stdout[:200]}")
            return
        if data.get("decision") != "allow":
            _fail(name, f"expected decision=allow, got {data}")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Test 16: Cleanup ──

def test_cleanup():
    name = "Cleanup: remove drift.db and drift_errors.log"
    try:
        _remove_db()
        err_log = HOOKS_DIR / "drift_errors.log"
        if err_log.exists():
            err_log.unlink()
        if DB_PATH.exists():
            _fail(name, "drift.db still exists after removal")
            return
        _pass(name)
    except Exception as e:
        _fail(name, str(e))


# ── Main ──

def main():
    print("=" * 60)
    print("Drift Hook System — Integration Test Suite")
    print("=" * 60)
    print()

    # Ensure we start clean
    _remove_db()

    test_fresh_db()
    test_insert_and_persist()
    test_tier1_detection()
    test_tier2_detection()
    test_pattern_a()
    test_pattern_b()
    test_pattern_c()
    test_drift_metric_output()
    test_drift_analysis_panel()
    test_missing_db_recovery()
    test_corrupt_db_recovery()
    test_safe_execute()
    test_dynamic_funnel_high_drift()
    test_commit_gate_empty_stdin()
    test_push_gate_empty_stdin()
    test_cleanup()

    print()
    print("=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print(f"Results: {passed} passed, {failed} failed out of {len(_results)} tests")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
