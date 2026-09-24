#!/usr/bin/env bash
#
# Verify built distributions before they are published.
#
#   scripts/check-dist.sh dist/
#
# 1. Metadata renders on PyPI (twine check --strict) and the wheel has no
#    stray or duplicate files (check-wheel-contents).
# 2. The wheel installs into a clean venv and both commands start.
# 3. The full test suite passes from the extracted sdist.
#
set -euo pipefail

dist="$(cd "${1:?usage: $0 DIST_DIR}" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

wheels=("$dist"/*.whl)
sdists=("$dist"/*.tar.gz)
[ "${#wheels[@]}" -eq 1 ] && [ -f "${wheels[0]}" ] || { echo "expected one wheel in $dist" >&2; exit 1; }
[ "${#sdists[@]}" -eq 1 ] && [ -f "${sdists[0]}" ] || { echo "expected one sdist in $dist" >&2; exit 1; }

echo "== metadata"
python -m twine check --strict "${wheels[0]}" "${sdists[0]}"
python -m check_wheel_contents "${wheels[0]}"

echo "== wheel in a clean venv"
python -m venv "$work/wheel"
"$work/wheel/bin/pip" install --quiet "${wheels[0]}"
"$work/wheel/bin/gpt-oss-azure-opencode-shim" --version
"$work/wheel/bin/gpt-oss-azure-opencode-shim-report" --help >/dev/null
if env -u UPSTREAM_URL -u AZURE_FOUNDRY_API_KEY "$work/wheel/bin/gpt-oss-azure-opencode-shim" 2>/dev/null; then
    echo "the shim started without configuration" >&2
    exit 1
fi

echo "== tests from the sdist"
tar -xzf "${sdists[0]}" -C "$work"
src=("$work"/gpt_oss_azure_opencode_shim-*/)
python -m venv "$work/sdist"
"$work/sdist/bin/pip" install --quiet "${src[0]}[dev]"
(cd "${src[0]}" && "$work/sdist/bin/pytest" -q -p no:cacheprovider)

echo "== ok"
