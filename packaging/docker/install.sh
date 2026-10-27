#!/usr/bin/env bash
# 具身数据平台 · 客户服务器一键安装（离线）
# 用法：sudo ./install.sh [安装目录，默认 /opt/edp] [许可证文件]
set -euo pipefail
cd "$(dirname "$0")"
PREFIX="${1:-/opt/edp}"
LIC="${2:-}"
VERSION="$(cat VERSION)"
say() { echo -e "\n== $*"; }
die() { echo -e "\n[错误] $*" >&2; exit 1; }

say "检查环境"
command -v docker >/dev/null || die "没有安装 Docker。请先安装 Docker Engine 24 以上版本。"
docker compose version >/dev/null 2>&1 || die "没有 docker compose 插件（Docker 24 以上自带）。"
command -v nvidia-smi >/dev/null || die "没有检测到 NVIDIA 驱动（nvidia-smi）。"
nvidia-smi -L
docker info 2>/dev/null | grep -qi nvidia || echo "提示：docker info 里没看到 nvidia 运行时，若 worker 起不来请安装 nvidia-container-toolkit 并重启 docker。"
[ -f /etc/machine-id ] || die "缺少 /etc/machine-id（许可证绑定机器时要用）。"

say "导入镜像 edp:${VERSION}（几分钟）"
docker load -i "images/edp-${VERSION}.tar.gz"

say "安装到 ${PREFIX}"
mkdir -p "${PREFIX}"/{data,imports,exports,models}
cp docker-compose.yml edpctl "${PREFIX}/"
chmod +x "${PREFIX}/edpctl"
if [ ! -f "${PREFIX}/.env" ]; then
  sed -e "s#__VERSION__#${VERSION}#" -e "s#/opt/edp#${PREFIX}#g" env.example > "${PREFIX}/.env"
else
  sed -i "s#^EDP_VERSION=.*#EDP_VERSION=${VERSION}#" "${PREFIX}/.env"
  echo "保留已有的配置 ${PREFIX}/.env（只更新了版本号）"
fi
cp -r docs "${PREFIX}/" 2>/dev/null || true
[ -n "${LIC}" ] && cp "${LIC}" "${PREFIX}/data/license.lic" && echo "许可证已放到 ${PREFIX}/data/license.lic"

say "启动服务"
cd "${PREFIX}"
docker compose up -d web worker
for i in $(seq 1 60); do
  docker compose exec -T web curl -fs http://127.0.0.1:8765/api/me >/dev/null 2>&1 && break
  sleep 2
done

say "创建管理员账号"
if docker compose exec -T web python launch.py manage users | grep -q "管理员"; then
  echo "已有管理员账号，跳过"
else
  read -rp "管理员用户名 [admin]：" ADMIN; ADMIN="${ADMIN:-admin}"
  docker compose exec web python launch.py manage create-admin "${ADMIN}"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "安装完成"
echo "  浏览器打开：http://${IP:-<服务器IP>}:$(grep ^EDP_PORT .env | cut -d= -f2)"
echo "  原始数据放到：${PREFIX}/imports（网页“导入”里选）"
echo "  本机指纹（签发绑定本机的许可证用）：$(docker compose exec -T web python launch.py manage license fingerprint)"
echo "  日常运维：${PREFIX}/edpctl status|logs|restart|backup|stop"
