#!/usr/bin/env bash
#
# Lists a patient's conditions from a FHIR server, with the source reference for
# each one -- the same two things the packet endpoint reports, straight from the
# record. Useful for spot-checking that a citation points at something real.
#
# Usage:
#   scripts/show_conditions.sh <patient-id> [fhir-base-url]
#
# Examples:
#   scripts/show_conditions.sh 632b7c1c-9545-4aa7-9fd5-07683139ef13
#   scripts/show_conditions.sh 632b7c1c-9545-4aa7-9fd5-07683139ef13 http://localhost:9090/fhir
#   BASE=http://localhost:9090/fhir scripts/show_conditions.sh 632b7c1c-...

set -euo pipefail

PATIENT_ID="${1:-}"
# Precedence: second argument, then a BASE in the environment, then the local default.
BASE="${2:-${BASE:-http://localhost:8080/fhir}}"

if [[ -z "$PATIENT_ID" ]]; then
    # Print this file's header comment as the usage text, stopping at the first
    # line that is not a comment.
    awk 'NR>2 && /^#/ { sub(/^# ?/, ""); print; next } NR>2 { exit }' "$0"
    exit 1
fi

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 1; }

# Fail with a clear message rather than an empty result set if the patient is not
# there -- an empty condition list and a missing patient look identical otherwise.
status=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/Patient/$PATIENT_ID")
if [[ "$status" != "200" ]]; then
    echo "Patient/$PATIENT_ID not found at $BASE (HTTP $status)" >&2
    exit 1
fi

name=$(curl -s "$BASE/Patient/$PATIENT_ID" |
    jq -r '"\(.name[0].given[0]) \(.name[0].family), born \(.birthDate), \(.gender)"')

echo "$name"
echo "Patient/$PATIENT_ID  at  $BASE"
echo

# _count=200 asks for everything in one page; real pagination belongs in the
# service, not in a spot-check script.
curl -s -G "$BASE/Condition" \
    --data-urlencode "patient=$PATIENT_ID" \
    --data-urlencode "_count=200" \
    --data-urlencode "_sort=-onset-date" |
    jq -r '
      if (.total // 0) == 0 then
        "  no conditions on file"
      else
        (.entry[].resource
         | "  \(.onsetDateTime[0:10] // .recordedDate[0:10] // "          ")" +
           "  \(.clinicalStatus.coding[0].code // "?" | (. + "        ")[0:9])" +
           "  \(.code.coding[0].display // .code.text // "?" | (. + "                                        ")[0:40])" +
           "  Condition/\(.id)"),
        "",
        "  \(.total) condition(s): " +
          ([.entry[].resource.clinicalStatus.coding[0].code]
           | group_by(.) | map("\(length) \(.[0])") | join(", "))
      end'
