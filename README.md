# 百行 · 具身数据平台

把头戴相机录下的人手操作（彩色 + 深度 + 手部关键点 + 位姿），自动加工成带分段、接触事件和语义标注的训练数据（LeRobot v3.0）。

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/` | 程序：网页接口 `server.py`、处理管线 `pipeline.py`、物体与接触 `objects.py`、导入识别 `scan.py`、大模型 `vlm.py` / `batchq.py` |
| `app/auth.py` `app/license.py` `app/migrations.py` | 部署版：账号与权限、许可证、数据库版本化升级 |
| `app/static/` | 前端（原生 JS，无构建步骤） |
| `launch.py` | 统一入口：`server` / `worker` / `manage` |
| `packaging/compile.py` | 把核心模块编译成 .so / .pyd |
| `packaging/docker/` | 服务器镜像、compose、安装与运维脚本、发布包构建 |
| `packaging/windows/` | Windows 单机安装包构建 |
| `packaging/docs/` | 随安装包发给客户的文档 |
| `tools/license_tool.py` | 许可证签发（内部使用，不随安装包发出） |
| `tools/mage/` | 本机 Mage-VL 语义服务 |
| `tests/smoke.py` | 冒烟测试 |

## 两种运行模式

| | 单机（默认） | 服务器 |
| --- | --- | --- |
| 开启方式 | 不设或 `EDP_MODE=single` | `EDP_MODE=server` |
| 登录 | 不需要 | 需要，管理员 / 标注员 |
| 处理在哪跑 | 网页服务进程里 | 独立的 `python launch.py worker`，可多个 |
| 监听地址 | 127.0.0.1 | 0.0.0.0（可用 `EDP_HOST` 改） |

## 本地开发

```bash
pip install -r requirements.txt
python launch.py server                 # 单机模式，浏览器打开 http://127.0.0.1:8765
# 服务器模式：
EDP_MODE=server python launch.py manage create-admin admin
EDP_MODE=server python launch.py server &
EDP_MODE=server python launch.py worker
```

Windows 上直接双击 `run.bat`。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `EDP_DATA` | 数据目录（默认 `./data`） |
| `EDP_MODE` | `single` / `server` |
| `EDP_PORT` `EDP_HOST` | 监听端口和地址 |
| `EDP_MODELS` | SAM 2 等模型权重目录 |
| `EDP_IMPORT_ROOTS` | 服务器模式允许导入的目录，多个用 `:` 分隔 |
| `EDP_EXPORTS` | 导出数据集的目录 |
| `EDP_ALLOW_EXTERNAL` | `0` 禁止外部大模型接口（数据不出内网），`1` 允许，不设由管理员在网页里决定 |
| `EDP_WORKER_ID` | worker 名称（多个 worker 时区分） |
| `EDP_LICENSE` | 许可证文件路径（默认 `数据目录/license.lic`） |

## 改数据库结构

不要直接改表。在 `app/migrations.py` 的 `MIGRATIONS` 末尾加一个新版本（版本号连续递增），已发布的版本函数不要再改。程序启动时会自动升级，升级前会把数据库备份到 `data/backups/`。

## 发版流程

1. 改 `app/version.py` 的版本号，提交，打标签：`git tag v1.1.0`。
2. 服务器安装包：在构建机上执行 `bash packaging/docker/build_release.sh`，产出 `dist/edp-<版本>-linux.tar.gz`。
3. Windows 安装包：在 Windows 构建机上执行 `powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1`。
4. 发布前在编译后的目录上跑一遍冒烟测试：`python tests/smoke.py --root <发布目录>`。

## 许可证签发

私钥只保存在我方，绝不放进仓库。

```bash
python tools/license_tool.py issue --key 私钥.pem --customer "客户名" --days 365 --gpus 4 \
       [--machine 客户机器指纹] [--edition trial] --out 客户.lic
```

`--perpetual` 表示永久授权（买断）。功能模块用 `--modules` 指定，默认 `base,fine,semantic,lerobot`。换密钥用 `keygen`，换完要把新公钥写进 `app/license.py`，旧许可证会全部失效。
