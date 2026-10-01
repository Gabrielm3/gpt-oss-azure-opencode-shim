# syntax=docker/dockerfile:1
#
# The image installs the wheel that the release pipeline already built and
# verified, so the container runs the same bytes as the PyPI package:
#
#   python -m build --wheel              # writes dist/*.whl
#   docker build -t gpt-oss-azure-opencode-shim .
#
# Runtime: Chainguard's distroless Python (no shell, no package manager,
# non-root user 65532). It had no fixable HIGH or CRITICAL CVE when chosen,
# while the Debian 12 distroless image carried 19. The build stage is the
# -dev variant of the same image, so both stages share one Python (3.14,
# covered by the CI matrix). Digests are pinned; Dependabot keeps them current.

FROM cgr.dev/chainguard/python:latest-dev@sha256:e7f69a8bf7fcae4010680cc2e6c28abf0cd172cfdf71d79cac29571a46b71da8 AS build

COPY dist/*.whl /tmp/dist/
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
        --target /tmp/shim "$(ls /tmp/dist/*.whl)[otel,entra]"

FROM cgr.dev/chainguard/python:latest@sha256:275d5493a184cf00a421871b1197de8eeb30c91280a8a18c222d61d9fb2c7919

COPY --from=build /tmp/shim /opt/shim

# Inside the container the shim must listen on all interfaces so a published
# port can reach it. Publish it on loopback only:
#   docker run -p 127.0.0.1:9526:9526 ...
# `-p 9526:9526` would let any machine on the network send requests that the
# shim signs with your Azure credentials.
#
# Without AZURE_FOUNDRY_API_KEY the shim uses Entra ID. The image has no az
# CLI, so the token comes from a managed identity (Azure hosts) or workload
# identity (AKS), through DefaultAzureCredential.
ENV PYTHONPATH=/opt/shim \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHIM_HOST=0.0.0.0 \
    SHIM_PORT=9526

USER 65532:65532
EXPOSE 9526

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9526/healthz', timeout=2)"]

ENTRYPOINT ["python", "-m", "gpt_oss_shim"]
