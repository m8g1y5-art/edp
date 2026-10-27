#!/usr/bin/env bash
# 具身数据平台：Linux 服务器上的一键启动（对应 Windows 的 run.bat）
# 用法：bash run.sh            默认用第 0 张显卡
#       EDP_GPU=1 bash run.sh  指定用第 1 张显卡
set -e
cd "$(dirname "$0")"

ENV_NAME=edp
TUNA="-i https://pypi.tuna.tsinghua.edu.cn/simple"

# 1) conda 环境（服务器上已有 miniconda3）
source "$(conda info --base)/etc/profile.d/conda.sh"
if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "== 创建 Python 3.11 环境 $ENV_NAME"
  conda create -y -n "$ENV_NAME" python=3.11
fi
conda activate "$ENV_NAME"

# 2) 依赖只装一次
if [ ! -f .ok_linux ]; then
  echo "== 安装基础依赖"
  pip install -r requirements.txt $TUNA
  echo "== 安装 PyTorch（CUDA 12.6 版：V100 是 sm_70，CUDA 12.8 版已不支持）"
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126 \
    || pip install torch torchvision $TUNA
  echo "== 安装 SAM 2 组件（ultralytics）"
  pip install --no-deps ultralytics ultralytics-thop $TUNA
  pip install polars psutil matplotlib pillow requests pyyaml $TUNA
  python -c "import torch;print('CUDA 可用:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
  touch .ok_linux
fi

# 3) 模型权重放在固定目录（默认会放 /tmp，重启后会丢）
export LOCALAPPDATA="$HOME/.edp"
mkdir -p "$LOCALAPPDATA"
export CUDA_VISIBLE_DEVICES="${EDP_GPU:-0}"

echo "== 启动平台：服务器本机 http://127.0.0.1:${EDP_PORT:-8765}"
echo "   在 Windows 上另开一个命令行运行： ssh -L 8766:127.0.0.1:${EDP_PORT:-8765} root@<服务器IP>"
echo "   然后浏览器打开 http://127.0.0.1:8766"
python -c "import cryptography" 2>/dev/null || pip install cryptography $TUNA
python launch.py server
