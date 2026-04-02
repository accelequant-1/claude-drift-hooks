"""
Drift Hook Configuration — thresholds configurable via env vars with hard floors.

Environment variables:
    DRIFT_COMMIT_THRESHOLD  — drift level that triggers commit gate warning (default: 0.30)
    DRIFT_PUSH_THRESHOLD    — drift level that blocks push (default: 0.15)
    DRIFT_COMMIT_FLOOR      — minimum commit threshold (cannot go above this, default: 0.50)
    DRIFT_PUSH_FLOOR        — minimum push threshold (cannot go above this, default: 0.30)

Example:
    export DRIFT_COMMIT_THRESHOLD=0.20  # stricter commit gate
    export DRIFT_PUSH_THRESHOLD=0.10    # stricter push gate
"""

import os

def _get_threshold(env_var: str, default: float, floor: float) -> float:
    """Get threshold from env, clamped to [0, floor]."""
    try:
        val = float(os.environ.get(env_var, default))
    except (ValueError, TypeError):
        val = default
    return min(max(val, 0.0), floor)

# Commit gate: warn + require --drift-override above this level
COMMIT_THRESHOLD = _get_threshold("DRIFT_COMMIT_THRESHOLD", 0.30,
                                   float(os.environ.get("DRIFT_COMMIT_FLOOR", 0.50)))

# Push gate: block push above this level
PUSH_THRESHOLD = _get_threshold("DRIFT_PUSH_THRESHOLD", 0.15,
                                 float(os.environ.get("DRIFT_PUSH_FLOOR", 0.30)))

# Dynamic funnel severity thresholds
FUNNEL_CRITICAL = 0.70   # full lockdown
FUNNEL_WARNING = 0.45    # targeted intervention
FUNNEL_ADVISORY = 0.20   # gentle nudge
