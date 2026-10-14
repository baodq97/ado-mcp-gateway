# ─── Stage 1: python deps into a venv ───────────────────────────────────────
FROM python:3.12-slim AS pydeps
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv export --no-dev --frozen -o requirements.txt \
    && python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /venv/bin/pip uninstall -y pip

# ─── Stage 2: the azure-devops-mcp backend + a node runtime to run it ────────
FROM node:22-slim AS nodedeps
# Prune what never loads on linux/node: other-OS broker binaries, the browser-only
# MSAL (only identity's browser build imports it), source maps and type declarations
RUN npm install -g @azure-devops/mcp@2.10.0 --omit=dev \
    && cd /usr/local/lib/node_modules/@azure-devops/mcp/node_modules \
    && rm -rf @azure/msal-node-runtime/dist/macos @azure/msal-node-runtime/dist/windows @azure/msal-browser \
    && find . \( -name '*.map' -o -name '*.d.ts' -o -name '*.d.mts' -o -name '*.d.cts' \) -delete

# ─── Final: slim python + copied venv + copied node runtime & backend ────────
FROM python:3.12-slim
# Pick up Debian security fixes newer than the base image; drop pip (unused at runtime)
# libsecret: the backend's msal-node-extensions dlopen()s it at startup and crashes without it
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libsecret-1-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && useradd -m -u 10001 app
# node binary + the globally-installed backend (no npm/npx in the final image)
COPY --from=nodedeps /usr/local/bin/node /usr/local/bin/node
COPY --from=nodedeps /usr/local/lib/node_modules/@azure-devops /usr/local/lib/node_modules/@azure-devops
COPY --from=pydeps /venv /venv

WORKDIR /app
COPY server.py ./

# Backend entry — server.py runs this directly instead of `npx -y @azure-devops/mcp`
ENV PATH=/venv/bin:$PATH \
    ADO_GW_BACKEND_CMD=node \
    ADO_GW_BACKEND_ARG=/usr/local/lib/node_modules/@azure-devops/mcp/dist/index.js \
    ADO_GW_HOST=0.0.0.0 ADO_GW_PORT=8090
EXPOSE 8090
# fastmcp keeps OAuth client state under ~/.local/share/fastmcp, so the user needs a home
USER app
CMD ["python", "server.py"]
