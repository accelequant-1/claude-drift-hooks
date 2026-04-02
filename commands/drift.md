# /drift — Instruction Drift Analysis Panel

When the user invokes `/drift`, run the drift analysis script and present the results as a structured panel. This is the on-demand deep-dive companion to the Stop hook one-liner.

## What to do

Run this exact command and present the output:

```bash
cd $PROJECT_DIR && python3 .claude/hooks/drift_analysis.py
```

Then present the output verbatim — do not summarize, rewrite, or editorialize. The script produces a formatted panel with all sections. Your only job is to run it and show the result.

After the panel, add a Key Insights box that interprets the data for this specific session. Use this format:

```
★ Insight ─────────────────────────────────────
- [Interpret the dominant pattern and what's causing it]
- [Note velocity trend and what it means for the session]
- [Actionable takeaway specific to the current drift state]
─────────────────────────────────────────────────
```

The insights should be specific to the numbers in the panel, not generic advice. Examples:
- If Pattern D dominates: "Post-compaction stale claims are driving drift — context was likely cleared or compacted recently. Re-read files before citing prior work."
- If drift is improving: "Drift dropped from 80% to 40% over the last 3 turns — evidence citations are working. Keep it up."
- If Pattern B spikes: "3 new narrative fabrication claims this turn — switch from 'roughly' to exact values from command output."
- If drift is 0%: "Clean session. Every claim has evidence."

After the insights, execute the 3 suggested next steps from the output. Each step is a specific verification command. Run them and report results with file:line citations.

## When this fails

If drift.db doesn't exist yet (new session, no claims tracked), say: "No drift data yet. The drift hook activates after your first response with factual claims."

If the script errors, show the error and suggest: "Try `rm .claude/hooks/drift.db` to reset, then make a response to seed the DB."
