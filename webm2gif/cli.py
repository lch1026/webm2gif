"""Headless conversion mode (``python -m webm2gif --cli …``).

Also used as a smoke test for packaged builds, since it exercises the same
conversion pipeline as the interface without needing a window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .converter import CANCELLED, DONE, FAILED, assign_outputs, convert_batch, summarize
from .converter import ConversionItem
from .discovery import expand_inputs
from .converter import auto_workers
from .ffmpeg import find_ffmpeg
from .hardware import (
    HARDWARE_AUTO,
    HARDWARE_MODE_KEYS,
    detect_capabilities,
    prefer_hardware_ffmpeg,
    run_self_test,
    use_gpu_scale,
)
from .options import DEFAULT_PRESET_KEY, PRESETS, GifOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webm2gif",
        description="将 .webm 文件转换为 .gif（同图形界面使用相同的转换流程）",
    )
    parser.add_argument("inputs", nargs="*", help="要转换的 .webm 文件或包含它们的文件夹")
    parser.add_argument("--cli", action="store_true", help="命令行模式（给出文件参数时即为默认模式）")
    parser.add_argument("-o", "--output", metavar="DIR", help="输出目录（默认为源文件所在目录）")
    parser.add_argument("--preset", choices=[preset.key for preset in PRESETS], help="画质预设")
    parser.add_argument("--fps", type=int, help="输出帧率")
    parser.add_argument("--width", help="输出宽度（像素，或 source 表示原始尺寸）")
    parser.add_argument("--once", action="store_true", help="只播放一次，不循环")
    parser.add_argument("-r", "--recursive", action="store_true", default=True, help="递归查找子文件夹（默认开启）")
    parser.add_argument("--check", action="store_true", help="只检查 ffmpeg 与硬件加速是否可用")
    parser.add_argument(
        "--hw",
        choices=HARDWARE_MODE_KEYS,
        default=HARDWARE_AUTO,
        help="硬件加速模式：auto = 自检实测不慢才用媒体引擎；videotoolbox = 强制使用；off = 纯 CPU",
    )
    parser.add_argument("--gpu-scale", action="store_true", help="让缩放也走 GPU（scale_vt，需自检通过；会退回软件解码）")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="并行转换的文件数（0 表示自动）")
    parser.add_argument("--version", action="version", version=f"WebM2GIF {__version__}")
    parser.add_argument("--ffmpeg", metavar="PATH", help="指定 ffmpeg 可执行文件")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结果")
    return parser


def describe_hardware(ffmpeg, mode: str) -> str:
    """Acceleration report for ``--check`` (listings plus a real self test)."""
    if ffmpeg is None:
        return "硬件加速: 未知（尚未找到 ffmpeg）"
    capabilities = detect_capabilities(ffmpeg.path)
    lines = [f"硬件加速: {capabilities.summary()}"]
    if mode != "off":
        result = run_self_test(ffmpeg.path, capabilities)
        lines.append(f"硬件自检: {result.summary()}")
        if result.tested and not result.usable and result.detail:
            lines.append(f"自检提示: {result.detail}")
    lines.append(capabilities.describe())
    return "\n".join(lines)


def describe_ffmpeg(ffmpeg) -> str:
    if ffmpeg is None:
        return (
            "ffmpeg : 未找到\n"
            "提示   : 在项目目录运行 scripts/setup.sh 安装（会下载到本地虚拟环境），\n"
            "         或使用 --ffmpeg /path/to/ffmpeg 指定，或用 brew install ffmpeg 安装。"
        )
    version = ffmpeg.version() or "(未知版本)"
    return f"ffmpeg : {ffmpeg.source}\n路径   : {ffmpeg.path}\n版本   : {version}"


def parse_width(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if value.lower() in {"source", "original", "原始"}:
        return 0
    if value.lower() in {"preset", "default"}:
        return None
    return int(value)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    ffmpeg = find_ffmpeg(preferred=args.ffmpeg)

    if args.check:
        print(f"WebM2GIF {__version__}")
        print(describe_ffmpeg(ffmpeg))
        if ffmpeg is not None:
            print(describe_hardware(ffmpeg, args.hw))
        return 0 if ffmpeg else 1

    if ffmpeg is None:
        print(describe_ffmpeg(None), file=sys.stderr)
        return 1

    capabilities = detect_capabilities(ffmpeg.path)
    selftest = run_self_test(ffmpeg.path, capabilities) if args.hw != "off" else None
    if args.hw != "off":
        ffmpeg = prefer_hardware_ffmpeg(ffmpeg, args.hw)
        capabilities = detect_capabilities(ffmpeg.path)
        selftest = run_self_test(ffmpeg.path, capabilities)
        if not args.quiet:
            print(f"ffmpeg : {ffmpeg.source} · {capabilities.summary()}")
            if selftest.tested:
                hint = f"（{selftest.detail}）" if selftest.detail and not selftest.decode_ok else ""
                print(f"加速度 : {selftest.summary()}{hint}")

    sources = expand_inputs(args.inputs, recursive=args.recursive)
    if not sources:
        print("没有找到 .webm 文件。用法示例：webm2gif --cli clip.webm -o ~/Desktop", file=sys.stderr)
        return 2

    options = GifOptions(
        preset_key=args.preset or DEFAULT_PRESET_KEY,
        fps=args.fps,
        width=parse_width(args.width),
        loop=not args.once,
        hardware=args.hw,
        gpu_scale=args.gpu_scale and use_gpu_scale(args.hw, True, capabilities, selftest),
    )

    items = [ConversionItem(source=path, output=path.with_suffix(".gif")) for path in sources]
    assign_outputs(items, args.output)

    total = len(items)
    interactive = sys.stdout.isatty() and not args.quiet

    def progress(item: ConversionItem) -> None:
        if args.quiet:
            return
        index = items.index(item) + 1
        line = f"[{index}/{total}] {item.source.name} → {item.output.name} {int(item.progress * 100):3d}%"
        if interactive:
            print("\r" + line[:120].ljust(120), end="", flush=True)
        elif item.status in (DONE, FAILED, CANCELLED) or item.progress >= 0.5:
            print(line)

    workers = args.jobs if args.jobs > 0 else auto_workers(total)
    convert_batch(
        ffmpeg.path,
        items,
        options,
        on_item_update=progress,
        max_workers=workers,
        capabilities=capabilities,
        selftest=selftest,
    )
    if interactive:
        print()

    summary = summarize(items)
    for item in items:
        if item.status == FAILED:
            print(f"✗ {item.source.name}: {item.message}", file=sys.stderr)

    print(f"完成 {summary.done} 个，失败 {summary.failed} 个" + (f"，取消 {summary.cancelled} 个" if summary.cancelled else ""))
    if args.output:
        print(f"输出目录：{Path(args.output).expanduser()}")
    return 0 if summary.failed == 0 else 1
