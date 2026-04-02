#!/bin/bash
# Install drift hooks into the current project's .claude/ directory.
# Run from any project root: bash install.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET=".claude/hooks"
COMMANDS=".claude/commands"
SETTINGS=".claude/settings.local.json"

echo "Installing drift hooks into $(pwd)/.claude/"

# Create directories
mkdir -p "$TARGET"
mkdir -p "$COMMANDS"

# Copy hook files
for f in drift_db.py drift-metric.py drift-metric.sh drift_analysis.py \
         git-commit-gate.sh git-push-gate.sh config.py; do
    cp "$SCRIPT_DIR/$f" "$TARGET/$f"
done

# Copy /drift command
cp "$SCRIPT_DIR/commands/drift.md" "$COMMANDS/drift.md"

# Make scripts executable
chmod +x "$TARGET/drift-metric.sh" "$TARGET/git-commit-gate.sh" "$TARGET/git-push-gate.sh"

# Add to .gitignore
GITIGNORE=".claude/.gitignore"
if [ ! -f "$GITIGNORE" ] || ! grep -q "hooks/\*.db" "$GITIGNORE" 2>/dev/null; then
    echo "" >> "$GITIGNORE"
    echo "# Drift metric (local-only, never commit)" >> "$GITIGNORE"
    echo "hooks/*.db" >> "$GITIGNORE"
    echo "hooks/drift_state.json" >> "$GITIGNORE"
    echo "hooks/drift_errors.log" >> "$GITIGNORE"
fi

# Merge hooks into settings.local.json
if [ -f "$SETTINGS" ]; then
    # Settings exist — merge hooks section
    python3 -c "
import json, sys

with open('$SETTINGS') as f:
    existing = json.load(f)

with open('$SCRIPT_DIR/settings-snippet.json') as f:
    snippet = json.load(f)

# Merge hooks
if 'hooks' not in existing:
    existing['hooks'] = {}

for event, handlers in snippet['hooks'].items():
    if event not in existing['hooks']:
        existing['hooks'][event] = handlers
    else:
        # Append handlers that don't already exist
        existing_cmds = {h['hooks'][0]['command'] for handler in existing['hooks'][event] for h in [handler]}
        for handler in handlers:
            cmd = handler['hooks'][0]['command']
            if cmd not in existing_cmds:
                existing['hooks'][event].append(handler)

with open('$SETTINGS', 'w') as f:
    json.dump(existing, f, indent=2)

print('Merged hooks into $SETTINGS')
" || echo "WARNING: Could not merge settings. Copy settings-snippet.json manually."
else
    # No settings — create from snippet + empty permissions
    python3 -c "
import json
with open('$SCRIPT_DIR/settings-snippet.json') as f:
    snippet = json.load(f)
snippet['permissions'] = {'allow': []}
with open('$SETTINGS', 'w') as f:
    json.dump(snippet, f, indent=2)
print('Created $SETTINGS')
"
fi

echo ""
echo "Done. Drift hooks installed."
echo ""
echo "  /drift        — full analysis panel (on demand)"
echo "  Stop hook     — drift score after every response (automatic)"
echo "  Commit gate   — warns at ${DRIFT_COMMIT_THRESHOLD:-30%} drift, requires --drift-override"
echo "  Push gate     — blocks at ${DRIFT_PUSH_THRESHOLD:-15%} drift, suggests git revert"
echo ""
echo "Configure thresholds:"
echo "  export DRIFT_COMMIT_THRESHOLD=0.20  # stricter commit gate"
echo "  export DRIFT_PUSH_THRESHOLD=0.10    # stricter push gate"
echo ""
echo "Run tests: python3 .claude/hooks/test_drift_system.py"
