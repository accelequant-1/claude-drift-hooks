#!/usr/bin/env python3
"""
Instruction Drift Metric — Post-response hook for Claude Code.

Extracts factual claims from assistant responses, checks for inline evidence,
stores claim hashes (NOT raw text) in SQLite, and displays:
  1. Session drift score
  2. Top 3 unchecked claims with suggested verification commands

Patterns:
  A: Docstring trust — claim cites a comment/docstring, not runtime evidence
  B: Narrative fabrication — round numbers, simplified counts
  C: Assumption propagation — repeats prior context without fresh check
  D: Post-compaction stale — claims about prior work after context clear/compact
"""

import json
import os
import re
import sys
from pathlib import Path

# Import the DB module from the same directory
sys.path.insert(0, str(Path(__file__).parent))
try:
    import drift_db
    _DB_AVAILABLE = True
except Exception as _db_import_err:
    _DB_AVAILABLE = False


def _tier1_has_claim(line_s: str) -> bool:
    """Tier 1 claim detection: existing regex patterns."""
    if re.search(r'\d+[%x×]|\d+\.\d+', line_s):
        return True
    if re.search(r'\d+[KMGkmg]?\s*(files?|rows?|epochs?|hours?|GB|MB|KB|samples|lines?|params?|qubits?|games?|wins?|days?|seconds?)', line_s):
        return True
    # Also catch "epoch 20", "step 500", "layer 3" (unit before number)
    if re.search(r'(epoch|step|layer|block|turn|batch|iteration|version|phase|round|level)\s+\d+', line_s, re.I):
        return True
    if re.search(r'\d{1,3}(,\d{3})+', line_s):
        return True
    if re.search(r'`[a-zA-Z_]+\.(py|sh|cfg|cpp|txt|npz|ckpt|gz)`', line_s):
        return True
    if re.search(r'\d+[x×]\s*(faster|slower|better|worse|fewer|more|speedup|improvement|reduction|compression)', line_s, re.I):
        return True
    return False


def _tier1_has_evidence(line_s: str) -> bool:
    """Tier 1 evidence detection: existing regex patterns."""
    if re.search(r'`?\w+\.\w+:\d+`?', line_s):
        return True
    if re.search(r'verified|from.*\.jsonl|from.*log|from.*output|checked|confirmed', line_s, re.I):
        return True
    if re.search(r'grep|wc -l|ls -l|cat |head |git show|git log', line_s, re.I):
        return True
    return False


def _tier1_pattern(line_s: str) -> str | None:
    """Tier 1 pattern classification (A/B/C) for unverified claims."""
    if re.search(r'docstring|comment|says|describes|according to|documentation', line_s, re.I):
        return "A"
    # Pattern B: round numbers OR approximate language
    if re.search(r'\b(~?\d+)([05]0+|000)\b', line_s):
        return "B"
    if re.search(r'\b(approximately|roughly|about|around|nearly|almost)\b|~\s*\d+', line_s, re.I):
        return "B"
    if re.search(r'as mentioned|previously|we established|earlier|as shown|as noted', line_s, re.I):
        return "C"
    return None


# ---------------------------------------------------------------------------
# Tier 2 heuristics
# ---------------------------------------------------------------------------

_NOUN_LIKE = r'(?:file|line|row|model|layer|block|epoch|step|run|pass|sample|token|batch|weight|param|node|channel|qubit|board|game|move|turn|win|loss|gpu|cpu|second|minute|hour|day|week|byte|chunk|item|entry|record|image|output|result|metric|score|rate|count|size|number|version|release|branch|commit|feature|class|method|function|module|package|test|case|error|warning|message|request|response|query|key|value|field|column|table|index|threshold|limit|bound|range|set|list|dict|array|vector|matrix|tensor|process|job|task|thread|worker|server|client|connection|checkpoint|experiment|session|attempt|iteration|call|invocation|instance|object|component|service|build|artifact|dataset|corpus|config|setting|parameter|argument|flag|option|path|directory|folder|script|tool|command|pid|port|address|endpoint|variable|constant|symbol)'

def _tier2_has_claim(line_s: str) -> bool:
    """Tier 2 claim heuristics for lines Tier 1 missed."""
    # Number adjacent to a noun (digit followed within ~3 words by a noun-like word)
    if re.search(r'\d+\s*(?:\w+\s*){0,3}' + _NOUN_LIKE, line_s, re.I):
        return True
    # Backtick-wrapped identifier: `anything.ext` or `any_name`
    if re.search(r'`[^`]+\.[a-zA-Z0-9]+`|`[a-zA-Z_][a-zA-Z0-9_]+`', line_s):
        return True
    # Comparative language
    if re.search(r'\b(better|faster|more|improved|worse|fewer|higher|lower|outperforms)\b', line_s, re.I):
        return True
    # Temporal language
    if re.search(r'\b(after|took|during|spent)\b|\bin\s+\d+\s+(hours?|minutes?|days?)\b', line_s, re.I):
        return True
    # Assertive framing at start of line
    if re.match(r'(The model|This produces|It achieves|The trunk|The circuit|Our code)\b', line_s, re.I):
        return True
    # Prior-reference language (these are claims repeating unverified prior context)
    if re.search(r'\b(as we established|as mentioned|as we saw|as before|we said|I noted|you said|earlier|previously)\b', line_s, re.I):
        return True
    return False


def _tier2_has_evidence(line_s: str, prev_line: str) -> bool:
    """Tier 2 evidence heuristics."""
    # Tool output reference
    if re.search(r'from the output|the command shows|PID\s+\d+|exit code', line_s, re.I):
        return True
    # file:line reference in various formats
    if re.search(r'\w+\.\w+:\d+|line\s+\d+\s+of\s+\w+|at\s+L\d+|\(line\s+\d+\)', line_s, re.I):
        return True
    # Verification language
    if re.search(r'\b(verified|confirmed|checked)\b|from\s+\S+\s+output|shows\s+that|running\s+\S+\s+returns', line_s, re.I):
        return True
    # Previous line was end of a code block or a shell command line
    prev_s = prev_line.strip()
    if prev_s == '```' or prev_s.startswith('```'):
        return True
    if prev_s and (prev_s.lstrip().startswith('$') or prev_s.lstrip().startswith('#')):
        return True
    return False


def _tier2_pattern(line_s: str) -> str | None:
    """Tier 2 pattern classification for unverified claims."""
    # Pattern A: docstring/comment/README trust
    if re.search(r'docstring|comment|README|the code says|header says|described as|notes that|the docs say', line_s, re.I):
        return "A"
    # Pattern B: approximate language with numbers
    if re.search(r'\b(approximately|roughly|about|around|nearly|almost)\b|~\s*\d+', line_s, re.I):
        return "B"
    # Pattern C: prior reference
    if re.search(r'\b(as mentioned|earlier|we said|previously|above|we established|you said|I noted|as we saw|as before)\b', line_s, re.I):
        return "C"
    return None


def _has_post_compaction_signal(line_s: str) -> bool:
    """Detect claims that reference prior work without fresh verification.

    After context compaction or /clear, Claude loses conversation history
    but the drift DB persists. Claims about "what we did" or "what we found"
    are especially dangerous here because Claude is reconstructing from
    fragments, not from actual memory of the work.

    Also catches user-side drift: when the user assumes something and Claude
    echoes it without checking.
    """
    return bool(re.search(
        r'\b(we found|we saw|we established|we determined|we confirmed|'
        r'we verified|we checked|as we discussed|from our earlier|'
        r'in the previous|last time|before the|when we ran|'
        r'the results showed|the output was|I recall|if I remember|'
        r'you mentioned|you said|you noted|you found|'
        r'that should be|that would be|that was already|'
        r'it was working|it should work|it already|we already)\b',
        line_s, re.I
    ))


def _tier3_has_claim(line_s: str) -> bool:
    """Tier 3 claim detection: catch-all for substantive statements.

    Every non-trivial sentence is a potential claim. Research teams use
    domain-specific language (QML, equivariant, D4, etc.) that tiers 1-2
    miss. Tier 3 catches any sentence that makes a statement, unless it's
    clearly a question or filler.
    """
    # Skip very short lines (greetings, "yes", "ok", etc.)
    if len(line_s) < 15:
        return False
    # Skip questions
    if line_s.rstrip().endswith("?"):
        return False
    # Skip pure list markers without content
    if re.match(r'^[-*]\s*$', line_s):
        return False
    # Any remaining sentence with a verb-like structure is a claim
    # (subject + verb patterns, or imperative statements)
    if re.search(r'\b(is|are|was|were|has|have|had|does|do|did|will|would|can|could|should|must|means|uses|runs|takes|produces|generates|creates|returns|shows|contains|includes|supports|requires|needs|works|handles|processes|stores|loads|reads|writes|calls|sends|receives|accepts|provides|allows|enables|prevents|ensures|maintains|preserves|tracks|measures|detects|computes|calculates|implements|defines|represents|maps|converts|transforms|encodes|decodes)\b', line_s, re.I):
        return True
    # Declarative statements starting with common patterns
    if re.match(r'(The|This|That|It|They|We|Our|Your|Each|Every|All|No|Any|Most|Some)\b', line_s):
        return True
    return False


def analyze_response(response_text: str) -> list[dict]:
    """Extract claims from response text. Returns list of claim dicts.

    Three tiers of detection (all responses tracked, no cold start):
      Tier 1: Numbers, percentages, file references, speedup claims
      Tier 2: Comparatives, temporals, assertive framing, backtick refs
      Tier 3: Any substantive statement (catch-all for domain-specific language)
    """
    claims = []
    lines = response_text.split("\n")
    for idx, line in enumerate(lines):
        line_s = line.strip()
        if not line_s or line_s.startswith(("```", "#", "|", "---", "<", ">")):
            continue

        prev_line = lines[idx - 1] if idx > 0 else ""

        # ── Tier 1 ──
        if _tier1_has_claim(line_s):
            has_evidence = _tier1_has_evidence(line_s)
            pattern = None if has_evidence else _tier1_pattern(line_s)
            # Pattern D overrides: post-compaction stale references
            if not has_evidence and _has_post_compaction_signal(line_s):
                pattern = "D"
            claims.append({
                "text": line_s[:200],
                "has_evidence": has_evidence,
                "pattern": pattern,
                "tier": 1,
            })
            continue

        # ── Tier 2 (only for lines Tier 1 did NOT classify as claims) ──
        if _tier2_has_claim(line_s):
            has_evidence = _tier2_has_evidence(line_s, prev_line)
            pattern = None if has_evidence else _tier2_pattern(line_s)
            if not has_evidence and _has_post_compaction_signal(line_s):
                pattern = "D"
            claims.append({
                "text": line_s[:200],
                "has_evidence": has_evidence,
                "pattern": pattern,
                "tier": 2,
            })
            continue

        # ── Tier 3: catch-all for substantive statements ──
        # Every non-trivial statement is tracked. Domain-specific language
        # (QML, equivariant maps, D4 orbits, etc.) won't slip through.
        if _tier3_has_claim(line_s):
            has_evidence = (
                _tier1_has_evidence(line_s)
                or _tier2_has_evidence(line_s, prev_line)
            )
            pattern = None if has_evidence else _tier2_pattern(line_s)
            # Pattern D override: post-compaction stale claims
            if not has_evidence and _has_post_compaction_signal(line_s):
                pattern = "D"
            claims.append({
                "text": line_s[:200],
                "has_evidence": has_evidence,
                "pattern": pattern,
                "tier": 3,
            })

    return claims


def _compute_alignment(conn, stats) -> str:
    """Compute dual alignment score: Claude and Prompter.

    Claude score = evidence ratio (% claims with citations).
    Prompter score = correction ratio (% of drift drops that followed a turn,
        indicating the user pushed back and Claude improved).

    Both are 0-100. Displayed as: "alignment: Claude 45 | Prompter 70"
    """
    try:
        total = stats["total"]
        evidenced = stats["evidenced"]
        if total == 0:
            return ""

        # Claude score: straightforward evidence ratio
        claude_score = int(100 * evidenced / total)

        # Prompter score: how often did drift decrease between turns?
        # A drift decrease means the user likely pushed back or Claude self-corrected.
        # We attribute decreases to the prompter (they asked for evidence)
        # and increases to Claude (it drifted without being caught).
        turns = conn.execute(
            "SELECT drift_score FROM turns ORDER BY turn"
        ).fetchall()
        drifts = [r[0] for r in turns]

        if len(drifts) < 2:
            prompter_score = 50  # no data, assume neutral
        else:
            decreases = sum(1 for i in range(1, len(drifts)) if drifts[i] < drifts[i - 1])
            total_changes = max(len(drifts) - 1, 1)
            # Prompter score: % of turn transitions where drift improved
            # High = user is actively course-correcting
            # Low = user is accepting drift without pushback
            prompter_score = int(100 * decreases / total_changes)

        return f"alignment: Claude {claude_score} | Prompter {prompter_score}"

    except Exception:
        return ""


def _dynamic_funnel(conn, stats, drift, pa, pb, pc, pd, unchecked) -> list[str]:
    """Context-dependent accountability intervention.

    Analyzes:
      1. Drift velocity (is it getting worse or better?)
      2. Pattern composition (which failure mode is active?)
      3. Recency (are the unverified claims from this turn or old?)
      4. Severity (raw drift level)
      5. Streak (consecutive high-drift turns?)

    Returns list of message lines. Hardcoded thresholds as fallback.
    """
    lines = []
    total = stats["total"]
    if total == 0:
        return lines

    try:
        # ── 1. Drift velocity: compare last 3 turns vs prior 3 ──
        recent_turns = conn.execute(
            "SELECT drift_score FROM turns ORDER BY turn DESC LIMIT 6"
        ).fetchall()
        velocities = [r[0] for r in recent_turns]

        if len(velocities) >= 4:
            recent_avg = sum(velocities[:3]) / 3
            prior_avg = sum(velocities[3:]) / max(len(velocities[3:]), 1)
            velocity = recent_avg - prior_avg  # positive = getting worse
        else:
            velocity = 0.0

        # ── 2. Streak: consecutive turns above 50% drift ──
        streak = 0
        for v in velocities:
            if v > 0.50:
                streak += 1
            else:
                break

        # ── 3. Pattern severity weighting ──
        # D (post-compaction stale) is most dangerous — no context to check against
        # A (docstring trust) is second — leads to factual errors
        # B (fabrication) is third — leads to misleading docs
        # C (propagation) is fourth — leads to stale claims
        weighted_severity = (pd * 4 + pa * 3 + pb * 2 + pc * 1) / max(total, 1)

        # ── 4. Recency: fraction of unverified claims from last 3 turns ──
        recent_unverified = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE has_evidence = 0 AND turn >= ?",
            (stats["last_turn"] - 2,)
        ).fetchone()[0]
        recent_total = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE turn >= ?",
            (stats["last_turn"] - 2,)
        ).fetchone()[0]
        recency_drift = recent_unverified / max(recent_total, 1)

        # ── 5. Dynamic severity score (0-1) ──
        severity = (
            drift * 0.35           # base drift level
            + velocity * 0.20      # getting worse penalized
            + weighted_severity * 0.15  # pattern A penalized most
            + recency_drift * 0.20  # recent unverified penalized
            + min(streak / 5, 1.0) * 0.10  # streak penalty
        )

        # ── 6. Generate context-dependent message ──

        if severity > 0.70:
            # CRITICAL: full lockdown
            lines.append("!! DRIFT CRITICAL (severity {:.0%}) !!".format(severity))
            lines.append("STOP. Do ONLY what the user asked. CITE evidence for every claim.")
            lines.append("ASK if anything is unclear. Do NOT commit until drift < 30%.")
            if velocity > 0.1:
                lines.append("  Drift is ACCELERATING ({:+.0%}/turn). You are getting less careful.".format(velocity))
            if streak >= 3:
                lines.append("  {} consecutive high-drift turns. PAUSE and re-read the user's last instruction.".format(streak))
            # Target the dominant pattern
            _add_pattern_intervention(lines, pa, pb, pc, pd, unchecked)

        elif severity > 0.45:
            # WARNING: targeted intervention
            lines.append("! DRIFT WARNING (severity {:.0%})".format(severity))
            _add_pattern_intervention(lines, pa, pb, pc, pd, unchecked)
            if velocity > 0.05:
                lines.append("  Trending worse. Slow down and verify.")
            elif velocity < -0.1:
                lines.append("  Improving. Keep citing evidence.")

        elif severity > 0.20:
            # ADVISORY: gentle nudge
            lines.append("drift advisory (severity {:.0%}) — cite sources to stay clean".format(severity))
            if pa > pb and pa > pc and pa > 0:
                lines.append("  watch for docstring trust (Pattern A active)")
            elif pb > pa and pb > pc and pb > 0:
                lines.append("  watch for round numbers (Pattern B active)")

        else:
            # CLEAN: no intervention needed
            pass

        # ── FALLBACK: if dynamic scoring fails, use hardcoded thresholds ──
        if not lines:
            if drift > 0.60:
                lines.append("!! DRIFT > 60% — CITE evidence for every claim. ASK if unclear. !!")
            elif drift > 0.30:
                lines.append("! DRIFT > 30% — Show evidence. Ask if unclear.")
                dominant = max(pa, pb, pc)
                if pa == dominant and pa > 0:
                    lines.append("  Pattern A: READ the actual file, not the comment.")
                if pb == dominant and pb > 0:
                    lines.append("  Pattern B: Use EXACT values, not rounded.")
                if pc == dominant and pc > 0:
                    lines.append("  Pattern C: RE-VERIFY, don't repeat prior claims.")
            elif drift > 0.10:
                lines.append("  (cite sources to keep drift down)")

    except Exception:
        # If any DB query fails, return empty list — don't crash the hook
        return []

    return lines


def _add_pattern_intervention(lines, pa, pb, pc, pd, unchecked):
    """Add targeted intervention based on which pattern is dominant."""
    patterns = [("A", pa), ("B", pb), ("C", pc), ("D", pd)]
    patterns.sort(key=lambda x: x[1], reverse=True)

    for pat, count in patterns:
        if count == 0:
            continue
        if pat == "D":
            lines.append("  Pattern D ({} claims): POST-COMPACTION STALE. You are citing prior work without fresh evidence. RE-READ files and RE-RUN commands. Context was lost.".format(count))
            for item in unchecked:
                if item.get("pattern") == "D":
                    lines.append("    e.g. \"{}\"".format(item["claim_display"][:60]))
                    break
        elif pat == "A":
            lines.append("  Pattern A ({} claims): You cited a docstring or comment as truth. READ THE FILE, run the code, check the process.".format(count))
            for item in unchecked:
                if item.get("pattern") == "A":
                    lines.append("    e.g. \"{}\"".format(item["claim_display"][:60]))
                    break
        elif pat == "B":
            lines.append("  Pattern B ({} claims): Round numbers detected. Use exact values from source.".format(count))
            for item in unchecked:
                if item.get("pattern") == "B":
                    lines.append("    e.g. \"{}\"".format(item["claim_display"][:60]))
                    break
        elif pat == "C":
            lines.append("  Pattern C ({} claims): Repeating prior context. Re-verify from current file state.".format(count))
            for item in unchecked:
                if item.get("pattern") == "C":
                    lines.append("    e.g. \"{}\"".format(item["claim_display"][:60]))
                    break


def main():
    try:
        if not _DB_AVAILABLE:
            print(json.dumps({
                "systemMessage": "[DRIFT] (hook error: drift_db module could not be imported)",
                "continue": True,
            }))
            return

        try:
            hook_input = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
        except (json.JSONDecodeError, OSError):
            hook_input = {}

        # Extract response text — Stop hook provides "last_assistant_message"
        response_text = (
            hook_input.get("last_assistant_message", "")
            or hook_input.get("response", "")
        )
        if not response_text:
            transcript = hook_input.get("transcript", [])
            if transcript:
                for msg in reversed(transcript):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            response_text = " ".join(
                                b.get("text", "") for b in content if b.get("type") == "text"
                            )
                        elif isinstance(content, str):
                            response_text = content
                        break

        # Use context manager — auto-closes even on errors
        with drift_db.DriftDB() as conn:
            # Get current turn count
            stats = drift_db.get_session_drift(conn)
            turn = stats["last_turn"] + 1

            # Analyze and store — EVERY turn is recorded, no cold start.
            # Even turns with zero detected claims get a turn entry so the
            # turn counter stays accurate and the session is always tracked.
            if response_text:
                claims = analyze_response(response_text)
                if claims:
                    drift_db.insert_claims(conn, turn, claims)
                total = len(claims)
                evidenced = sum(1 for c in claims if c["has_evidence"])
                drift = (total - evidenced) / total if total > 0 else 0.0
                drift_db.insert_turn(conn, turn, total, evidenced, drift)

            # Get session-level stats
            stats = drift_db.get_session_drift(conn)

            # Get top 3 unchecked claims for display
            unchecked = drift_db.get_uncommitted_unverified(conn)[:3]

            # NOTE: conn stays open inside the with-block — _dynamic_funnel needs it
            # Format output
            total = stats["total"]
            drift = stats["drift"]
            pa, pb, pc, pd = stats["pattern_a"], stats["pattern_b"], stats["pattern_c"], stats.get("pattern_d", 0)

            pattern_parts = []
            if pa: pattern_parts.append(f"A:{pa}")
            if pb: pattern_parts.append(f"B:{pb}")
            if pc: pattern_parts.append(f"C:{pc}")
            if pd: pattern_parts.append(f"D:{pd}")
            pattern_str = f" | {' '.join(pattern_parts)}" if pattern_parts else ""

            lines = [
                f"drift: {drift:.0%} ({stats['evidenced']}/{total} evidenced){pattern_str} | turn {stats['last_turn']}"
            ]

            for item in unchecked:
                cmd = item.get("verification_cmd", "")
                cmd_str = f" -> {cmd}" if cmd else ""
                lines.append(f"  CHECK: \"{item['claim_display']}\"{cmd_str}")

            # ── DYNAMIC FUNNEL ──
            # Context-dependent intervention based on drift severity, velocity,
            # pattern composition, and recency. Hardcoded thresholds are fallback.

            funnel_msg = _dynamic_funnel(conn, stats, drift, pa, pb, pc, pd, unchecked)
            if funnel_msg:
                lines.append("")
                lines.extend(funnel_msg)

            # ── ALIGNMENT SCORE ──
            # Dual accountability: Claude score + Prompter score.
            # Claude: % of claims with evidence (higher = better)
            # Prompter: % of turns where user pushed back / asked for clarification
            #   (approximated by: turns where drift DECREASED after user intervention)
            # Both scores persist across context compaction.
            alignment = _compute_alignment(conn, stats)
            if alignment:
                lines.append("")
                lines.append(alignment)

        msg = "\n".join(lines)

        output = {
            "systemMessage": f"[DRIFT]\n{msg}",
            "continue": True,
        }
        print(json.dumps(output))

    except Exception as exc:
        print(json.dumps({
            "systemMessage": f"[DRIFT] (hook error: {exc})",
            "continue": True,
        }))


if __name__ == "__main__":
    main()
