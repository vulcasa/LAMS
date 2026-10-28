# 合并版镜像：内置开源 LTFS（编译），可直接管理磁带设备。
#
# 构建（国内网络建议加镜像参数）：
#   docker build \
#     --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm \
#     --build-arg APT_MIRROR=mirrors.aliyun.com \
#     --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#     -t av-archive:local .
#
# 运行时需要把磁带设备直通进来（compose 里 devices + privileged）。

ARG BASE_IMAGE=python:3.12-slim-bookworm
ARG APT_MIRROR=deb.debian.org

# ---------------------------------------------------------------------------
# 1) 编译 LTFS（多阶段，产物只进运行镜像）
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS ltfs-builder
ARG APT_MIRROR
ENV DEBIAN_FRONTEND=noninteractive

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config ca-certificates \
        libfuse-dev libxml2-dev libssl-dev libsnmp-dev libicu-dev uuid-dev \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/ltfs-*.tar.gz /tmp/ltfs.tar.gz
COPY vendor/uthash-*.tar.gz /tmp/uthash.tar.gz
RUN mkdir -p /tmp/ltfs && tar xzf /tmp/ltfs.tar.gz -C /tmp/ltfs --strip-components=1 \
    && mkdir -p /tmp/ltfs/src/libltfs/uthash_submodule \
    && tar xzf /tmp/uthash.tar.gz -C /tmp/ltfs/src/libltfs/uthash_submodule --strip-components=1 \
    && cd /tmp/ltfs \
    && ./autogen.sh \
    && ./configure --prefix=/usr --sysconfdir=/etc \
    && make -j"$(nproc)" \
    && make install DESTDIR=/ltfs-root

# ---------------------------------------------------------------------------
# 2) 运行镜像
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE}
ARG APT_MIRROR
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    LANG=C.UTF-8

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        libfuse2 libxml2 libsnmp40 libicu72 libssl3 libuuid1 \
        sg3-utils mt-st util-linux ca-certificates cifs-utils ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 把编译好的 LTFS（/usr/bin/ltfs、/usr/lib/libltfs*、/etc/ltfs.conf）拷进来
COPY --from=ltfs-builder /ltfs-root/ /

WORKDIR /app

COPY requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY app ./app
COPY scripts ./scripts
COPY wsgi.py ./

ENV AVLIB_DATA_DIR=/data \
    LTFS_MOUNT=/media/tape \
    AVLIB_TAPE_ADAPTER=direct \
    PORT=8000

RUN mkdir -p /data /media/tape && ldconfig && (ltfs --version || echo "WARN: ltfs 不可用")

EXPOSE 8000

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "12", "--timeout", "300", "wsgi:app"]
