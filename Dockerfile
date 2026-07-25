FROM node:22-alpine

WORKDIR /app

COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app && rm -rf /tmp/source-parts

# 兼容原 Render 服务里已有的 COOKIE_ENCRYPTION_KEY，避免重新部署后 Cookie 无法解密。
RUN sed -i 's/process.env.CREDENTIAL_SECRET || ""/process.env.CREDENTIAL_SECRET || process.env.COOKIE_ENCRYPTION_KEY || ""/' lib/secrets.ts

RUN npm install && npm run build

ENV NODE_ENV=production
ENV PORT=10000
ENV HOSTNAME=0.0.0.0
EXPOSE 10000

CMD ["npm", "run", "start"]
