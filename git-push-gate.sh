#!/bin/bash
# Git Push Gate — PreToolUse hook for Claude Code
# Blocks push when session drift > 15%. Suggests git revert for unverified commits.

set -e
cd "$(dirname "$0")"

DB="drift.db"
ERRLOG="drift_errors.log"

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

# Only gate git push commands
if ! echo "$COMMAND" | grep -q "git push"; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

# Check if DB exists
if [ ! -f "$DB" ]; then
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
    exit 0
fi

# Query session drift and unverified commits
RESULT=$(python3 -c "
import drift_db, json
conn = drift_db.open_db()
stats = drift_db.get_session_drift(conn)
commits = drift_db.get_unverified_commits(conn)
conn.close()
print(json.dumps({
    'drift': stats['drift'],
    'total': stats['total'],
    'unverified': stats['unverified'],
    'commits': [{'sha': c['sha'], 'drift': c['drift_score'], 'override': c['override'], 'unverified': c['unverified_claims']} for c in commits]
}))
" 2>>"$ERRLOG" || python3 -c "import json; print(json.dumps({'drift':0,'total':0,'unverified':0,'commits':[]}))")

DRIFT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['drift'])" 2>>"$ERRLOG" || echo "0")

# Gate logic: block if drift > 15%
if python3 -c "import sys; sys.exit(0 if $DRIFT > 0.15 else 1)" 2>>"$ERRLOG"; then
    # Build revert suggestions
    REVERT_MSG=$(echo "$RESULT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
lines = ['PUSH BLOCKED — session drift {:.0%} ({}/{} claims uncited)'.format(data['drift'], data['unverified'], data['total'])]
lines.append('')
for c in data['commits']:
    flag = ' (override)' if c['override'] else ''
    lines.append('  Commit {}: drift {:.0%}, {} unverified{}'.format(c['sha'], c['drift'], c['unverified'], flag))
    lines.append('    git revert {}'.format(c['sha']))
lines.append('')
lines.append('Verify claims first, then recommit and push.')
print('\n'.join(lines))
" 2>>"$ERRLOG" || echo "Push blocked — drift too high. Verify claims before pushing.")

    python3 -c "
import sys, json
reason = sys.argv[1]
print(json.dumps({'decision':'block','reason':reason}))
" "$REVERT_MSG" 2>>"$ERRLOG" || python3 -c "import json; print(json.dumps({'decision':'allow'}))"
else
    python3 -c "import json; print(json.dumps({'decision':'allow'}))"
fi
