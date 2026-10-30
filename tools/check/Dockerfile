# `uv run check --linux` 的检查镜像，由 tools/check 构建并运行。仓库在运行时以 bind mount 挂到 /work，不复制进镜像。
# Rust 与 Python 的版本须与 rust-toolchain.toml、.python-version 一致，由 tools/check 的测试核对。
FROM rust:1.99.1-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /usr/local/bin/
RUN rustup component add rustfmt clippy
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12

# /cache/uv 与 cargo registry 是各检出目录共用的下载缓存卷；/cache/work 是本检出目录专用的卷，
# 放虚拟环境、cargo target 与工具缓存，不写回仓库。
ENV UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/cache/uv \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/cache/work/venv \
    CARGO_TARGET_DIR=/cache/work/target \
    RUFF_CACHE_DIR=/cache/work/ruff \
    PYTHONPYCACHEPREFIX=/cache/work/pycache \
    PYTEST_ADDOPTS="-o cache_dir=/cache/work/pytest"
WORKDIR /work
