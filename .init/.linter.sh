#!/bin/bash
cd /home/kavia/workspace/code-generation/online-chess-matchmaker-314945-314961/chess_backend
source venv/bin/activate
flake8 .
LINT_EXIT_CODE=$?
if [ $LINT_EXIT_CODE -ne 0 ]; then
  exit 1
fi

