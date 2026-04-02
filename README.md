# claude-drift-hooks

Instruction drift measurement + git enforcement for Claude Code. Measures the gap between what Claude claims and what Claude can prove.

## Why

In a long session we found 87% of technical claims lacked a file:line citation or command output. Most were correct but unverifiable without manually checking. After context compaction or `/clear`, it gets worse — Claude reconstructs from fragments and echoes user assumptions without checking. This hooks into the session to surface that.

## How it works

Three hooks:

| Layer | When | What |
|-------|------|------|
| **Stop hook** | After every response | Shows drift score + top 3 unchecked claims with verify commands |
| **Commit gate** | On `git commit` | Warns at >30% drift. Requires `--drift-override` in commit message to proceed |
| **Push gate** | On `git push` | Blocks at >15% drift. Suggests `git revert` for unverified commits |

Plus a dynamic funnel that escalates intervention based on:
- **Velocity** — is drift getting worse or better?
- **Streak** — how many consecutive high-drift turns?
- **Pattern composition** — which failure mode is active?
- **Recency** — are the bad claims fresh or old?

## Install

```bash
# 1. Clone once (anywhere on your system)
git clone git@github.com:accelequant-1/claude-drift-hooks.git ~/claude-drift-hooks

# 2. cd into your project root
cd /path/to/your/project

# 3. Run the installer
bash ~/claude-drift-hooks/install.sh

# 4. Verify
python3 .claude/hooks/test_drift_system.py
```

The installer:
- Copies hook scripts to `.claude/hooks/`
- Copies the `/drift` slash command to `.claude/commands/`
- Creates or merges `.claude/settings.local.json` with **absolute paths** (required — Claude Code hooks can run from any working directory)
- Adds `.claude/.gitignore` entries for the local DB and logs

### Important: absolute paths

Claude Code hooks execute from an **unpredictable working directory** — not necessarily your project root. The installer writes absolute paths like:

```json
"command": "bash /home/you/your-project/.claude/hooks/drift-metric.sh"
```

If you move your project directory, re-run `install.sh` to regenerate the paths.

### Manual setup (if you skip install.sh)

If you copy the files yourself, you must create `.claude/settings.local.json` with absolute paths to your hooks directory:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/your/project/.claude/hooks/drift-metric.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash(git commit*)",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/your/project/.claude/hooks/git-commit-gate.sh",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Bash(git push*)",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/your/project/.claude/hooks/git-push-gate.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Replace `/absolute/path/to/your/project` with your actual project root. Do NOT use relative paths like `.claude/hooks/...` — they will break.

## Usage

After installation, everything is automatic:

- **After every Claude response**: drift score appears in the status line
- **`/drift`**: full analysis panel (velocity, patterns, phase transitions, next steps)
- **`git commit`**: gated. High drift = warning with CHECK items
- **`git push`**: gated. High drift = blocked with revert suggestions

## Drift patterns

| Pattern | What Claude does | How the hook catches it |
|---------|-----------------|----------------------|
| **A: Docstring trust** | Cites a comment or docstring as evidence of runtime behavior | Detects "docstring", "comment", "says", "documentation" near claims |
| **B: Narrative fabrication** | Rounds numbers, simplifies counts, constructs clean stories | Detects approximate language ("roughly", "about", "~") near numbers |
| **C: Assumption propagation** | Repeats prior-context claims without re-verifying | Detects "as mentioned", "earlier", "we established", "previously" |
| **D: Post-compaction stale** | After context clear/compact, cites "what we found" without re-reading | Detects "we found", "we confirmed", "you mentioned", "it was working" |

Pattern D is the most dangerous — after compaction Claude has no actual memory of the prior work, only fragments. It will confidently reconstruct a narrative that may be wrong. The hook catches this and forces re-verification.

## Claim detection

Three tiers, all under 1ms:

- **Tier 1 (regex):** Numbers with units, percentages, file references, speedup claims.
- **Tier 2 (heuristic):** Comparative language, temporal claims, assertive framing, PID/process references.
- **Tier 3 (catch-all):** Any substantive statement with a verb. Catches domain-specific language (QML, equivariant maps, D4 orbits, etc.) that tiers 1-2 miss.

Every turn is recorded regardless of whether claims are detected — no cold start gap.

## Thresholds

Configurable via env vars:

```bash
export DRIFT_COMMIT_THRESHOLD=0.20  # default 0.30
export DRIFT_PUSH_THRESHOLD=0.10    # default 0.15
```

Hard floors: commit max 0.50, push max 0.30.

## Privacy

- Claim texts hashed (SHA-256) before storage. DB stores `hash(text)`, not raw text.
- Redacted display only (first 5 words + last word).
- SQLite local-only, `0600` permissions, git-ignored.
- No network calls. Auto-truncates after 7 days.

## Files

| File | Purpose |
|------|---------|
| `drift_db.py` | SQLite schema, context manager, transactions, safe queries |
| `drift-metric.py` | Stop hook: claim detection (3 tiers), drift scoring, dynamic funnel |
| `drift-metric.sh` | Shell wrapper for the Stop hook |
| `drift_analysis.py` | `/drift` panel: velocity, patterns, transitions, next steps |
| `git-commit-gate.sh` | PreToolUse hook: commit gating |
| `git-push-gate.sh` | PreToolUse hook: push gating |
| `config.py` | Configurable thresholds with hard floors |
| `test_drift_system.py` | Integration test suite |
| `commands/drift.md` | `/drift` slash command definition |
| `settings-snippet.json` | Hook config template (install.sh generates absolute paths from this) |
| `install.sh` | One-command installer for any project |

## Origin

Internal tool from a session where uncited claims led to wrong docs and multiple correction cycles.
