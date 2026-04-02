#!/usr/bin/env python3
"""
Drift Analysis Panel — full deep-dive for /drift command.

Reads from drift.db and produces a structured text panel with:
  1. Session drift score + component breakdown
  2. Drift velocity + streak analysis
  3. Top 5 unchecked claims with verification commands
  4. Pattern composition (A/B/C)
  5. Phase transition history
  6. Three strategic next-step suggestions
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    import drift_db
except Exception as _import_err:
    print(f"drift_analysis: could not import drift_db: {_import_err}")
    sys.exit(1)

DB_PATH = Path(__file__).parent / "drift.db"


def bar(fraction, width=20):
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def main():
    # Early exit if no DB file exists yet
    if not DB_PATH.exists():
        print("No drift data yet. The drift hook activates after your first response with factual claims.")
        sys.exit(0)

    try:
        with drift_db.DriftDB() as conn:

            # ── 1. Session drift score ──
            try:
                stats = drift_db.get_session_drift(conn)
                total = stats["total"]
                evidenced = stats["evidenced"]
                unverified = stats["unverified"]
                drift = stats["drift"]
                pa, pb, pc = stats["pattern_a"], stats["pattern_b"], stats["pattern_c"]

                print("╔══════════════════════════════════════════════════════════════╗")
                print("║               INSTRUCTION DRIFT ANALYSIS                    ║")
                print("╠══════════════════════════════════════════════════════════════╣")
                print(f"║  Session Drift:  {drift:5.1%}  {bar(drift)}  ║")
                print(f"║  Claims:         {evidenced}/{total} evidenced, {unverified} unchecked          ║")
                print(f"║  Turns:          {stats['last_turn']}                                          ║")
                print("╠══════════════════════════════════════════════════════════════╣")
            except Exception:
                print("╔══════════════════════════════════════════════════════════════╗")
                print("║               INSTRUCTION DRIFT ANALYSIS                    ║")
                print("╠══════════════════════════════════════════════════════════════╣")
                print("║  (section error: session drift)                             ║")
                print("╠══════════════════════════════════════════════════════════════╣")
                total = evidenced = unverified = pa = pb = pc = 0
                drift = 0.0
                stats = {"last_turn": 0, "pattern_a": 0, "pattern_b": 0, "pattern_c": 0}

            # ── 2. Velocity + streak ──
            try:
                turns = conn.execute(
                    "SELECT turn, drift_score, total_claims FROM turns ORDER BY turn DESC LIMIT 10"
                ).fetchall()

                velocities = [r[1] for r in turns]
                if len(velocities) >= 4:
                    recent = sum(velocities[:3]) / 3
                    prior = sum(velocities[3:6]) / max(len(velocities[3:6]), 1)
                    velocity = recent - prior
                else:
                    velocity = 0.0

                streak = 0
                for v in velocities:
                    if v > 0.50:
                        streak += 1
                    else:
                        break

                trend = "ACCELERATING ↗" if velocity > 0.05 else "DECELERATING ↘" if velocity < -0.05 else "STABLE →"
                print("║  VELOCITY & STREAK                                          ║")
                print(f"║  Trend:          {trend:15s} ({velocity:+.0%}/turn)           ║")
                print(f"║  Streak:         {streak} consecutive high-drift turns              ║")

                # Recent turn history
                print("║  Recent turns:   ", end="")
                for t in reversed(turns[:8]):
                    d = t[1]
                    if d > 0.60:
                        sym = "●"  # high drift
                    elif d > 0.30:
                        sym = "◐"  # medium
                    elif d > 0:
                        sym = "○"  # low
                    else:
                        sym = "·"  # clean
                    print(sym, end="")
                print("  (● >60% ◐ >30% ○ >0% · clean)     ║")
            except Exception:
                velocity = 0.0
                streak = 0
                print("║  VELOCITY & STREAK                                          ║")
                print("║  (section error: velocity)                                  ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 3. Top 5 unchecked claims ──
            try:
                unchecked = drift_db.get_uncommitted_unverified(conn)[:5]
                print("║  TOP UNCHECKED CLAIMS                                       ║")
                if unchecked:
                    for i, item in enumerate(unchecked):
                        display = item["claim_display"][:50]
                        cmd = item.get("verification_cmd", "")
                        print(f"║  {i+1}. \"{display}\"")
                        if cmd:
                            print(f"║     → {cmd[:55]}")
                else:
                    print("║  (none — all claims evidenced or committed)                 ║")
            except Exception:
                unchecked = []
                print("║  TOP UNCHECKED CLAIMS                                       ║")
                print("║  (section error: unchecked claims)                          ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 4. Pattern composition ──
            try:
                total_patterns = pa + pb + pc
                uncategorized = unverified - total_patterns

                print("║  PATTERN COMPOSITION                                        ║")
                if total_patterns > 0 or uncategorized > 0:
                    if pa > 0:
                        pct = pa / max(unverified, 1)
                        print(f"║  A docstring trust:    {pa:3d} ({pct:4.0%}) {bar(pct, 15)} ║")
                    if pb > 0:
                        pct = pb / max(unverified, 1)
                        print(f"║  B fabrication:        {pb:3d} ({pct:4.0%}) {bar(pct, 15)} ║")
                    if pc > 0:
                        pct = pc / max(unverified, 1)
                        print(f"║  C propagation:        {pc:3d} ({pct:4.0%}) {bar(pct, 15)} ║")
                    if uncategorized > 0:
                        pct = uncategorized / max(unverified, 1)
                        print(f"║  - uncategorized:      {uncategorized:3d} ({pct:4.0%}) {bar(pct, 15)} ║")
                else:
                    print("║  (no patterns detected — all claims clean)                  ║")
            except Exception:
                print("║  PATTERN COMPOSITION                                        ║")
                print("║  (section error: pattern composition)                       ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 5. Phase transitions ──
            try:
                all_turns = conn.execute(
                    "SELECT turn, drift_score, total_claims FROM turns ORDER BY turn"
                ).fetchall()

                transitions = []
                prev_drift = 0
                for turn_num, d, tc in all_turns:
                    if abs(d - prev_drift) > 0.20 and tc > 1:
                        direction = "↗" if d > prev_drift else "↘"
                        transitions.append((turn_num, prev_drift, d, direction, tc))
                    prev_drift = d

                print("║  PHASE TRANSITIONS (>20% drift change)                      ║")
                if transitions:
                    for turn_num, old_d, new_d, direction, tc in transitions[-6:]:
                        print(f"║  Turn {turn_num:3d}: {old_d:4.0%} {direction} {new_d:4.0%}  (delta {new_d-old_d:+.0%}, {tc} claims)     ║")
                else:
                    print("║  (no major transitions yet)                                 ║")
            except Exception:
                print("║  PHASE TRANSITIONS (>20% drift change)                      ║")
                print("║  (section error: phase transitions)                         ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 6. Strategic next steps ──
            try:
                steps = []

                # Step based on top unchecked claim
                if unchecked:
                    top = unchecked[0]
                    cmd = top.get("verification_cmd", "")
                    if cmd:
                        steps.append(f"Verify top claim: {cmd}")
                    else:
                        steps.append(f"Find evidence for: \"{top['claim_display'][:40]}\"")

                # Step based on dominant pattern
                if pa >= pb and pa >= pc and pa > 0:
                    steps.append("Pattern A active: re-read the actual source file for your last docstring-based claim, not the comment")
                elif pb >= pa and pb >= pc and pb > 0:
                    steps.append("Pattern B active: replace rounded numbers with exact values from wc -l, ls -lh, or grep output")
                elif pc > 0:
                    steps.append("Pattern C active: re-run the verification command for claims copied from earlier in the conversation")

                # Step based on velocity
                if velocity > 0.05:
                    steps.append("Drift accelerating: PAUSE. Re-read the user's last message. Do only what was asked.")
                elif streak >= 3:
                    steps.append(f"{streak}-turn streak: ask the user a clarifying question before your next implementation step")
                elif drift > 0.30:
                    steps.append("Drift > 30%: next response should cite file:line for every number and file reference")
                else:
                    steps.append("Drift manageable: maintain evidence citations to stay clean")

                # Pad to exactly 3
                while len(steps) < 3:
                    steps.append("Continue citing evidence with every factual claim")
                steps = steps[:3]

                print("║  STRATEGIC NEXT STEPS                                       ║")
                for i, step in enumerate(steps):
                    # Wrap long lines
                    if len(step) > 55:
                        print(f"║  {i+1}. {step[:55]}")
                        print(f"║     {step[55:]}")
                    else:
                        print(f"║  {i+1}. {step}")
            except Exception:
                print("║  STRATEGIC NEXT STEPS                                       ║")
                print("║  (section error: next steps)                                ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 7. Alignment score ──
            try:
                claude_score = int(100 * evidenced / max(total, 1))

                turns_data = conn.execute(
                    "SELECT drift_score FROM turns ORDER BY turn"
                ).fetchall()
                drifts = [r[0] for r in turns_data]
                if len(drifts) >= 2:
                    decreases = sum(1 for i in range(1, len(drifts)) if drifts[i] < drifts[i - 1])
                    prompter_score = int(100 * decreases / max(len(drifts) - 1, 1))
                else:
                    prompter_score = 50

                uncited = total - evidenced
                increases = sum(1 for i in range(1, len(drifts)) if drifts[i] > drifts[i - 1]) if len(drifts) >= 2 else 0

                c_bar = bar(claude_score / 100, 10)
                p_bar = bar(prompter_score / 100, 10)
                print("║  ALIGNMENT                                                  ║")
                print(f"║  Claude:   {claude_score:3d}/100  {c_bar}  (evidence ratio)       ║")
                print(f"║  Prompter: {prompter_score:3d}/100  {p_bar}  (correction ratio)     ║")
                print("║                                                              ║")
                # Claude guidance
                if claude_score >= 100:
                    print("║  Claude:   perfect — every claim has evidence                ║")
                elif uncited <= 3:
                    print(f"║  Claude:   cite {uncited} more claim{'s' if uncited != 1 else ' '} to reach 100                    ║")
                else:
                    print(f"║  Claude:   {uncited} uncited — use file:line and cmd output       ║")
                # Prompter guidance
                if prompter_score >= 100:
                    print("║  Prompter: perfect — every drift spike was corrected         ║")
                elif increases > 0:
                    print(f"║  Prompter: {increases} uncorrected spike{'s' if increases != 1 else ' '} — paste CHECKs back      ║")
                else:
                    print("║  Prompter: push back when drift rises — ask to verify        ║")
            except Exception:
                print("║  ALIGNMENT                                                  ║")
                print("║  (section error: alignment)                                 ║")
            print("╠══════════════════════════════════════════════════════════════╣")

            # ── 8. Verification ledger ──
            try:
                recent_v = drift_db.get_recent_verifications(conn, limit=10)
                pending_v = drift_db.get_unverified_claims(conn, limit=5)
                vstats = drift_db.get_verification_stats(conn)

                print("║  VERIFICATION LEDGER                                        ║")
                print(f"║  {vstats['verified']} verified, {vstats['pending']} pending, {vstats['auto_evidenced']} auto-evidenced     ║")

                if recent_v:
                    for v in recent_v[:5]:
                        loc = ""
                        if v.get("file_path") and v.get("line_number"):
                            loc = f" @ {v['file_path']}:{v['line_number']}"
                        label = f"✓ turn {v['turn']}: \"{v['claim_display'][:30]}\"{loc}"
                        if len(label) > 56:
                            label = label[:56]
                        print(f"║  {label}")

                if pending_v:
                    for p in pending_v[:3]:
                        label = f"✗ turn {p['turn']}: \"{p['claim_display'][:35]}\" — PENDING"
                        if len(label) > 56:
                            label = label[:56]
                        print(f"║  {label}")

                if not recent_v and not pending_v:
                    print("║  (no verification events yet)                               ║")
            except Exception:
                print("║  VERIFICATION LEDGER                                        ║")
                print("║  (section error: verification ledger)                       ║")
            print("╚══════════════════════════════════════════════════════════════╝")

    except Exception as exc:
        print(f"drift_analysis: could not read drift data ({exc})")
        print("Try removing .claude/hooks/drift.db if it is corrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
