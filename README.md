# Claude Drift Hooks

Instruction drift measurement and enforcement for Claude Code sessions.

Measures the gap between what Claude claims and what Claude can prove. Gates git commits and pushes based on evidence quality.

## The Problem

In a 700+ turn session, we measured **87.4% drift** — 229 out of 262 technical claims had no inline evidence (file:line citation, command output, or process verification). 8 of those were factually wrong. The rest were correct but unverifiable without checking yourself.

The breakdown layer: Claude says "checked" without showing the evidence. You accept because you can't see the work.

## What This Does

Three layers of enforcement:

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

## The Three Drift Patterns

| Pattern | What Claude does | How the hook catches it |
|---------|-----------------|----------------------|
| **A: Docstring trust** | Cites a comment or docstring as evidence of runtime behavior | Detects "docstring", "comment", "says", "documentation" near claims |
| **B: Narrative fabrication** | Rounds numbers, simplifies counts, constructs clean stories | Detects approximate language ("roughly", "about", "~") near numbers |
| **C: Assumption propagation** | Repeats prior-context claims without re-verifying | Detects "as mentioned", "earlier", "we established", "previously" |

## Configure Thresholds

```bash
# Stricter (recommended for production docs)
export DRIFT_COMMIT_THRESHOLD=0.20
export DRIFT_PUSH_THRESHOLD=0.10

# Looser (for exploratory sessions)
export DRIFT_COMMIT_THRESHOLD=0.40
export DRIFT_PUSH_THRESHOLD=0.25
```

Hard floors prevent disabling:
- Commit threshold cannot exceed 0.50
- Push threshold cannot exceed 0.30

## Two-Tier Claim Detection

**Tier 1 (regex, <1ms):** Catches numbers with units, percentages, file references, speedup claims.

**Tier 2 (heuristic, <1ms):** Catches comparative language ("outperforms"), temporal claims ("took 3 hours"), assertive framing ("The model produces"), PID/process references.

## Privacy

- Claim texts are **hashed** (SHA-256) before storage. The DB stores `hash(text)`, not the raw text.
- Only a redacted display summary is kept (first 5 words + last word).
- The SQLite DB is local-only, `0600` permissions, git-ignored.
- No network calls. No telemetry. No data leaves your machine.
- Auto-truncates after 7 days.

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

Built during a forensic code review session where instruction drift led to wrong documentation, wrong file sets, and multiple correction cycles across two GCP instances. The drift metric is the tool we wish we had from the start.
