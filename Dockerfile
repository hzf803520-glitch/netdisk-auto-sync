FROM node:22-alpine

WORKDIR /app

COPY deploy-source/app-lite.part* /tmp/source-parts/
RUN cat /tmp/source-parts/app-lite.part* | base64 -d | tar -xz -C /app && rm -rf /tmp/source-parts

RUN npm install && npm run build

ENV NODE_ENV=production
ENV PORT=10000
ENV HOSTNAME=0.0.0.0
EXPOSE 10000

CMD ["npm", "run", "start"]
