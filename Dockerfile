# ─── Stage 1: python deps into a venv ───────────────────────────────────────
FROM python:3.12-slim AS pydeps
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv export --no-dev --frozen -o requirements.txt \
    && python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

# ─── Stage 2: the azure-devops-mcp backend + a node runtime to run it ────────
FROM node:22-slim AS nodedeps
RUN npm install -g @azure-devops/mcp --omit=dev

# ─── Final: slim python + copied venv + copied node runtime & backend ────────
FROM python:3.12-slim
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
CMD ["python", "server.py"]
