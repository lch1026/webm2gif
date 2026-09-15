#!/bin/bash
# Prepare the local development environment (virtualenv + dependencies).
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

PYTHON_BIN="${PYTHON:-}"
if [ -z "${PYTHON_BIN}" ]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then PYTHON_BIN="${candidate}"; break; fi
  done
fi
if [ -z "${PYTHON_BIN}" ]; then
  echo "找不到 python3，请先安装 Python 3.10+（例如 brew install python@3.12）" >&2
  exit 1
fi

echo "==> 使用解释器: $(${PYTHON_BIN} -V) ($(command -v "${PYTHON_BIN}"))"

if [ ! -d .venv ]; then
  echo "==> 创建虚拟环境 .venv"
  # --system-site-packages lets an existing PyObjC installation be reused when
  # the machine is offline; pip still installs the pinned requirements below.
  "${PYTHON_BIN}" -m venv --system-site-packages .venv
fi

echo "==> 安装依赖"
.venv/bin/python -m pip install --upgrade pip >/dev/null
if ! .venv/bin/python -m pip install -r requirements.txt; then
  echo "依赖安装失败。若只是缺少网络，可先手动安装：" >&2
  echo "  brew install ffmpeg" >&2
  echo "  .venv/bin/python -m pip install pyobjc-framework-Cocoa imageio-ffmpeg" >&2
  exit 1
fi

echo "==> 检查 ffmpeg"
.venv/bin/python -m webm2gif --check || true

echo
echo "提示：转换速度主要靠「并行」（界面里的「并行」或命令行的 -j）。"
echo "      imageio 自带的 ffmpeg 已支持 VideoToolbox 硬件解码，但实测它比软件解码慢，"
echo "      所以「自动」模式通常保持 CPU；想要全局 ffmpeg 才需要 bash scripts/install_ffmpeg.sh"
echo
echo "完成！接下来可以："
echo "  启动界面      : .venv/bin/python -m webm2gif"
echo "  构建 .app     : .venv/bin/python tools/build_app.py"
echo "  运行测试      : .venv/bin/python -m pytest"
echo
echo "项目目录: ${PROJECT_DIR}"
