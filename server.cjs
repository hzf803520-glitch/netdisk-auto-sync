'use strict';

const { createServer } = require('node:http');
const next = require('next');

const port = Number.parseInt(process.env.PORT || '10000', 10);
const hostname = process.env.HOSTNAME || '0.0.0.0';
const app = next({ dev: false, hostname, port });
const handle = app.getRequestHandler();

app.prepare()
  .then(() => {
    const server = createServer((req, res) => {
      const pathname = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`).pathname;

      if (pathname === '/healthz' || pathname === '/api/health') {
        res.writeHead(200, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store'
        });
        res.end(JSON.stringify({
          status: 'ok',
          service: 'netdisk-auto-sync-v2',
          timestamp: new Date().toISOString()
        }));
        return;
      }

      Promise.resolve(handle(req, res)).catch((error) => {
        console.error('request failed', error);
        if (!res.headersSent) {
          res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
        }
        res.end('Internal Server Error');
      });
    });

    server.on('error', (error) => {
      console.error('server error', error);
      process.exit(1);
    });

    server.listen(port, hostname, () => {
      console.log(`server ready on http://${hostname}:${port}`);
    });
  })
  .catch((error) => {
    console.error('failed to prepare Next.js', error);
    process.exit(1);
  });
