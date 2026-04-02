# /drift — Show drift analysis

Run the drift analysis script and show the output.

```bash
cd $PROJECT_DIR && python3 .claude/hooks/drift_analysis.py
```

Show the output verbatim. Then run the 3 suggested next steps from the output and report results with file:line citations.

If drift.db doesn't exist: "No drift data yet — the hook activates after responses with factual claims."

If it errors: show the error, suggest `rm .claude/hooks/drift.db` to reset.
