#!/bin/zsh
set -e
cd -- "${0:A:h}"
task_python="${DRAGON_PYTHON:-}"
if [[ -z "$task_python" && -x "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" ]]; then
  task_python="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
fi
if [[ -z "$task_python" ]]; then
  task_python="python3"
fi
if [[ $# -eq 0 ]]; then
  "$task_python" -m dragon_agent demo --open
else
  "$task_python" -m dragon_agent "$@"
fi
