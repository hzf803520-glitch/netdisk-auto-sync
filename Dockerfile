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
# 重建脚本以未污染的 part00 长度为准，截取其余分片的有效前缀，清除换行，
# 再实际解码并打开 tar.gz 验证，不再依赖可能过期的固定 SHA 值。
COPY deploy-source/kdocs-override.part* /tmp/kdocs-override/
COPY deploy-source/rebuild-kdocs-overlay.py /tmp/rebuild-kdocs-overlay.py
RUN python3 /tmp/rebuild-kdocs-overlay.py /tmp/kdocs-override /tmp/kdocs-override.tar.gz \
  && tar -xzf /tmp/kdocs-override.tar.gz -C /app \
  && rm -rf /tmp/kdocs-override /tmp/kdocs-override.tar.gz /tmp/rebuild-kdocs-overlay.py

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