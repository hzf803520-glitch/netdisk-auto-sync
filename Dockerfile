FROM node:22-bookworm AS build

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# 使用上传的完整 Next.js 项目作为唯一代码源。
# 不再通过 base64/tar 分片覆盖，避免 GitHub 分片损坏导致 Render 构建失败。
# 项目内包含共享文档 Cookie、前500条读取、变化监听、增量同步和 CSV 导出功能。
COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app \
  && rm -rf /tmp/source-parts

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
