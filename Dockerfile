FROM node:22-bookworm AS build

WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# 解压用户原本已经跑通的 Next.js 项目。
COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app \
  && rm -rf /tmp/source-parts

# 只覆盖新增的金山文档 Cookie 读取、前 500 条识别、变化监听和增量同步功能。
COPY deploy-source/kdocs-cookie-override.b64 /tmp/kdocs-cookie-override.b64
RUN base64 -d /tmp/kdocs-cookie-override.b64 | tar -xz -C /app \
  && rm -f /tmp/kdocs-cookie-override.b64

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
