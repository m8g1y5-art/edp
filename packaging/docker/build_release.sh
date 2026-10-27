#!/usr/bin/env bash
# 在构建机（能联网、装了 Docker 的 Linux 服务器，比如公司的 V100）上生成客户安装包。
# 用法：在仓库根目录执行  bash packaging/docker/build_release.sh
# 产出：dist/edp-<版本>-linux.tar.gz（含镜像、compose、安装脚本、文档），拷给客户即可离线安装
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION="$(python3 -c 'import re;print(re.search(r"VERSION = \"(.+?)\"", open("app/version.py").read()).group(1))')"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
OUT="dist/edp-${VERSION}"
echo "== 构建 edp:${VERSION}（提交 ${COMMIT}）"
[ -z "$(git status --porcelain 2>/dev/null)" ] || echo "提示：工作区有未提交的改动，安装包会包含这些改动"
docker build -f packaging/docker/Dockerfile -t "edp:${VERSION}" --label "edp.commit=${COMMIT}" .
rm -rf "${OUT}"; mkdir -p "${OUT}/images" "${OUT}/docs"
echo "== 导出镜像"
docker save "edp:${VERSION}" | gzip -1 > "${OUT}/images/edp-${VERSION}.tar.gz"
cp packaging/docker/{docker-compose.yml,env.example,install.sh,edpctl} "${OUT}/"
cp packaging/docs/*.md "${OUT}/docs/" 2>/dev/null || true
echo "${VERSION}" > "${OUT}/VERSION"
echo "${COMMIT}" > "${OUT}/COMMIT"
chmod +x "${OUT}/install.sh" "${OUT}/edpctl"
( cd "${OUT}" && sha256sum images/*.tar.gz > SHA256SUMS )
tar -C dist -czf "dist/edp-${VERSION}-linux.tar.gz" "edp-${VERSION}"
ls -lh "dist/edp-${VERSION}-linux.tar.gz"
echo "完成。客户机器上：tar xzf edp-${VERSION}-linux.tar.gz && cd edp-${VERSION} && sudo ./install.sh /opt/edp 许可证.lic"
