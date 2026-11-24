# 平台镜像：所有服务（grid / site / gateway / controller / loadgen / nvml-exporter）共用一个镜像，
# 通过不同的启动命令区分角色。
#
# 构建：docker build -t cpc-platform:latest .
# 国内服务器可指定 PyPI 镜像：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t cpc-platform:latest .

FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    CPC_DATA_DIR=/var/lib/cpc/datasets

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[nvml,docker,data]"
COPY configs ./configs
COPY deploy ./deploy
# 公开数据集不打包进镜像：需要时把宿主机的数据集缓存目录挂载到 $CPC_DATA_DIR

EXPOSE 8080 9000 9100 9300 9400
ENTRYPOINT ["cpc"]
CMD ["--help"]
