#!/usr/bin/env bash
# Register one release with Datadog DORA (RC1-459). Called by release.yml after
# the tag installs and imports the way consumers pin it, so a deployment event
# means the release is usable, not merely that a tag exists.
#
# For a library the release is the deployment: consumers pin a tag, so the
# change reaches them when the tag is pushed. Lead time is the git range between
# consecutive release commits. Timestamps are nanoseconds. A failed POST fails
# the job on purpose: the tag is out either way, but an unrecorded release
# silently undercounts.
#
# Usage: report_dora_deployment.sh <service> <started_at_epoch_seconds> [commit_sha]
# The commit defaults to GITHUB_SHA; pass it explicitly for an annotated tag,
# whose own object id is not the commit's.
# Env:   DD_API_KEY, GITHUB_SERVER_URL, GITHUB_REPOSITORY
set -euo pipefail

service="$1"
started_at="$2"
commit_sha="${3:-$GITHUB_SHA}"
repo_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"

payload=$(printf '{"data":{"attributes":{"service":"%s","env":"prod","started_at":%s000000000,"finished_at":%s000000000,"git":{"commit_sha":"%s","repository_url":"%s"}}}}' \
  "$service" "$started_at" "$(date +%s)" "$commit_sha" "$repo_url")
code=$(curl -s -o dora-response.json -w '%{http_code}' -m 30 -X POST \
  -H "DD-API-KEY: $DD_API_KEY" -H "Content-Type: application/json" \
  -d "$payload" "https://api.datadoghq.com/api/v2/dora/deployment")
cat dora-response.json; echo
case "$code" in
  2*) echo "DORA deployment recorded for $service at $commit_sha (HTTP $code)";;
  *) echo "::error::DORA deployment POST for $service failed with HTTP $code — this release is out but unrecorded"; exit 1;;
esac
