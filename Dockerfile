FROM node:22-bookworm AS build

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app \
  && rm -rf /tmp/source-parts

# Apply targeted fixes after unpacking the uploaded application source.
COPY overrides/app/api/providers/config/route.ts /app/app/api/providers/config/route.ts
COPY overrides/app/components/netdisk-setup.tsx /app/app/components/netdisk-setup.tsx
COPY overrides/lib/secrets.ts /app/lib/secrets.ts

RUN npm install --no-audit --no-fund \
  && npm run build

FROM node:22-bookworm-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends chromium ca-certificates fonts-noto-cjk \
  && rm -rf /var/lib/apt/lists/*

COPY --from=build /app /app
COPY server.cjs /app/server.cjs

ENV NODE_ENV=production
ENV PORT=10000
ENV HOSTNAME=0.0.0.0
ENV CHROMIUM_PATH=/usr/bin/chromium

EXPOSE 10000
CMD ["node", "server.cjs"]
