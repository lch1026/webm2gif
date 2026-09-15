#!/bin/bash
# （可选）安装 Homebrew 的 ffmpeg，并检查本机能否真正启用 VideoToolbox 加速。
#
# 说明：虚拟环境里 imageio-ffmpeg 自带的静态 ffmpeg（7.1）已经支持通用的
# `-hwaccel videotoolbox`，本项目能加速的部分它都能做，所以这个脚本不是必需品，
# 只在「想要一个全局 ffmpeg」时才需要。另外实测 M 系列芯片上硬件解码比软件解码
# 更慢（GIF 编码只能在 CPU 上完成），提速请优先调「并行」。
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null 2>&1; then
  cat >&2 <<'MSG'
未找到 Homebrew，请先安装：https://brew.sh
安装后重新运行本脚本，或手动执行：
  brew install ffmpeg
MSG
  exit 1
fi

echo "==> 安装 ffmpeg（Homebrew 版本，全局可用、编码器更全）"
echo "    首次安装会下载较多依赖，请耐心等待。"
brew install ffmpeg

PYTHON=".venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="$(command -v python3)"
fi

echo
echo "==> 检查硬件加速（会自动切换到刚装好的 ffmpeg）"
"${PYTHON}" -m webm2gif --check || true

echo
echo "想强制用硬件解码试试（自检说更慢时）："
echo "  图形界面 : 加速 = “VideoToolbox 解码”（或 “+ 缩放”）"
echo "  命令行   : ${PYTHON} -m webm2gif --cli clip.webm --hw videotoolbox"
echo
echo "注意：自检显示硬件解码慢于软件解码是正常的，真正有效的提速手段是并行（-j / 界面里的「并行」）。"
