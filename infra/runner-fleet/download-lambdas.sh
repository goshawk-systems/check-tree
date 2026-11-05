#!/usr/bin/env bash
# Downloads the three Lambda zips (webhook, runners, runner-binaries-syncer) that a plain `terraform init` does not fetch for
# github-aws-runners/terraform-aws-github-runner v7.11.0, from that release's assets, and checks each against a pinned sha256.
# Run once from this directory before the first `terraform plan` (infra/README.md, step 3):  ./download-lambdas.sh
# Release: https://github.com/github-aws-runners/terraform-aws-github-runner/releases/tag/v7.11.0
set -euo pipefail

MODULE_VERSION="v7.11.0"
BASE_URL="https://github.com/github-aws-runners/terraform-aws-github-runner/releases/download/${MODULE_VERSION}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDAS_DIR="${SCRIPT_DIR}/lambdas"

# sha256 of each v7.11.0 release asset, recorded by this change (verified 2026-09-29) so a later
# run can detect a swapped or corrupted download instead of silently accepting it. Plain
# "name sha256" pairs, not an associative array -- macOS ships bash 3.2, which has none.
LAMBDA_CHECKSUMS="
webhook 7dbbb262757fdcc4be8928250a614fda4deb85cbd02e50bbad0f6538426f3bd3
runners 7b262395ed7683b7940f2efbd9e234a8712fb33ba71e896db356204aeb0a3f53
runner-binaries-syncer d56b60477be52fcd94c3b35f39d1a6770420661d539eda2ea3ae920e5c5f1ea2
"

mkdir -p "${LAMBDAS_DIR}"

for name in webhook runners runner-binaries-syncer; do
  dest="${LAMBDAS_DIR}/${name}.zip"
  echo "Downloading ${name}.zip (${MODULE_VERSION})..."
  curl -fL --retry 3 -o "${dest}" "${BASE_URL}/${name}.zip"

  actual_sha256="$(shasum -a 256 "${dest}" | awk '{print $1}')"
  expected_sha256="$(echo "${LAMBDA_CHECKSUMS}" | awk -v n="${name}" '$1 == n {print $2}')"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    echo "CHECKSUM MISMATCH for ${name}.zip" >&2
    echo "  expected: ${expected_sha256}" >&2
    echo "  actual:   ${actual_sha256}" >&2
    rm -f "${dest}"
    exit 1
  fi
  echo "  sha256 OK: ${actual_sha256}"
done

echo "All three v7.11.0 lambda zips downloaded and verified in ${LAMBDAS_DIR}/"
