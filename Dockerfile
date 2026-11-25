# Platform image: all services (grid / site / gateway / controller) share one image and differ by
# their command.
#
# Build: docker build -t cpc-platform:latest .
# A PyPI mirror may be given: docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t cpc-platform:latest .

FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    CPC_DATA_DIR=/var/lib/cpc/datasets

WORKDIR /app
# ipmitool: whole-server power through /dev/ipmi0 on bare-metal servers (ipmi sensor)
RUN apt-get update && apt-get install -y --no-install-recommends ipmitool && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[nvml,docker]"
COPY configs ./configs
COPY deploy ./deploy
# public datasets are not part of the image: mount the dataset directory of the host at $CPC_DATA_DIR

EXPOSE 8080 9000 9100 9300 9400
ENTRYPOINT ["cpc"]
CMD ["--help"]
