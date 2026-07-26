FROM node:22-bookworm AS build

WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# 使用用户原本的 Next.js 项目
COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app \
  && rm -rf /tmp/source-parts

# 追加 KDocs Cookie 读取和前500条同步功能
# 分片直接按文件顺序合并，不做固定长度和 SHA 校验，避免 GitHub 内容转换导致误失败
COPY deploy-source/kdocs-override.part* /tmp/kdocs-override/
RUN set -eux; \
  cat /tmp/kdocs-override/kdocs-override.part* > /tmp/kdocs-override.b64; \
  base64 -d /tmp/kdocs-override.b64 > /tmp/kdocs-override.tar.gz; \
  tar -xzf /tmp/kdocs-override.tar.gz -C /app; \
  rm -rf /tmp/kdocs-override /tmp/kdocs-override.b64 /tmp/kdocs-override.tar.gz

RUN npm install --no-audit --no-fund \
  && npm run build

FROM node:22-bookworm-slim

WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends chromium ca-certificates fonts-noto-cjk \
  && rm -rf /var/lib/apt/lists/*

COPY --from=build /app /app

ENV NODE_ENV=production
ENV PORT=10000
ENV HOSTNAME=0.0.0.0
ENV CHROMIUM_PATH=/usr/bin/chromium

EXPOSE 10000
CMD ["npm", "run", "start"]