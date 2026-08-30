#!/usr/bin/env bash
# Steady traffic, so the graphs have something in them. Ctrl-C to stop.
set -u
HOST="${1:-http://localhost:8000}"
while true; do
  curl -s -o /dev/null "$HOST/orders/$((RANDOM % 4 + 1))"   # 4 -> a 404
  curl -s -o /dev/null "$HOST/health"
  [ $((RANDOM % 8)) -eq 0 ] && curl -s -o /dev/null "$HOST/boom"
  sleep 1
done
