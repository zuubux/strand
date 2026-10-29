# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin strand \
 && mkdir /data && chown 10001:10001 /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    STRAND_DB_PATH=/data/memory.db \
    STRAND_LOG_PATH=/data/strand.log \
    STRAND_HOST=0.0.0.0 \
    STRAND_PORT=8088
VOLUME /data
USER 10001:10001
EXPOSE 8088
ENTRYPOINT ["python", "-m", "strand.daemon"]
