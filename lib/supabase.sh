#!/bin/bash
# Supabase REST API wrappers
# Requires: SUPA_URL, SUPA_KEY from config.sh

_supa_headers() {
  echo -H "apikey: ${SUPA_KEY}" -H "Authorization: Bearer ${SUPA_KEY}"
}

# supa_get "table" "query_params"
# Returns: response body on stdout, HTTP code on fd 3
supa_get() {
  local table="$1" params="$2"
  curl -s --max-time 3 \
    "${SUPA_URL}/rest/v1/${table}?${params}" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}" 2>/dev/null
}

# supa_patch "table" "filter" "json_body"
supa_patch() {
  local table="$1" filter="$2" body="$3"
  curl -s --max-time 3 -o /dev/null -w "%{http_code}" -X PATCH \
    "${SUPA_URL}/rest/v1/${table}?${filter}" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d "$body" 2>/dev/null
}

# supa_post "table" "json_body"
supa_post() {
  local table="$1" body="$2"
  curl -s --max-time 3 -o /dev/null -w "%{http_code}" -X POST \
    "${SUPA_URL}/rest/v1/${table}" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d "$body" 2>/dev/null
}

# supa_delete "table" "filter"
supa_delete() {
  local table="$1" filter="$2"
  curl -s --max-time 3 -o /dev/null -w "%{http_code}" -X DELETE \
    "${SUPA_URL}/rest/v1/${table}?${filter}" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}" \
    -H "Prefer: return=minimal" 2>/dev/null
}

# supa_exists "table" "filter" -> 0 if exists, 1 if not
supa_exists() {
  local table="$1" filter="$2"
  local result
  result=$(supa_get "$table" "${filter}&select=id&limit=1")
  [ "$result" != "[]" ] && [ -n "$result" ]
}
