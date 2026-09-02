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

# 先复制构建时需要的字体脚本和种子字体清单，使字体步骤不依赖全部源码上下文
COPY scripts/download_fallback_fonts.py ./scripts/download_fallback_fonts.py
# 复用本地已有镜像的 fallback 字体作为构建种子（避免 gstatic 批量下载偶发失败导致构建中断）。
# 字体层位于 COPY . . 之前 → 之后任何源码/资产变更都不会触发字体重下/重传。
# 若本机无该镜像（全新环境），此 COPY 会失败；届时改为去掉本行、保留下方在线下载兜底即可。
COPY --from=streamcap-streamcap:latest /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/ /tmp/seed_fonts/
# 种子字体已包含全部 fallback 字体，直接注入 flet_web;仅当种子为空时才回退在线下载
RUN if [ -n "$(ls -A /tmp/seed_fonts 2>/dev/null)" ]; then \
        mkdir -p /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts && \
        cp -rn /tmp/seed_fonts/* /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/ && \
        echo "Seeded fallback fonts from previous image ($(ls /tmp/seed_fonts | wc -l) families)"; \
    else \
        echo "Seed fonts empty, falling back to online download"; \
        python3 scripts/download_fallback_fonts.py --out /tmp/flutter_fonts && \
        cp -rn /tmp/flutter_fonts/* /usr/local/lib/python3.12/site-packages/flet_web/web/assets/fonts/; \
    fi; \
    rm -rf /tmp/flutter_fonts /tmp/seed_fonts

# 复制其他项目文件
COPY . .
# 提示：在这里创建目录其实没用，因为第二阶段是干净的镜像，建议在第二阶段创建

# --- 第二阶段: 最终生产镜像 ---
FROM python:3.12-slim

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