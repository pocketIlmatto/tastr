# The hosted Tastr web server for Fly.io (docs/deploy.md). Analysis and stems run on Modal
# (tastr/cloud/modal_web.py), so this image has no Demucs or PyTorch and the server fits in 512 MB.
FROM python:3.12-slim

# Litestream streams every change to the library database to R2, and restores it onto a new volume.
ARG LITESTREAM_VERSION=0.3.13
ADD --checksum=sha256:eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0 \
    https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz litestream && rm /tmp/litestream.tar.gz

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tastr ./tastr
# The engine's version pins (docs/bench.md), the same as the Modal images.
RUN pip install --no-cache-dir ".[hosted,cloud]" "numpy<2.3" "numba==0.61.2" "llvmlite==0.44.0" "librosa==0.11.0" "scipy<1.16"
COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/start.sh /app/start.sh

ENV TASTR_HOME=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["/app/start.sh"]
