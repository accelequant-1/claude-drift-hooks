# /drift — Instruction Drift Analysis Panel

When the user invokes `/drift`, run the drift analysis script and present the results as a structured panel. This is the on-demand deep-dive companion to the Stop hook one-liner.

## What to do

Run this exact command and present the output:

```bash
cd $PROJECT_DIR && python3 .claude/hooks/drift_analysis.py
```

Then present the output verbatim — do not summarize, rewrite, or editorialize. The script produces a formatted panel with all sections. Your only job is to run it and show the result.

After showing the panel, execute the 3 suggested next steps from the output. Each step is a specific verification command. Run them and report results with file:line citations.

## When this fails

If drift.db doesn't exist yet (new session, no claims tracked), say: "No drift data yet. The drift hook activates after your first response with factual claims."

If the script errors, show the error and suggest: "Try `rm .claude/hooks/drift.db` to reset, then make a response to seed the DB."
