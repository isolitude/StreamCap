# --- 第一阶段: 编译/构建阶段 ---
ARG http_proxy
ARG https_proxy

FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 先复制 streamget 目录和 requirements 文件
COPY streamget/ ./streamget/
COPY requirements-web.txt .

# 优先从本地安装 streamget
RUN pip install --no-cache-dir ./streamget

# 然后安装其他依赖（streamget 已安装，会被跳过）
RUN pip install --no-cache-dir -r requirements-web.txt

# 先复制构建时需要的字体脚本，使字体回退步骤不依赖全部源码上下文
COPY scripts/download_fallback_fonts.py ./scripts/download_fallback_fonts.py
# 把本地 fallback 字体缓存烘焙进镜像的 flet_web 包，供 no_cdn web 模式离线提供中文等
# 回退子集。这些字体位于 build context 的 assets/fonts/（已不在 .dockerignore 中排除）。
# 放在 COPY . . 之前 → 字体层在最前面，后续源码/资产变更不会触发这层重传。
# 复制到 flet_web/web/assets/fonts/:Flet 在 no_cdn 模式下正是从该目录提供 /assets/fonts/ 路径。
RUN mkdir -p /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts
COPY assets/fonts/ /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/
# 若本地缓存缺失(全新环境),则回退到在线下载补齐(仅 fallback 场景,正常构建走本地缓存)。
# 以 notosanssc 目录是否存在来判断 fallback 字体是否就位。
RUN if [ ! -d /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/notosanssc ]; then \
        echo "Local fallback font cache empty, downloading online"; \
        python3 scripts/download_fallback_fonts.py --out /tmp/flutter_fonts && \
        cp -rn /tmp/flutter_fonts/* /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/ && \
        rm -rf /tmp/flutter_fonts; \
    fi

# 复制其他项目文件
COPY . .
# 提示：在这里创建目录其实没用，因为第二阶段是干净的镜像，建议在第二阶段创建

# --- 可选目标: 精简无头远程录制后端 (target: headless) ---
FROM python:3.12-slim AS headless

ARG PUID=1000
ARG PGID=1000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo "$TZ" > /etc/timezone

COPY streamget/ ./streamget/
COPY requirements-headless.txt .
RUN pip install --no-cache-dir ./streamget \
    && pip install --no-cache-dir -r requirements-headless.txt

COPY app/ ./app/
COPY config/default_settings.json config/language.json config/version.json ./config/
COPY locales/ ./locales/

RUN groupadd -g ${PGID} appgroup && \
    useradd -u ${PUID} -g appgroup -s /bin/sh -m appuser && \
    mkdir -p /app/logs /app/downloads /app/config && \
    chown -R appuser:appgroup /app

USER appuser
ENV STREAMCAP_DATA_DIR=/app
ENV STREAMCAP_RUN_DIR=/app
ENV STREAMCAP_HOST=0.0.0.0
ENV STREAMCAP_PORT=8000
EXPOSE 8000

CMD ["python", "-m", "app.api.remote_server"]

# --- 默认生产镜像: Web 前端 (包含远程后端客户端支持) ---
FROM python:3.12-slim AS web

# 1. 定义构建参数，允许在 docker-compose 中传入宿主机的 UID/GID
ARG PUID=1000
ARG PGID=1000

WORKDIR /app

# 2. 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 3. 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo "$TZ" > /etc/timezone

# 4. 创建非 root 用户
# 创建一个名为 appuser 的用户，并赋予指定的 UID 和 GID
RUN groupadd -g ${PGID} appgroup && \
    useradd -u ${PUID} -g appgroup -s /bin/sh -m appuser

# 5. 从 builder 阶段复制文件，并利用 --chown 直接修改所有权
# 关键点：将 site-packages 和可执行文件的权限也交给 appuser
COPY --from=builder --chown=appuser:appgroup /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder --chown=appuser:appgroup /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appgroup /app/ ./

# 6. 预创建必要的持久化目录并授权
# 确保在容器启动前，这些目录的所有者就是 appuser
RUN mkdir -p /app/logs /app/downloads /app/config && \
    chown -R appuser:appgroup /app

# 7. 切换到非 root 用户运行
USER appuser

# 暴露端口（仅作为文档声明，不影响实际映射）
EXPOSE 6006

CMD ["sh", "-c", "python main.py --web --host 0.0.0.0"]