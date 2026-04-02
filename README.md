# claude-drift-hooks

Internal hooks for measuring instruction drift in Claude Code sessions.

Tracks whether Claude's factual claims have inline evidence. Gates commits and pushes when too many claims are uncited.

## Why

In a long session we found 87% of technical claims lacked a file:line citation or command output. Most were correct but unverifiable without manually checking. This hooks into the session to surface that.

## How it works

Three hooks:

| Layer | When | What |
|-------|------|------|
| **Stop hook** | After every response | Shows drift score + top 3 unchecked claims with verify commands |
| **Commit gate** | On `git commit` | Warns at >30% drift. Requires `--drift-override` in commit message to proceed. |
| **Push gate** | On `git push` | Blocks at >15% drift. Suggests `git revert` for unverified commits. |

Plus a dynamic funnel that escalates intervention based on:
- **Velocity** — is drift getting worse or better?
- **Streak** — how many consecutive high-drift turns?
- **Pattern composition** — which failure mode is active?
- **Recency** — are the bad claims fresh or old?

## Install

```bash
# Clone once
git clone git@github.com:accelequant-1/claude-drift-hooks.git ~/claude-drift-hooks

# Install into any project
cd /path/to/your/project
bash ~/claude-drift-hooks/install.sh

# Verify
python3 .claude/hooks/test_drift_system.py
```

## Usage

After installation, everything is automatic:

- **After every Claude response**: drift score appears in transcript
- **`/drift`**: type this for the full analysis panel (velocity, patterns, phase transitions, next steps)
- **`git commit`**: gated. High drift = warning with CHECK items
- **`git push`**: gated. High drift = blocked with revert suggestions

## Drift patterns

| Pattern | What Claude does | How the hook catches it |
|---------|-----------------|----------------------|
| **A: Docstring trust** | Cites a comment or docstring as evidence of runtime behavior | Detects "docstring", "comment", "says", "documentation" near claims |
| **B: Narrative fabrication** | Rounds numbers, simplifies counts, constructs clean stories | Detects approximate language ("roughly", "about", "~") near numbers |
| **C: Assumption propagation** | Repeats prior-context claims without re-verifying | Detects "as mentioned", "earlier", "we established", "previously" |

## Thresholds

Configurable via env vars:

```bash
export DRIFT_COMMIT_THRESHOLD=0.20  # default 0.30
export DRIFT_PUSH_THRESHOLD=0.10    # default 0.15
```

Hard floors: commit max 0.50, push max 0.30.

## Claim detection

Two tiers, both under 1ms:

- **Tier 1 (regex):** Numbers with units, percentages, file references, speedup claims.
- **Tier 2 (heuristic):** Comparative language, temporal claims, assertive framing, PID/process references.

## Privacy

- Claim texts hashed (SHA-256) before storage. DB stores `hash(text)`, not raw text.
- Redacted display only (first 5 words + last word).
- SQLite local-only, `0600` permissions, git-ignored.
- No network calls. Auto-truncates after 7 days.

## Files

| File | Purpose |
|------|---------|
| `drift_db.py` | SQLite schema, context manager, transactions, safe queries |
| `drift-metric.py` | Stop hook: claim detection, drift scoring, dynamic funnel |
| `drift-metric.sh` | Shell wrapper for the Stop hook |
| `drift_analysis.py` | `/drift` panel: velocity, patterns, transitions, next steps |
| `git-commit-gate.sh` | PreToolUse hook: commit gating |
| `git-push-gate.sh` | PreToolUse hook: push gating |
| `config.py` | Configurable thresholds with hard floors |
| `test_drift_system.py` | 16-test integration suite |
| `commands/drift.md` | `/drift` slash command definition |
| `settings-snippet.json` | Hook configuration to merge into settings.local.json |
| `install.sh` | One-command installer for any project |

## Origin

Internal tool from a session where uncited claims led to wrong docs and multiple correction cycles.
