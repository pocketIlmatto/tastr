# The hosted Tastr web server for Fly.io (docs/deploy.md). Analysis and stems run on Modal
# (tastr/cloud/modal_web.py), so this image has no Demucs or PyTorch and the server fits in 512 MB.

# The Rust engine's WebAssembly build, which the server hands to browsers with TASTR_BROWSER_ENGINE on
# (docs/rust-engine.md). Only the .wasm file (about 2 MB) goes into the server image.
FROM rust:1.97.0-slim AS engine
RUN rustup target add wasm32-unknown-unknown
WORKDIR /src
COPY engine-rs ./
RUN cargo build --release --lib --target wasm32-unknown-unknown --locked

FROM python:3.12-slim

# Litestream streams every change to the library database to R2, and restores it onto a new volume.
ARG LITESTREAM_VERSION=0.3.13
ADD --checksum=sha256:eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0 \
    https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz litestream && rm /tmp/litestream.tar.gz

# sql.js (SQLite as WebAssembly, MIT): hosted searches run in the browser on a copy of the person's library (TAS-49).
# The server hands these two files to the page from /sqljs/ (tastr/web/routes/search.py, SQLJS_VERSION).
ARG SQLJS_VERSION=1.10.3
ADD --checksum=sha256:ca1d58734183c5bbe14d2a7eee4637c40f2be1ae765609b0d589123a8070bb25 \
    https://registry.npmjs.org/sql.js/-/sql.js-${SQLJS_VERSION}.tgz /tmp/sqljs.tgz
RUN mkdir -p /app/sqljs \
    && tar -C /app/sqljs --strip-components=2 -xzf /tmp/sqljs.tgz package/dist/sql-wasm.js package/dist/sql-wasm.wasm \
    && tar -C /app/sqljs --strip-components=1 -xzf /tmp/sqljs.tgz package/LICENSE \
    && rm /tmp/sqljs.tgz

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tastr ./tastr
# No DSP libraries: cards come from Modal or the visitor's browser. numpy at the Modal images' pin.
RUN pip install --no-cache-dir ".[hosted,cloud]" "numpy<2.3"
# Which commit this is and the GitHub Actions run that built it (.github/workflows/deploy.yml passes them),
# for the deploy lines in the admin log. Late in the file, so a new commit doesn't redo the pip install.
ARG TASTR_COMMIT=""
ARG TASTR_COMMIT_SUBJECT=""
ARG TASTR_DEPLOY_RUN=""
ENV TASTR_COMMIT=$TASTR_COMMIT TASTR_COMMIT_SUBJECT=$TASTR_COMMIT_SUBJECT TASTR_DEPLOY_RUN=$TASTR_DEPLOY_RUN
COPY --from=engine /src/target/wasm32-unknown-unknown/release/tastr_engine.wasm /app/engine/tastr_engine.wasm
COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/start.sh /app/start.sh

ENV TASTR_HOME=/data PYTHONUNBUFFERED=1 TASTR_ENGINE_WASM=/app/engine/tastr_engine.wasm TASTR_SQLJS_DIR=/app/sqljs
EXPOSE 8080
CMD ["/app/start.sh"]
