#!/usr/bin/env bash
#
# Verify a built container image before it is published.
#
#   scripts/check-image.sh gpt-oss-azure-opencode-shim:test
#
# 1. It runs as a non-root user and has no shell.
# 2. It starts with a read-only root filesystem, no capabilities and
#    no-new-privileges, answers /healthz and reports healthy.
# 3. The local-only Host check still holds inside the container.
#
set -euo pipefail

image="${1:?usage: $0 IMAGE}"
port=19526
name="shim-check-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

echo "== user and shell"
user="$(docker inspect "$image" --format '{{.Config.User}}')"
case "$user" in
    "" | 0 | 0:* | root | root:*) echo "image runs as root ($user)" >&2; exit 1 ;;
esac
echo "user $user"
if docker run --rm --entrypoint sh "$image" -c true >/dev/null 2>&1; then
    echo "image has a shell" >&2
    exit 1
fi
docker run --rm "$image" --version

echo "== hardened start"
docker run -d --name "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
    -p "127.0.0.1:$port:9526" \
    -e UPSTREAM_URL=https://upstream.invalid/openai -e AZURE_FOUNDRY_API_KEY=placeholder \
    "$image" >/dev/null
for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "http://127.0.0.1:$port/healthz"
echo

for _ in $(seq 1 60); do
    health="$(docker inspect "$name" --format '{{.State.Health.Status}}')"
    [ "$health" = healthy ] && break
    [ "$health" = unhealthy ] && break
    sleep 1
done
[ "$health" = healthy ] || { echo "container health: $health" >&2; docker logs "$name" >&2; exit 1; }
echo "health $health"

echo "== Entra ID mode starts without a key"
docker rm -f "$name" >/dev/null
docker run -d --name "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
    -p "127.0.0.1:$port:9526" -e UPSTREAM_URL=https://upstream.invalid/openai \
    "$image" >/dev/null
for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "http://127.0.0.1:$port/healthz" || { docker logs "$name" >&2; exit 1; }
echo

echo "== Host check"
status="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: attacker.example' "http://127.0.0.1:$port/healthz")"
[ "$status" = 403 ] || { echo "foreign Host header got HTTP $status, want 403" >&2; exit 1; }
echo "foreign Host header: 403"

echo "== ok"
