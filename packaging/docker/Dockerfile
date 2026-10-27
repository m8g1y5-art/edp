# 具身数据平台 · 服务器镜像
# 在仓库根目录构建：docker build -f packaging/docker/Dockerfile -t edp:<版本> .
# 基础镜像：PyTorch 2.7 + CUDA 12.6（支持 V100 / A100 / 4090 / 5080 等，sm_70 起）
ARG BASE=pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime
ARG BUILDER=pytorch/pytorch:2.7.1-cuda12.6-cudnn9-devel

# ---------- 第一阶段：把核心模块编译成二进制 ----------
FROM ${BUILDER} AS builder
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX} "cython>=3.0" setuptools
WORKDIR /src
COPY app ./app
COPY launch.py ./
COPY packaging/compile.py /tmp/compile.py
RUN python /tmp/compile.py /src /rel

# ---------- 第二阶段：运行镜像（只有编译后的模块，没有核心源码） ----------
FROM ${BASE}
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    EDP_MODE=server EDP_DATA=/data EDP_MODELS=/opt/edp/models EDP_IMPORT_ROOTS=/imports \
    GLOG_minloglevel=2 TF_CPP_MIN_LOG_LEVEL=3
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r /tmp/requirements.txt \
    && pip install --no-cache-dir -i ${PIP_INDEX} --no-deps ultralytics ultralytics-thop \
    && pip install --no-cache-dir -i ${PIP_INDEX} polars psutil matplotlib pillow requests
# SAM 2 权重放进镜像，客户机器不用联网
RUN mkdir -p /opt/edp/models && (curl -fL --retry 3 -o /opt/edp/models/sam2.1_s.pt \
      https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt \
    || curl -fL --retry 3 -o /opt/edp/models/sam2.1_s.pt \
      https://ghfast.top/https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt) \
    && test $(stat -c %s /opt/edp/models/sam2.1_s.pt) -gt 50000000
WORKDIR /opt/edp
COPY --from=builder /rel/ /opt/edp/
# 自检：模块都能导入（编译出错在构建时就发现）
RUN python -c "import app.pipeline, app.objects, app.license, app.server, cv2, mediapipe, torch, ultralytics; print('ok', torch.__version__)"
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD curl -fs http://127.0.0.1:8765/api/me >/dev/null || exit 1
CMD ["python", "launch.py", "server"]
