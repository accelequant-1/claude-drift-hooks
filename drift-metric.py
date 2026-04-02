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
    if re.search(r'docstring|comment|README|the code says|header says|described as|notes that|the docs say|documentation|according to', line_s, re.I):
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


def _has_structural_evidence(line_s: str) -> bool:
    """Evidence that survives post-compaction: file:line refs, command output.

    Words like "verified" and "confirmed" are NOT structural evidence —
    they are claims about past verification. Only concrete references to
    files, line numbers, and command output count.
    """
    # file:line reference
    if re.search(r'`?\w+\.\w+:\d+`?', line_s):
        return True
    # Command output reference
    if re.search(r'grep|wc -l|ls -l|cat |head |git show|git log', line_s, re.I):
        return True
    # Explicit "from <file> output"
    if re.search(r'from.*\.jsonl|from.*\.log|from.*output', line_s, re.I):
        return True
    return False


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
    if re.search(r'\b(is|are|was|were|has|have|had|does|do|did|will|would|can|could|should|must|means|uses|runs|takes|produces|generates|creates|returns|shows|contains|includes|supports|requires|needs|works|handles|processes|stores|loads|reads|writes|calls|sends|receives|accepts|provides|allows|enables|prevents|ensures|maintains|preserves|tracks|measures|detects|computes|calculates|implements|defines|represents|maps|converts|transforms|encodes|decodes|matches|inherits|exists|passes|fails|operates|verifies|confirms|validates|expects|achieves|reaches|exceeds)\b', line_s, re.I):
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

        # ── Post-compaction check (shared across tiers) ──
        # "We confirmed X" is a CLAIM about past verification, not fresh evidence.
        # When a post-compaction signal is present, words like "verified" and
        # "confirmed" don't count as evidence — only structural evidence does
        # (file:line refs, command output, tool results).
        is_post_compaction = _has_post_compaction_signal(line_s)

        # ── Tier 1 ──
        if _tier1_has_claim(line_s):
            has_evidence = _tier1_has_evidence(line_s)
            if is_post_compaction and has_evidence:
                # Re-check: only structural evidence survives post-compaction
                has_evidence = _has_structural_evidence(line_s)
            pattern = None if has_evidence else _tier1_pattern(line_s)
            if not has_evidence and is_post_compaction:
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
            if is_post_compaction and has_evidence:
                has_evidence = _has_structural_evidence(line_s)
            pattern = None if has_evidence else _tier2_pattern(line_s)
            if not has_evidence and is_post_compaction:
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
            if is_post_compaction and has_evidence:
                has_evidence = _has_structural_evidence(line_s)
            pattern = None if has_evidence else _tier2_pattern(line_s)
            if not has_evidence and is_post_compaction:
                pattern = "D"
            claims.append({
                "text": line_s[:200],
                "has_evidence": has_evidence,
                "pattern": pattern,
                "tier": 3,
            })

    return claims


# ---------------------------------------------------------------------------
# Citation extraction & auto-verification
# ---------------------------------------------------------------------------

def _extract_citations(response_text: str) -> list[dict]:
    """Extract exact citations from a response for matching against pending claims.

    Detects:
    - file.py:42 references (file + line number)
    - `wc -l`, `grep`, `ls -l` command invocations
    - Explicit "verified via" / "confirmed at" patterns
    """
    citations = []

    # file:line references (e.g., drift_db.py:33, config.py:15)
    for m in re.finditer(r'`?(\w[\w/.-]*\.\w+):(\d+)`?', response_text):
        citations.append({
            "type": "file",
            "file_path": m.group(1),
            "line_number": int(m.group(2)),
            "byte_offset": None,
            "snippet": None,
            "verification_cmd": None,
            "cmd_output_hash": None,
            "raw_match": m.group(0),
        })

    # Command output patterns (wc -l, grep, ls -lh, etc.)
    for m in re.finditer(r'(?:wc -l|grep -[ncl]|ls -[lh]+|head -\d+|cat )\s*(\S+)', response_text):
        citations.append({
            "type": "command",
            "file_path": m.group(1) if not m.group(1).startswith('-') else None,
            "line_number": None,
            "byte_offset": None,
            "snippet": None,
            "verification_cmd": m.group(0).strip(),
            "cmd_output_hash": None,
            "raw_match": m.group(0),
        })

    return citations


def _match_and_verify(conn, citations: list[dict], unverified: list[dict]):
    """Match extracted citations against unverified claims and record verifications.

    Matching heuristics (in priority order):
    1. File path overlap: claim display or DB text mentions file X
    2. Keyword overlap: extract significant words from claim and citation
    3. Numeric overlap: claim mentions number N, citation references same magnitude
    4. Command match: claim's verification_cmd matches a run command
    """
    if not citations or not unverified:
        return

    for claim in unverified:
        display = claim.get("claim_display", "")
        claim_vcmd = claim.get("verification_cmd", "")

        # Extract keywords from claim for fuzzy matching
        claim_words = set(re.findall(r'[a-zA-Z_]\w+', display.lower()))
        # Extract numbers from claim
        claim_numbers = set(re.findall(r'\d+', display))

        for cit in citations:
            matched = False
            cit_file = cit.get("file_path") or ""

            # 1. Direct file path overlap in claim text
            if cit_file and cit_file in display:
                matched = True

            # 2. File basename overlap
            if not matched and cit_file:
                basename = cit_file.rsplit("/", 1)[-1] if "/" in cit_file else cit_file
                basename_stem = basename.rsplit(".", 1)[0] if "." in basename else basename
                if basename and (basename in display or basename_stem.lower() in claim_words):
                    matched = True

            # 3. Keyword overlap: if claim mentions "config" and citation is config.py
            if not matched and cit_file:
                file_stem = cit_file.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
                if file_stem and file_stem in claim_words:
                    matched = True

            # 4. Numeric overlap with same file context
            if not matched and cit_file and claim_numbers:
                cit_line = str(cit.get("line_number") or "")
                # If the claim has a number and the citation is about a plausibly related file
                # This is a weaker match — only use if keyword context supports it
                cit_words = set(re.findall(r'[a-zA-Z_]\w+', cit_file.lower()))
                if claim_words & cit_words:
                    matched = True

            # 5. Verification command overlap
            if not matched and claim_vcmd and cit.get("verification_cmd"):
                if cit["verification_cmd"] in claim_vcmd or claim_vcmd in cit["verification_cmd"]:
                    matched = True

            if matched:
                drift_db.record_verification(conn, claim["id"], cit)
                break  # One verification per claim


def _suggest_prompt(claim_display: str, pattern: str | None) -> str:
    """Generate a ready-to-paste prompt for verifying an unchecked claim."""
    if pattern == "D":
        return "re-read the file and re-run the command — don't trust prior context"
    if pattern == "A":
        return "read the actual source code, not the docstring or comment"
    if pattern == "B":
        return "run the command to get the exact number, not a rounded estimate"
    if pattern == "C":
        return "re-verify from current file state, don't repeat prior claims"
    # Generic fallbacks based on content
    if re.search(r'`[^`]+\.[a-zA-Z]+`', claim_display):
        # Has a file reference
        m = re.search(r'`([^`]+\.[a-zA-Z]+)`', claim_display)
        if m:
            return f"read {m.group(1)} and cite the exact line"
    if re.search(r'\d+', claim_display):
        return "run the command to get the exact number and cite the output"
    return "cite file:line or command output to back this claim"


def _compute_alignment(conn, stats) -> list[str]:
    """Compute dual alignment score with actionable guidance.

    Claude score = evidence ratio (% claims with citations).
    Prompter score = correction ratio (% of drift drops that followed a turn).

    Returns list of lines: score line + guidance for each party.
    """
    try:
        total = stats["total"]
        evidenced = stats["evidenced"]
        if total == 0:
            return []

        # Claude score: verified claims get full credit, auto-evidenced 80%, uncited 0%
        vstats = drift_db.get_verification_stats(conn)
        verified = vstats["verified"]
        auto_ev = vstats["auto_evidenced"]
        weighted = (verified * 1.0 + auto_ev * 0.8) / max(total, 1)
        claude_score = min(int(100 * weighted), 100)
        uncited = total - evidenced - verified

        turns = conn.execute(
            "SELECT drift_score FROM turns ORDER BY turn"
        ).fetchall()
        drifts = [r[0] for r in turns]

        if len(drifts) < 2:
            prompter_score = 50
            missed_corrections = 0
        else:
            decreases = sum(1 for i in range(1, len(drifts)) if drifts[i] < drifts[i - 1])
            increases = sum(1 for i in range(1, len(drifts)) if drifts[i] > drifts[i - 1])
            total_changes = max(len(drifts) - 1, 1)
            prompter_score = int(100 * decreases / total_changes)
            missed_corrections = increases

        result = [f"alignment: Claude {claude_score} | Prompter {prompter_score}"]

        # Claude guidance
        if claude_score >= 100:
            result.append("  Claude: perfect — every claim has evidence")
        elif uncited <= 3:
            result.append(f"  Claude: cite {uncited} more claim{'s' if uncited != 1 else ''} to reach 100")
        else:
            result.append(f"  Claude: {uncited} uncited claims — use file:line refs and command output")

        # Prompter guidance
        if prompter_score >= 100:
            result.append("  Prompter: perfect — every drift spike was corrected")
        elif missed_corrections == 0 and len(drifts) < 3:
            result.append("  Prompter: too early to score — keep checking CHECKs when drift rises")
        elif missed_corrections > 0:
            result.append(f"  Prompter: {missed_corrections} uncorrected drift spike{'s' if missed_corrections != 1 else ''} — paste CHECK items back to close them")
        else:
            result.append("  Prompter: push back when drift rises — ask Claude to verify claims")

        return result

    except Exception:
        return []


def _dynamic_funnel(conn, stats, drift, pa, pb, pc, pd, unchecked, recently_compacted=False) -> list[str]:
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
        pd_weight = 6 if recently_compacted else 4
        weighted_severity = (pd * pd_weight + pa * 3 + pb * 2 + pc * 1) / max(total, 1)

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

        # Check if this is a re-entry (stop_hook_active) to prevent infinite loops
        stop_hook_active = hook_input.get("stop_hook_active", False)

        # Use context manager — auto-closes even on errors
        with drift_db.DriftDB() as conn:
            # Get current turn count
            stats = drift_db.get_session_drift(conn)
            turn = stats["last_turn"] + 1

            # ── AUTO-VERIFY: match citations in this response against pending claims ──
            verified_this_turn = []
            if response_text:
                citations = _extract_citations(response_text)
                if citations:
                    pending = drift_db.get_unverified_claims(conn, limit=20)
                    if pending:
                        _match_and_verify(conn, citations, pending)
                        # Check what was just verified
                        verified_this_turn = drift_db.get_recent_verifications(conn, limit=5)

            # Analyze and store — EVERY turn is recorded, no cold start.
            if response_text:
                claims = analyze_response(response_text)
                if claims:
                    drift_db.insert_claims(conn, turn, claims)
                total_turn = len(claims)
                evidenced_turn = sum(1 for c in claims if c["has_evidence"])
                drift_turn = (total_turn - evidenced_turn) / total_turn if total_turn > 0 else 0.0
                drift_db.insert_turn(conn, turn, total_turn, evidenced_turn, drift_turn)

            # Get session-level stats (after verification updates)
            stats = drift_db.get_session_drift(conn)
            vstats = drift_db.get_verification_stats(conn)

            # Get pending claims for display
            unchecked = drift_db.get_uncommitted_unverified(conn)[:5]

            # Check for recent compaction
            recently_compacted = drift_db.get_recent_compaction(conn)

            # Format output
            total = stats["total"]
            drift = stats["drift"]
            pa, pb, pc, pd = stats["pattern_a"], stats["pattern_b"], stats["pattern_c"], stats.get("pattern_d", 0)
            verified_count = vstats["verified"]
            pending_count = vstats["pending"]

            pattern_parts = []
            if pa: pattern_parts.append(f"A:{pa}")
            if pb: pattern_parts.append(f"B:{pb}")
            if pc: pattern_parts.append(f"C:{pc}")
            if pd: pattern_parts.append(f"D:{pd}")
            pattern_str = f" | {' '.join(pattern_parts)}" if pattern_parts else ""

            verified_str = f", {verified_count} verified" if verified_count else ""
            lines = [
                f"drift: {drift:.0%} ({stats['evidenced']}/{total} evidenced{verified_str}){pattern_str} | turn {stats['last_turn']}"
            ]

            if recently_compacted:
                lines.append("  !! CONTEXT COMPACTED — re-verify all prior claims. Pattern D weight raised.")

            # Show verified claims this turn
            if verified_this_turn:
                lines.append(f"  VERIFIED this turn ({len(verified_this_turn)}):")
                for v in verified_this_turn[:3]:
                    loc = ""
                    if v.get("file_path") and v.get("line_number"):
                        loc = f" @ {v['file_path']}:{v['line_number']}"
                        if v.get("byte_offset"):
                            loc += f" (byte {v['byte_offset']})"
                    lines.append(f"    ✓ \"{v['claim_display'][:50]}\"{loc}")

            # Show pending claims
            if unchecked:
                lines.append(f"  PENDING ({pending_count} unclosed):")
                for item in unchecked:
                    cmd = item.get("verification_cmd", "")
                    if cmd:
                        prompt = f"verify: {cmd}"
                    else:
                        prompt = _suggest_prompt(item.get("claim_display", ""), item.get("pattern"))
                    lines.append(f"    \"{item['claim_display']}\"")
                    lines.append(f"      → {prompt}")

            # ── DYNAMIC FUNNEL ──
            funnel_msg = _dynamic_funnel(conn, stats, drift, pa, pb, pc, pd, unchecked, recently_compacted)
            if funnel_msg:
                lines.append("")
                lines.extend(funnel_msg)

            # ── ALIGNMENT SCORE ──
            alignment_lines = _compute_alignment(conn, stats)
            if alignment_lines:
                lines.append("")
                lines.extend(alignment_lines)

        msg = "\n".join(lines)

        # ── DECISION: block or continue ──
        # If drift is high AND there are actionable pending claims AND this isn't
        # a re-entry (prevent infinite loops), force Claude to verify before stopping.
        should_block = (
            not stop_hook_active
            and drift > 0.50
            and pending_count > 0
            and any(item.get("verification_cmd") for item in unchecked)
        )

        if should_block:
            # Build verification instruction for Claude
            verify_cmds = []
            for item in unchecked[:3]:
                cmd = item.get("verification_cmd", "")
                if cmd:
                    verify_cmds.append(f"  {cmd}  # for: {item['claim_display'][:40]}")
            if verify_cmds:
                block_reason = (
                    f"[DRIFT] drift {drift:.0%} with {pending_count} unclosed claims. "
                    f"Run these commands and cite the results:\n"
                    + "\n".join(verify_cmds)
                )
                output = {
                    "decision": "block",
                    "reason": block_reason,
                }
            else:
                output = {
                    "systemMessage": f"[DRIFT]\n{msg}",
                    "continue": True,
                }
        else:
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
