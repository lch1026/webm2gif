#!/usr/bin/env python3
"""Measure how much each acceleration strategy helps on *this* Mac.

    .venv/bin/python tools/benchmark.py                 # 默认 3 秒 720p 素材
    .venv/bin/python tools/benchmark.py --duration 5 --jobs 4
    .venv/bin/python tools/benchmark.py --clip clip.webm

The script generates a synthetic WebM clip (or uses ``--clip``), then converts
it with every available strategy and prints wall-clock times. Nothing is
guessed: an arm is only offered when the current ffmpeg really supports it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webm2gif.converter import DONE, ConversionItem, auto_workers, convert_batch  # noqa: E402
from webm2gif.ffmpeg import find_ffmpeg  # noqa: E402
from webm2gif.hardware import (  # noqa: E402
    HARDWARE_OFF,
    HARDWARE_VIDEOTOOLBOX,
    detect_capabilities,
    prefer_hardware_ffmpeg,
    run_self_test,
)
from webm2gif.options import DEFAULT_PRESET_KEY, PRESETS, GifOptions  # noqa: E402


@dataclass
class Arm:
    """One strategy: a label, the options to use and the parallelism."""

    label: str
    hardware: str
    gpu_scale: bool
    workers: int
    detail: str = ""
    #: ``False`` when this build cannot run the arm at all (shown, not measured).
    available: bool = True


def make_clip(ffmpeg_path: str, destination: Path, duration: float, width: int, height: int) -> bool:
    """Encode a small VP9 clip so the benchmark is repeatable."""
    command = [
        ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
        "-c:v", "libvpx-vp9", "-b:v", "3M", "-pix_fmt", "yuv420p", str(destination),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"无法生成测试素材：{error}", file=sys.stderr)
        return False
    if completed.returncode != 0 or not destination.exists():
        print(f"无法生成测试素材：{(completed.stderr or '').strip().splitlines()[-1:]}", file=sys.stderr)
        return False
    return True


def build_arms(capabilities, selftest, workers: int) -> list[Arm]:
    """Only offer strategies that this ffmpeg actually supports.

    Every arm converts the same number of files, so the totals are comparable;
    ``workers`` only changes how many of them run at once.
    """
    parallel = workers
    arms = [
        Arm("CPU 解码 · 串行", HARDWARE_OFF, False, 1),
        Arm(f"CPU 解码 · 并行 {parallel}", HARDWARE_OFF, False, parallel),
    ]
    if not capabilities.can_decode_webm_in_hardware:
        missing = "当前 ffmpeg 未启用 VideoToolbox（可运行 scripts/install_ffmpeg.sh）"
        arms.append(Arm("VideoToolbox 解码 · 串行", HARDWARE_VIDEOTOOLBOX, False, 1, missing, available=False))
        arms.append(
            Arm(f"VideoToolbox 解码 · 并行 {parallel}", HARDWARE_VIDEOTOOLBOX, False, parallel, missing, available=False)
        )
        return arms

    arms.append(Arm("VideoToolbox 解码 · 串行", HARDWARE_VIDEOTOOLBOX, False, 1))
    arms.append(Arm(f"VideoToolbox 解码 · 并行 {parallel}", HARDWARE_VIDEOTOOLBOX, False, parallel))
    note = "GPU 缩放只加速缩放那一小步，本机可能反而更慢"
    if not capabilities.gpu_scale_available or selftest is None or not selftest.scale_ok:
        note = "本机自检未通过（scale_vt 不可用），会自动回退到 CPU 缩放"
    arms.append(Arm("VideoToolbox + GPU 缩放", HARDWARE_VIDEOTOOLBOX, True, parallel, note))
    return arms


def prepare_clips(clip: Path, folder: Path, count: int) -> list[Path]:
    """``count`` copies of the sample, so every arm has the same workload."""
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    payload = clip.read_bytes()
    for index in range(count):
        target = folder / f"clip-{index}.webm"
        if index == 0:
            target.write_bytes(payload)
        else:
            target.write_bytes(payload)
        sources.append(target)
    return sources


def run_arm(arm: Arm, ffmpeg_path: str, sources, folder: Path, options: GifOptions, capabilities, selftest):
    """Convert every source with ``arm``'s strategy; return the wall time."""
    folder.mkdir(parents=True, exist_ok=True)
    items = [
        ConversionItem(source=source, output=folder / f"{source.stem}.gif") for source in sources
    ]
    arm_options = options.with_changes(hardware=arm.hardware, gpu_scale=arm.gpu_scale)
    started = time.monotonic()
    convert_batch(
        ffmpeg_path,
        items,
        arm_options,
        max_workers=arm.workers,
        capabilities=capabilities,
        selftest=selftest,
    )
    elapsed = time.monotonic() - started
    failures = [item for item in items if item.status != DONE]
    return elapsed, failures, items[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="对比 CPU / VideoToolbox 的转换速度")
    parser.add_argument("--clip", help="用于测试的 .webm（默认自动生成）")
    parser.add_argument("--duration", type=float, default=3.0, help="生成素材的时长（秒）")
    parser.add_argument("--width", type=int, default=1280, help="生成素材的宽度")
    parser.add_argument("--height", type=int, default=720, help="生成素材的高度")
    parser.add_argument("--preset", default=DEFAULT_PRESET_KEY, choices=[p.key for p in PRESETS])
    parser.add_argument("-j", "--jobs", type=int, default=0, help="并行数（0 表示自动）")
    parser.add_argument("--files", type=int, default=0, help="每个方案转换的文件数（0 表示等于并行数）")
    parser.add_argument("--repeat", type=int, default=1, help="每个方案重复次数，取最快的一次")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本处理")
    args = parser.parse_args()

    ffmpeg = prefer_hardware_ffmpeg(find_ffmpeg(), HARDWARE_VIDEOTOOLBOX)
    if ffmpeg is None:
        print("未找到 ffmpeg，请先运行 scripts/setup.sh", file=sys.stderr)
        return 1

    capabilities = detect_capabilities(ffmpeg.path)
    selftest = run_self_test(ffmpeg.path, capabilities)
    options = GifOptions(preset_key=args.preset)

    if not args.json:
        print(f"ffmpeg : {ffmpeg.source} · {ffmpeg.path}")
        print(f"素材   : {args.clip or f'testsrc2 {args.width}x{args.height} {args.duration:g}s'}")
        print(f"预设   : {options.preset.label}（{options.describe()}）")
        print(f"加速   : {capabilities.summary()}")
        print(f"自检   : {selftest.summary()}" + (f"（{selftest.detail}）" if selftest.detail else ""))
        print()

    with tempfile.TemporaryDirectory(prefix="webm2gif-bench-") as workspace:
        root = Path(workspace)
        if args.clip:
            clip = Path(args.clip).expanduser().resolve()
            if not clip.is_file():
                print(f"找不到素材：{clip}", file=sys.stderr)
                return 2
        else:
            clip = root / "bench.webm"
            if not make_clip(ffmpeg.path, clip, args.duration, args.width, args.height):
                return 2

        workers = args.jobs if args.jobs > 0 else auto_workers()
        files = max(1, args.files or workers)
        sources = prepare_clips(clip, root / "clips", files)

        if not args.json:
            print(f"工作负载: {files} 个文件 · 每个方案都完整转换这些文件")
            print()

        arms = build_arms(capabilities, selftest, workers)
        results = []
        baseline = None
        for index, arm in enumerate(arms):
            if not arm.available:
                results.append({"arm": arm, "seconds": None, "per_file": None, "note": arm.detail})
                continue
            best = None
            failures = []
            probe = None
            for attempt in range(max(1, args.repeat)):
                elapsed, failures, probe = run_arm(
                    arm,
                    ffmpeg.path,
                    sources,
                    root / f"run-{index}-{attempt}",
                    options,
                    capabilities,
                    selftest,
                )
                best = elapsed if best is None else min(best, elapsed)
            if baseline is None and arm.workers == 1 and arm.hardware == HARDWARE_OFF:
                baseline = best
            note = arm.detail
            if failures:
                note = (note + " " if note else "") + f"{len(failures)} 个失败：{failures[0].message}"
            results.append(
                {
                    "arm": arm,
                    "seconds": best,
                    "per_file": best / files if files else None,
                    "note": note,
                    "hardware": probe.hardware if probe else "",
                }
            )

    if args.json:
        print(
            json.dumps(
                {
                    "ffmpeg": {"path": ffmpeg.path, "source": ffmpeg.source},
                    "capabilities": capabilities.summary(),
                    "selftest": selftest.summary(),
                    "workload": {"files": max(1, args.files or (args.jobs or auto_workers()))},
                    "results": [
                        {
                            "label": row["arm"].label,
                            "seconds": row["seconds"],
                            "seconds_per_file": row["per_file"],
                            "speedup": (baseline / row["seconds"]) if row["seconds"] and baseline else None,
                            "codec": row.get("hardware", ""),
                            "note": row["note"],
                        }
                        for row in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"{'方案':<30}{'总耗时':>9}{'每个文件':>10}{'加速比':>9}   说明")
    print("-" * 88)
    for row in results:
        arm, seconds, note = row["arm"], row["seconds"], row["note"]
        if seconds is None:
            print(f"{arm.label:<30}{'—':>9}{'—':>10}{'—':>9}   {note}")
            continue
        speedup = f"{baseline / seconds:.2f}×" if baseline else "—"
        detail = note or row.get("hardware") or "—"
        print(f"{arm.label:<30}{seconds:>8.2f}s{row['per_file']:>9.2f}s{speedup:>9}   {detail}")
    print()
    print("提示：GIF 的调色板生成与编码只能在 CPU 上完成，硬件加速只作用于解码（与可选的缩放）；")
    print("      NPU（神经网络引擎）无法被 ffmpeg 调用，所以不要期待“整段流程”都被 GPU 接管。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
