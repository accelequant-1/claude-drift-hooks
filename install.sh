#!/bin/bash
# Install drift hooks into the current project's .claude/ directory.
# Run from any project root: bash install.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(pwd)"
TARGET="$PROJECT_ROOT/.claude/hooks"
COMMANDS="$PROJECT_ROOT/.claude/commands"
SETTINGS="$PROJECT_ROOT/.claude/settings.local.json"

echo "Installing drift hooks into $PROJECT_ROOT/.claude/"
echo "  Using absolute paths for hook commands"

# Create directories
mkdir -p "$TARGET"
mkdir -p "$COMMANDS"

# Copy hook files
for f in drift_db.py drift-metric.py drift-metric.sh drift_analysis.py \
         git-commit-gate.sh git-push-gate.sh post-compact-hook.sh config.py; do
    cp "$SCRIPT_DIR/$f" "$TARGET/$f"
done

# Copy /drift command
cp "$SCRIPT_DIR/commands/drift.md" "$COMMANDS/drift.md"

# Make scripts executable
chmod +x "$TARGET/drift-metric.sh" "$TARGET/git-commit-gate.sh" "$TARGET/git-push-gate.sh"

# Add to .gitignore
GITIGNORE="$PROJECT_ROOT/.claude/.gitignore"
if [ ! -f "$GITIGNORE" ] || ! grep -q "hooks/\*.db" "$GITIGNORE" 2>/dev/null; then
    echo "" >> "$GITIGNORE"
    echo "# Drift metric (local-only, never commit)" >> "$GITIGNORE"
    echo "hooks/*.db" >> "$GITIGNORE"
    echo "hooks/drift_state.json" >> "$GITIGNORE"
    echo "hooks/drift_errors.log" >> "$GITIGNORE"
    echo "hooks/verification_ledger.jsonl" >> "$GITIGNORE"
    echo "hooks/__pycache__/" >> "$GITIGNORE"
    echo "settings.local.json" >> "$GITIGNORE"
fi

# Generate settings with absolute paths
# Claude Code hooks can run from ANY directory, so paths must be absolute
HOOK_DIR="$TARGET"

if [ -f "$SETTINGS" ]; then
    # Settings exist — merge hooks section with absolute paths
    python3 -c "
import json

with open('$SETTINGS') as f:
    existing = json.load(f)

hooks_config = {
    'Stop': [
        {
            'matcher': '',
            'hooks': [
                {
                    'type': 'command',
                    'command': 'bash $HOOK_DIR/drift-metric.sh',
                    'timeout': 10
                }
            ]
        }
    ],
    'PreToolUse': [
        {
            'matcher': 'Bash(git commit*)',
            'hooks': [
                {
                    'type': 'command',
                    'command': 'bash $HOOK_DIR/git-commit-gate.sh',
                    'timeout': 5
                }
            ]
        },
        {
            'matcher': 'Bash(git push*)',
            'hooks': [
                {
                    'type': 'command',
                    'command': 'bash $HOOK_DIR/git-push-gate.sh',
                    'timeout': 5
                }
            ]
        }
    ],
    'PostCompact': [
        {
            'matcher': '',
            'hooks': [
                {
                    'type': 'command',
                    'command': 'bash $HOOK_DIR/post-compact-hook.sh',
                    'timeout': 5
                }
            ]
        }
    ]
}

if 'hooks' not in existing:
    existing['hooks'] = {}

for event, handlers in hooks_config.items():
    if event not in existing['hooks']:
        existing['hooks'][event] = handlers
    else:
        existing_cmds = set()
        for handler in existing['hooks'][event]:
            for h in handler.get('hooks', []):
                existing_cmds.add(h.get('command', ''))
        for handler in handlers:
            cmd = handler['hooks'][0]['command']
            # Also match if a relative-path version already exists
            basename = cmd.split('/')[-1]
            already = any(basename in c for c in existing_cmds)
            if not already:
                existing['hooks'][event].append(handler)
            else:
                # Replace existing relative-path version with absolute
                for i, eh in enumerate(existing['hooks'][event]):
                    for h in eh.get('hooks', []):
                        if basename in h.get('command', '') and h['command'] != cmd:
                            h['command'] = cmd

with open('$SETTINGS', 'w') as f:
    json.dump(existing, f, indent=2)

print('Merged hooks into $SETTINGS (absolute paths)')
" || echo "WARNING: Could not merge settings automatically."
else
    # No settings — create fresh with absolute paths
    python3 -c "
import json

settings = {
    'permissions': {'allow': []},
    'hooks': {
        'Stop': [
            {
                'matcher': '',
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'bash $HOOK_DIR/drift-metric.sh',
                        'timeout': 10
                    }
                ]
            }
        ],
        'PreToolUse': [
            {
                'matcher': 'Bash(git commit*)',
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'bash $HOOK_DIR/git-commit-gate.sh',
                        'timeout': 5
                    }
                ]
            },
            {
                'matcher': 'Bash(git push*)',
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'bash $HOOK_DIR/git-push-gate.sh',
                        'timeout': 5
                    }
                ]
            }
        ],
        'PostCompact': [
            {
                'matcher': '',
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'bash $HOOK_DIR/post-compact-hook.sh',
                        'timeout': 5
                    }
                ]
            }
        ]
    }
}

with open('$SETTINGS', 'w') as f:
    json.dump(settings, f, indent=2)
print('Created $SETTINGS (absolute paths)')
"
fi

echo ""
echo "Done. Drift hooks installed."
echo ""
echo "  Hook scripts:  $HOOK_DIR/"
echo "  Settings:      $SETTINGS"
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
echo "Run tests: python3 $HOOK_DIR/test_drift_system.py"
