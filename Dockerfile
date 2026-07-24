FROM golang:1.24-bookworm AS worker
WORKDIR /src
COPY go.mod ./
RUN go get github.com/wgx0307/netdisk@eb4cd97607558d1c54ed317fdf9aa1b364ea3535
COPY cmd ./cmd
RUN go build -trimpath -ldflags="-s -w" -o /out/netdisk-worker ./cmd/netdisk-worker

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY v2 ./v2
COPY --from=worker /out/netdisk-worker ./bin/netdisk-worker
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 10000
CMD ["python", "v2/app.py"]
