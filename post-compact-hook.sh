#!/bin/bash
# PostCompact Hook — records compaction events in drift.db
# Spikes Pattern D sensitivity for the next 5 minutes.
cd "$(dirname "$0")"
python3 -c "
import sys, json
sys.path.insert(0, '.')
import drift_db
try:
    hook_input = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
except: hook_input = {}
trigger = hook_input.get('trigger', 'unknown')
with drift_db.DriftDB() as conn:
    drift_db.record_compaction(conn, trigger)
msg = '[DRIFT] Context compacted — Pattern D sensitivity raised. Re-verify all prior claims.'
print(json.dumps({'systemMessage': msg, 'continue': True}))
" 2>>drift_errors.log || echo '{"systemMessage":"[DRIFT] compaction hook error","continue":true}'
