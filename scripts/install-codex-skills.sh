#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
  echo "Usage: $0 [skills-directory]"
  exit 0
fi
if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [skills-directory]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${1:-$HOME/.agents/skills}"
mkdir -p "$target_dir"

for skill_dir in "$repo_root"/skills/*; do
  [[ -f "$skill_dir/SKILL.md" ]] || continue
  skill_name="$(basename "$skill_dir")"
  target="$target_dir/$skill_name"
  if [[ -L "$target" && "$(readlink "$target")" == "$skill_dir" ]]; then
    continue
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    echo "Skipped $skill_name because an existing skill uses that name." >&2
    continue
  fi
  ln -s "$skill_dir" "$target"
  echo "Linked $skill_name"
done
