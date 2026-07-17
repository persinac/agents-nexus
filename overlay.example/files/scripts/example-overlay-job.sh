#!/usr/bin/env bash
# Example overlay-provided script. In a real overlay this would be your private automation
# (e.g. an internal MR-labeler, a review queue, a vault sync). It lands at
# <core>/scripts/example-overlay-job.sh and is recorded in .git/info/exclude so the core
# tree stays clean. Replace this with your own.
set -euo pipefail
echo "example overlay job — replace me with real private automation"
