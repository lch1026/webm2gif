# WebM2GIF — 开发与打包快捷命令
PYTHON := .venv/bin/python
APP    := WebM2GIF.app

.PHONY: help setup run cli test lint preview icon app standalone check bench ffmpeg clean

help:
	@echo "make setup       创建本地虚拟环境并安装依赖"
	@echo "make run         启动图形界面"
	@echo "make cli FILE=x  命令行转换（示例：make cli FILE=clip.webm）"
	@echo "make test        运行测试"
	@echo "make lint        代码风格检查（flake8，与 CI 一致）"
	@echo "make preview     生成界面预览 PNG（docs/preview.png）"
	@echo "make icon        从 packaging/AppIcon.png 生成图标 build/AppIcon.icns"
	@echo "make app         构建 $(APP)（轻量版，复用 .venv）"
	@echo "make standalone  构建完全独立的 $(APP)（需要 PyInstaller）"
	@echo "make check       检查 ffmpeg 与硬件加速状态"
	@echo "make bench       对比 CPU / VideoToolbox / 并行的实际耗时"
	@echo "make ffmpeg      安装带 VideoToolbox 解码器的 ffmpeg（Homebrew）"
	@echo "make clean       清理构建产物"

setup:
	bash scripts/setup.sh

run:
	$(PYTHON) -m webm2gif

cli:
	$(PYTHON) -m webm2gif --cli $(FILE) $(ARGS)

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m flake8

preview:
	$(PYTHON) tools/preview_ui.py --demo docs/preview.png

icon:
	$(PYTHON) tools/make_icon.py --output build

app: icon
	$(PYTHON) tools/build_app.py

standalone: icon
	$(PYTHON) tools/build_app.py --standalone

check:
	$(PYTHON) -m webm2gif --check

bench:
	$(PYTHON) tools/benchmark.py

ffmpeg:
	bash scripts/install_ffmpeg.sh

clean:
	rm -rf build dist out .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
