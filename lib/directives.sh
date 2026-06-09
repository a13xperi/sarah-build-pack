#!/bin/bash
# Directives — inject operator strategic directives into the shared briefing.
#
# Sourced + backgrounded by hooks/auto-register.sh:
#   source "$HOOK_DIR/../lib/directives.sh" 2>/dev/null \
#     && inject_directives_to_briefing "$REPO" &
#
# Best-effort, fail-silent, fast (<50ms target). If no directives are
# configured this is a clean no-op. Directives are operator-authored and live
# in (first match wins):
#   1. ~/.battlestation-directives        — plain text, one directive per line.
#      Optional per-repo scoping with a leading "repo:" prefix, e.g.
#        atlas: keep the crafting flow snappy — no blocking calls on render
#        *: never commit secrets
#   2. drain-prevention.json "directives" — { "<repo>": ["..."], "*": ["..."] }
#
# Output is appended to /tmp/battlestation/briefing.md (the same file
# hooks/generate-briefing.py writes), under a "## Directives" section.
#
# Requires (already sourced by caller): config.sh, log.sh, atomic.sh

BRIEFING_FILE="/tmp/battlestation/briefing.md"
DIRECTIVES_TXT="${HOME}/.battlestation-directives"
DIRECTIVES_JSON="${HOME}/battlestation/drain-prevention.json"

# Collect directives applicable to a repo ("*" = all repos).
# Echoes one directive per line; empty output means "nothing to inject".
_directives_for_repo() {
  local repo="$1"

  if [ -f "$DIRECTIVES_TXT" ]; then
    # Lines are "repo: text", "*: text", or bare "text" (applies to all).
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;                          # blank / comment
        "${repo}:"*) echo "${line#"${repo}:"}" | sed 's/^ *//' ;;
        '*:'*)       echo "${line#'*:'}" | sed 's/^ *//' ;;
        *:*)         : ;;                              # scoped to a different repo
        *)           echo "$line" ;;                   # bare = all repos
      esac
    done < "$DIRECTIVES_TXT"
    return
  fi

  if [ -f "$DIRECTIVES_JSON" ] && command -v jq >/dev/null 2>&1; then
    jq -r --arg r "$repo" \
      '(.directives[$r] // []) + (.directives["*"] // []) | .[]?' \
      "$DIRECTIVES_JSON" 2>/dev/null
  fi
}

# inject_directives_to_briefing "<repo>"
inject_directives_to_briefing() {
  local repo="${1:-unknown}"

  # Throttle: refresh at most once per 60s (this runs on every registration).
  local stamp="/tmp/battlestation/${PPID}/directives-stamp"
  local now last
  now=$(date +%s)
  last=$(cat "$stamp" 2>/dev/null || echo 0)
  [ $(( now - last )) -lt 60 ] && return 0

  local directives
  directives=$(_directives_for_repo "$repo")
  [ -z "$directives" ] && return 0   # nothing configured — clean no-op

  echo "$now" > "$stamp" 2>/dev/null

  # Build a "## Directives" section and append it to the briefing. We rewrite
  # the file dropping any prior Directives section so it doesn't accumulate.
  local existing="" section
  [ -f "$BRIEFING_FILE" ] && existing=$(awk '
    /^## Directives$/ { skip=1; next }
    /^## / { if (skip) skip=0 }
    !skip { print }
  ' "$BRIEFING_FILE" 2>/dev/null)

  section=$(printf '## Directives (%s)\n' "$repo"
            printf '%s\n' "$directives" | sed 's/^/- /')

  mkdir -p "$(dirname "$BRIEFING_FILE")" 2>/dev/null
  if type atomic_write &>/dev/null; then
    atomic_write "$BRIEFING_FILE" "$(printf '%s\n\n%s\n' "$existing" "$section")"
  else
    printf '%s\n\n%s\n' "$existing" "$section" > "$BRIEFING_FILE" 2>/dev/null
  fi

  bs_log "INFO" "directives" "injected $(printf '%s\n' "$directives" | grep -c .) directive(s) for ${repo}" 2>/dev/null || true
}
