import { createServer } from 'node:http';
import next from 'next';

const port = Number.parseInt(process.env.PORT || '10000', 10);
const hostname = process.env.HOSTNAME || '0.0.0.0';
const app = next({ dev: false, hostname, port });
const handle = app.getRequestHandler();

try {
  await app.prepare();

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

    handle(req, res);
  });

  server.listen(port, hostname, () => {
    console.log(`server ready on http://${hostname}:${port}`);
  });
} catch (error) {
  console.error('failed to start server', error);
  process.exit(1);
}
