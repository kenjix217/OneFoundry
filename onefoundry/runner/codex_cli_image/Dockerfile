FROM node:22.22.2-bookworm-slim@sha256:868499d55378719bffa87b0ed1f099591823c029b543043c09c2483468e93201

WORKDIR /opt/codex-cli
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --include=optional --no-audit --no-fund \
    && test "$(node -p 'require("./node_modules/@openai/codex/package.json").version')" = "0.156.0" \
    && test -x node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex

USER node
ENV HOME=/home/codex CODEX_HOME=/home/codex/.codex
ENTRYPOINT ["/opt/codex-cli/node_modules/.bin/codex"]
