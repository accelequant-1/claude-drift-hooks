#!/bin/bash
# Drift Metric Hook — runs after every Claude response
# Displays: drift: X% (N/M evidenced) | patterns: A:n B:n C:n | turn N
cd "$(dirname "$0")"
python3 drift-metric.py 2>>drift_errors.log || echo '{"systemMessage":"[DRIFT] hook error","continue":true}'
