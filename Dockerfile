# Source and checksum from https://www.haproxy.org/download/3.2/src/.
FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS proxy
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl --fail --silent --show-error --location --proto '=https' \
      https://www.haproxy.org/download/3.2/src/haproxy-3.2.23.tar.gz -o haproxy.tar.gz \
    && echo '82d14ef33571e4edeb9197516c0d058a3775fb80541e46afe4377428e461fef0  haproxy.tar.gz' | sha256sum -c - \
    && tar -xzf haproxy.tar.gz --strip-components=1 \
    && make -j2 TARGET=linux-glibc USE_THREAD=1 \
    && strip haproxy

FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS wheels
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --only-binary=:all: --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    AI_PROVIDER=workers-ai AI_GATEWAY_URL=https://ai.prateekmulye.dev/v1/infer \
    PUBLIC_ORIGIN=https://scheduler.prateekmulye.dev \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY --from=wheels /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=proxy /build/haproxy /usr/local/sbin/haproxy
COPY --from=proxy /build/haproxy.tar.gz /usr/share/doc/haproxy/source.tar.gz
COPY Dockerfile /usr/share/doc/haproxy/Dockerfile
COPY ai.py hosted.py scheduler.py server.py ./
COPY static ./static
COPY examples ./examples
COPY deploy ./deploy
RUN /usr/local/sbin/haproxy -c -f /app/deploy/haproxy.cfg
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 CMD python -c "import urllib.request; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/health',headers={'Host':'scheduler.prateekmulye.dev'}),timeout=3); assert r.status == 200"
ENTRYPOINT ["python", "-B", "/app/deploy/run.py"]
CMD ["python", "-B", "hosted.py"]
