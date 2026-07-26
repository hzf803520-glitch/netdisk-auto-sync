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
# part01/part03 尾部混入过额外内容；每个有效分片占 8198 字节，其中包含换行。
# 先截取有效范围，再删除换行后拼接，避免校验值因为分片换行而不一致。
COPY deploy-source/kdocs-override.part* /tmp/kdocs-override/
RUN set -eux; \
  : > /tmp/kdocs-override.b64; \
  for file in \
    /tmp/kdocs-override/kdocs-override.part00 \
    /tmp/kdocs-override/kdocs-override.part01 \
    /tmp/kdocs-override/kdocs-override.part02 \
    /tmp/kdocs-override/kdocs-override.part03; do \
      test "$(wc -c < "$file")" -ge 8198; \
      head -c 8198 "$file" | tr -d '\r\n\t ' >> /tmp/kdocs-override.b64; \
    done; \
  wc -c /tmp/kdocs-override.b64; \
  echo "31f57d2528f66aa05c4762aaea6f7e82c35eeb3fec6a21b42fb765db01f1dff1  /tmp/kdocs-override.b64" | sha256sum -c -; \
  base64 -d /tmp/kdocs-override.b64 > /tmp/kdocs-override.tar.gz; \
  echo "339fef1d55e1f6fa6cf801a01214aac9474fbaec5aa0ac0cb100a8be39d05e3c  /tmp/kdocs-override.tar.gz" | sha256sum -c -; \
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