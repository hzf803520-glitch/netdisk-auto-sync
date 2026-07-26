const http = require('http');
const next = require('next');

const port = process.env.PORT || 10000;
const hostname = '0.0.0.0';

const app = next({ dev: false, hostname, port });
const handle = app.getRequestHandler();

app.prepare().then(() => {
  http.createServer((req, res) => {
    if (req.url === '/healthz') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok' }));
      return;
    }
    handle(req, res);
  }).listen(port, hostname, () => {
    console.log(`server ready on ${port}`);
  });
});
