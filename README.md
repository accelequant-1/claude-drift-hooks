# claude-drift-hooks

Active verification system for Claude Code. Tracks every factual claim, auto-verifies citations, blocks high-drift commits/pushes, and records exact file:line evidence in a persistent ledger.

## Why

In a long session we found 87% of technical claims lacked a file:line citation or command output. Most were correct but unverifiable without manually checking. After context compaction or `/clear`, it gets worse — Claude reconstructs from fragments and echoes user assumptions without checking.

This hook system doesn't just measure drift — it actively closes the gap:
- **Auto-verifies** claims when Claude cites `file.py:42` in a response
- **Blocks** Claude from stopping when drift is high, forcing verification
- **Records** every verification with exact file path, line number, and code snippet
- **Tracks** both Claude and Prompter alignment scores toward 100/100

## How it works

### Three enforcement layers

| Layer | When | What |
|-------|------|------|
| **Stop hook** | After every response | Detects claims, auto-verifies citations, blocks if drift > 50% |
| **Commit gate** | On `git commit` | Warns at >30% drift. Requires `--drift-override` to proceed |
| **Push gate** | On `git push` | Blocks at >15% drift. Suggests `git revert` for unverified commits |

### Auto-verification loop

```
Claude responds with "The model has 10M params"
  → Hook detects claim, stores in DB (verified=0)
  → Hook blocks: "Run these commands and cite results"
  → Claude runs: grep -c param model.py → cites model.py:42
  → Hook matches citation to pending claim
  → Sets verified=1, records file:line in verifications table
  → Writes to verification_ledger.jsonl (audit trail)
```

### Alignment scores

```
alignment: Claude 60 | Prompter 75
  Claude: 5 uncited claims — use file:line refs and command output
  Prompter: 1 uncorrected drift spike — paste CHECK items back to close them
```

- **Claude score**: weighted evidence ratio (verified claims 100%, auto-evidenced 80%, uncited 0%)
- **Prompter score**: correction ratio (% of drift spikes followed by improvement)
- Both show actionable guidance to reach 100

### Drift patterns

| Pattern | What Claude does | How the hook catches it |
|---------|-----------------|----------------------|
| **A: Docstring trust** | Cites a comment/docstring as evidence of runtime behavior | Detects "docstring", "comment", "documentation" near claims |
| **B: Narrative fabrication** | Rounds numbers ("roughly 500", "about 80%") | Detects approximate language near numbers |
| **C: Assumption propagation** | Repeats prior-context claims without re-verifying | Detects "as mentioned", "earlier", "we established" |
| **D: Post-compaction stale** | After context clear/compact, cites "what we found" without re-reading | Detects "we found", "you mentioned", "if I remember" |

Pattern D is the most dangerous — after compaction Claude has no actual memory of the prior work. The PostCompact hook detects compaction events and raises Pattern D severity from 4x to 6x for 5 minutes.

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
- Copies all hook scripts to `.claude/hooks/`
- Copies the `/drift` slash command to `.claude/commands/`
- Creates or merges `.claude/settings.local.json` with **absolute paths**
- Registers Stop, PreToolUse (commit + push), and PostCompact hooks
- Adds gitignore entries for drift.db, verification_ledger.jsonl, and logs

### Important: absolute paths

Claude Code hooks execute from an **unpredictable working directory**. The installer writes absolute paths like:

```json
"command": "bash /home/you/your-project/.claude/hooks/drift-metric.sh"
```

If you move your project directory, re-run `install.sh` to regenerate the paths.

### Manual setup

If you skip `install.sh`, create `.claude/settings.local.json` with absolute paths:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/.claude/hooks/drift-metric.sh",
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
            "command": "bash /absolute/path/to/.claude/hooks/git-commit-gate.sh",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Bash(git push*)",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/.claude/hooks/git-push-gate.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash /absolute/path/to/.claude/hooks/post-compact-hook.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## Usage

After installation, everything is automatic:

- **After every Claude response**: drift score + verification status appears
- **`/drift`**: full analysis panel with verification ledger, patterns, velocity, alignment
- **`git commit`**: gated at 30% drift (override with `--drift-override` in message)
- **`git push`**: blocked at 15% drift
- **High drift (>50%)**: Claude is blocked from stopping until it runs verification commands

### What the Stop hook output looks like

```
drift: 52% (11/23 evidenced, 5 verified) | B:4 D:3 | turn 6
  VERIFIED this turn (2):
    ✓ "The model has 10M params" @ model.py:42
    ✓ "Config uses batch 256" @ config.py:15
  PENDING (7 unclosed):
    "Training takes roughly 4h [B]"
      → run the command to get the exact number, not a rounded estimate
    "We found the win rate [D]"
      → re-read the file and re-run the command — don't trust prior context

  alignment: Claude 60 | Prompter 75
    Claude: 7 uncited claims — use file:line refs and command output
    Prompter: 1 uncorrected drift spike — paste CHECK items back to close them
```

### Getting both scores to 100

**Claude → 100**: Every claim must have a file:line citation or command output. Use `Read`, `Grep`, `Bash` before making assertions. The hook auto-verifies when you cite `file.py:42`.

**Prompter → 100**: When you see PENDING items after a response, paste them back ("verify these claims"). Every time drift drops after your pushback, your score goes up.

## Claim detection

Three tiers, all under 1ms:

- **Tier 1 (regex):** Numbers with units, percentages, file references, speedup claims
- **Tier 2 (heuristic):** Comparatives, temporals, assertive framing, backtick refs
- **Tier 3 (catch-all):** Any substantive statement with a verb (catches domain-specific language)

Every turn is recorded regardless of whether claims are detected — no cold start gap.

## Verification system

### Database schema

```
claims:        id, turn, claim_hash, claim_display, has_evidence, verified, pattern, verification_cmd
verifications: id, claim_id, citation_type, file_path, line_number, byte_offset, snippet, cmd_output_hash
turns:         turn, total_claims, evidenced_claims, drift_score
commits:       sha, drift_score, override, total_claims, unverified_claims
compaction_events: timestamp, trigger
```

### Verification ledger

`verification_ledger.jsonl` — persistent JSONL audit trail:

```json
{"timestamp": "2026-04-02T11:30:00", "claim_id": 3, "citation": {"type": "file", "file": "config.py", "line": 15, "byte": null, "snippet": "batch_size = 256"}, "status": "verified"}
```

Survives DB resets. Gitignored (local only).

## Thresholds

Configurable via env vars:

```bash
export DRIFT_COMMIT_THRESHOLD=0.20  # default 0.30
export DRIFT_PUSH_THRESHOLD=0.10    # default 0.15
```

Hard floors: commit max 0.50, push max 0.30.

## Privacy

- Claim texts hashed (SHA-256) before storage. DB stores `hash(text)`, not raw text
- Redacted display only (first 5 words + last word)
- SQLite local-only, `0600` permissions, gitignored
- No network calls. Auto-truncates after 7 days

## Files

| File | Purpose |
|------|---------|
| `drift-metric.py` | Stop hook: 3-tier claim detection, auto-verification, citation matching, decision:block |
| `drift-metric.sh` | Shell wrapper for the Stop hook |
| `drift_db.py` | SQLite schema, verifications table, citation ledger, all DB operations |
| `drift_analysis.py` | `/drift` panel: velocity, patterns, transitions, verification ledger, alignment |
| `git-commit-gate.sh` | PreToolUse hook: commit gating at 30% drift |
| `git-push-gate.sh` | PreToolUse hook: push gating at 15% drift |
| `post-compact-hook.sh` | PostCompact hook: records compaction events, spikes Pattern D sensitivity |
| `config.py` | Configurable thresholds with hard floors |
| `test_drift_system.py` | Integration test suite |
| `commands/drift.md` | `/drift` slash command definition |
| `settings-snippet.json` | Hook config template (install.sh generates absolute paths) |
| `install.sh` | One-command installer for any project |

## Origin

Internal tool from a QML research team where uncited claims led to wrong docs and multiple correction cycles.
