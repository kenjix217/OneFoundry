FROM node:22.22.2-bookworm-slim@sha256:868499d55378719bffa87b0ed1f099591823c029b543043c09c2483468e93201

WORKDIR /opt/claude-code
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --include=optional --no-audit --no-fund \
    && test "$(node -p 'require("./node_modules/@anthropic-ai/claude-code/package.json").version')" = "2.1.274" \
    && test -x node_modules/.bin/claude

USER node
ENV HOME=/home/claude DISABLE_UPDATES=1
ENTRYPOINT ["/opt/claude-code/node_modules/.bin/claude"]
