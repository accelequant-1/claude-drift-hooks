#!/bin/bash
# Git Commit Gate — PreToolUse hook for Claude Code
# Warns when drift > 30%, requires --drift-override in commit message.
# Blocks if drift.db is staged (security: never commit the DB).

set -e
cd "$(dirname "$0")"

DB="drift.db"
ERRLOG="drift_errors.log"

# Safety: block if drift.db is in the staging area
if git -C ../.. diff --cached --name-only 2>>"$ERRLOG" | grep -q "drift\.db"; then
    python3 -c "import json; print(json.dumps({'decision':'block','reason':'BLOCKED: drift.db is staged for commit. Run: git reset HEAD .claude/hooks/drift.db'}))"
    exit 0
fi

# Read the command being executed from stdin
INPUT=$(cat)

# Validate INPUT is non-empty before JSON parsing
if [ -z "$INPUT" ]; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>>"$ERRLOG" || echo "")

# Validate COMMAND is non-empty before grep
if [ -z "$COMMAND" ]; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

# Only gate git commit commands
if ! echo "$COMMAND" | grep -q "git commit"; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

# Check if DB exists
if [ ! -f "$DB" ]; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

# Query drift
RESULT=$(python3 -c "
import drift_db, json
conn = drift_db.open_db()
stats = drift_db.get_session_drift(conn)
unchecked = drift_db.get_uncommitted_unverified(conn)
conn.close()
print(json.dumps({'drift': stats['drift'], 'total': stats['total'], 'unverified': stats['unverified'], 'unchecked': [{'display': c['claim_display'], 'cmd': c.get('verification_cmd','')} for c in unchecked[:5]]}))
" 2>>"$ERRLOG" || python3 -c "import json; print(json.dumps({'drift':0,'total':0,'unverified':0,'unchecked':[]}))")

DRIFT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['drift'])" 2>>"$ERRLOG" || echo "0")
UNVERIFIED=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['unverified'])" 2>>"$ERRLOG" || echo "0")

# Check if --drift-override is in the commit message
HAS_OVERRIDE=0
if echo "$COMMAND" | grep -q "drift-override"; then
    HAS_OVERRIDE=1
fi

# Gate logic
if python3 -c "import sys; sys.exit(0 if $DRIFT > 0.30 else 1)" 2>>"$ERRLOG"; then
    if [ "$HAS_OVERRIDE" -eq 1 ]; then
        # Allow with override — log it
        python3 -c "
import drift_db, subprocess, json
try:
    conn = drift_db.open_db()
    sha = subprocess.run(['git','rev-parse','--short','HEAD'], capture_output=True, text=True).stdout.strip()
    drift_db.mark_committed(conn, sha or 'pending', $DRIFT, override=True)
    conn.close()
except Exception as e:
    import sys
    print(f'[drift] mark_committed error: {e}', file=sys.stderr)
" 2>>"$ERRLOG"
        python3 -c "import json; print(json.dumps({'decision':'allow','reason':'[DRIFT] Override accepted. Drift $DRIFT logged as override commit.'}))"
    else
        # Build CHECK list
        CHECKS=$(echo "$RESULT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
lines = []
for c in data['unchecked'][:5]:
    cmd = ' -> {}'.format(c['cmd']) if c['cmd'] else ''
    lines.append('  CHECK: \"{}\"{}'.format(c['display'], cmd))
print('\n'.join(lines))
" 2>>"$ERRLOG" || echo "  (run drift-metric.py for details)")

        REASON="DRIFT WARNING: ${DRIFT} drift (${UNVERIFIED} unverified claims).
Add --drift-override to commit message to proceed, or verify these claims first:
${CHECKS}"
        python3 -c "
import sys, json
reason = sys.argv[1]
print(json.dumps({'decision':'block','reason':reason}))
" "$REASON" 2>>"$ERRLOG" || python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    fi
elif python3 -c "import sys; sys.exit(0 if $DRIFT > 0.10 else 1)" 2>>"$ERRLOG"; then
    python3 -c "import json; print(json.dumps({'decision':'allow','reason':'[DRIFT] Warning: drift $DRIFT ($UNVERIFIED unchecked claims). Consider verifying before push.'}))" 2>>"$ERRLOG" || python3 -c "import json; print(json.dumps({'decision':'allow'}))"
else
    # Low drift — allow silently, mark committed
    python3 -c "
import drift_db, subprocess, json
try:
    conn = drift_db.open_db()
    sha = subprocess.run(['git','rev-parse','--short','HEAD'], capture_output=True, text=True).stdout.strip()
    drift_db.mark_committed(conn, sha or 'clean', $DRIFT, override=False)
    conn.close()
except Exception as e:
    import sys
    print(f'[drift] mark_committed error: {e}', file=sys.stderr)
" 2>>"$ERRLOG"
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
fi
