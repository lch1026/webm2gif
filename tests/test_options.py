"""Quality presets and the ffmpeg arguments they produce."""

from __future__ import annotations

import pytest

from webm2gif.ffmpeg import MediaInfo
from webm2gif.options import (
    DEFAULT_PRESET_KEY,
    PRESETS,
    WIDTH_CHOICES,
    GifOptions,
    preset_for,
    unique_output_path,
)


def test_default_preset_exists():
    assert DEFAULT_PRESET_KEY in {preset.key for preset in PRESETS}
    assert preset_for("nope").key == DEFAULT_PRESET_KEY


def test_preset_defaults_drive_fps_and_width():
    options = GifOptions()
    preset = preset_for(DEFAULT_PRESET_KEY)
    assert options.effective_fps() == preset.fps
    assert options.target_width(4000) == preset.max_width


def test_small_source_is_not_upscaled():
    options = GifOptions(preset_key="high")
    assert options.target_width(320) == 0
    assert "scale=" not in options.filter_graph(320)


def test_explicit_width_wins_and_original_size_disables_scaling():
    assert GifOptions(width=1080).target_width(4000) == 1080
    assert GifOptions(width=0).target_width(4000) == 0


def test_preset_width_is_followed_when_configured():
    width_index = next(index for index, (_, value) in enumerate(WIDTH_CHOICES) if value == -1)
    assert WIDTH_CHOICES[width_index][1] == -1
    assert GifOptions(width=-1, preset_key="small").target_width(4000) == preset_for("small").max_width


def test_filter_graph_has_palette_pipeline():
    graph = GifOptions(preset_key="high").filter_graph(1920)
    assert graph.startswith("scale=720:-1:flags=lanczos,fps=20,split[s0][s1];")
    assert "palettegen=max_colors=256:stats_mode=diff[p]" in graph
    assert graph.endswith("[s1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")


def test_bayer_dither_keeps_its_scale_option():
    graph = GifOptions(preset_key="balanced").filter_graph(1920)
    assert "dither=bayer:bayer_scale=5" in graph


def test_loop_flag_and_command_layout():
    looped = GifOptions().build_command("ffmpeg", "in.webm", "out.gif", MediaInfo(10, 1920, 1080, "vp9"))
    once = GifOptions(loop=False).build_command("ffmpeg", "in.webm", "out.gif")

    assert looped[0] == "ffmpeg"
    assert looped[-3:] == ["-loop", "0", "out.gif"]
    assert once[-3:] == ["-loop", "-1", "out.gif"]
    assert "-progress" in looped and looped[looped.index("-progress") + 1] == "pipe:1"
    assert "-y" in looped


def test_arguments_are_passed_without_shell_quoting():
    command = GifOptions().build_command("ffmpeg", "in file.webm", "out file.gif")
    assert "in file.webm" in command and "out file.gif" in command
    assert not any(part.startswith("'") for part in command)


def test_unique_output_path_avoids_existing_files(tmp_path):
    existing = tmp_path / "clip.gif"
    existing.write_bytes(b"x")
    assert unique_output_path(existing) == tmp_path / "clip (2).gif"
    (tmp_path / "clip (2).gif").write_bytes(b"x")
    assert unique_output_path(existing) == tmp_path / "clip (3).gif"
    assert unique_output_path(tmp_path / "fresh.gif") == tmp_path / "fresh.gif"


def test_describe_reports_effective_settings():
    text = GifOptions(preset_key="small", fps=30, width=0, loop=False).describe(1920)
    assert "30 fps" in text and "原始尺寸" in text and "播放一次" in text


@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset.key)
def test_every_preset_builds_a_valid_graph(preset):
    graph = GifOptions(preset_key=preset.key).filter_graph(1920)
    assert graph.count(";") == 2
    assert "palettegen" in graph and "paletteuse" in graph
    assert "  " not in graph and not graph.startswith(",")


# --------------------------------------------------- hardware acceleration
def test_decoder_is_an_input_option_so_ffmpeg_builds_the_hw_pipeline():
    command = GifOptions(hardware="videotoolbox").build_command(
        "ffmpeg", "in.webm", "out.gif", MediaInfo(10, 1920, 1080, "vp9"), decoder="vp9_videotoolbox"
    )

    assert command[command.index("-c:v") + 1] == "vp9_videotoolbox"
    assert command.index("-c:v") < command.index("-i"), "VideoToolbox decoders must precede the input"
    assert "-init_hw_device" not in command


def test_software_decode_has_no_hardware_flags():
    command = GifOptions().build_command("ffmpeg", "in.webm", "out.gif", MediaInfo(10, 1920, 1080, "vp9"))
    assert "-c:v" not in command and "-init_hw_device" not in command


def test_gpu_scaling_creates_a_device_and_runs_before_the_palette_filters():
    command = GifOptions(preset_key="high", hardware="videotoolbox", gpu_scale=True).build_command(
        "ffmpeg",
        "in.webm",
        "out.gif",
        MediaInfo(10, 1920, 1080, "vp9"),
        decoder="vp9_videotoolbox",
        gpu_scale=True,
    )
    graph = command[command.index("-filter_complex") + 1]

    assert command[command.index("-init_hw_device") + 1] == "videotoolbox=vt"
    assert command[command.index("-filter_hw_device") + 1] == "vt"
    assert command.index("-filter_hw_device") < command.index("-i")
    # scale_vt only accepts VideoToolbox frames, so it is uploaded/downloaded
    # around the scaler, and the CPU filters follow afterwards.
    assert graph.startswith("scale_vt=w=720:h=406,hwdownload,format=yuv420p,fps=20,")
    assert graph.index("scale_vt") < graph.index("palettegen")


def test_gpu_scale_needs_both_dimensions_known():
    options = GifOptions(width=720, gpu_scale=True)
    assert options.gpu_scale_height(1920, 1080) == 406  # even, and not the -1 trick
    assert options.gpu_scale_height(1920, 1079) == 406
    assert options.can_gpu_scale(1920, 1080)
    assert not options.can_gpu_scale(1920, 0)
    assert not options.can_gpu_scale(320, 240), "nothing to scale when the source is small"


def test_gpu_scale_falls_back_to_the_cpu_scaler_without_metadata():
    # The height comes from the probe; when it is missing, scale_vt cannot be
    # configured (it refuses "-1"), so the CPU scaler is used instead.
    graph = GifOptions(gpu_scale=True).filter_graph(1920, gpu_scale=True, source_height=0)
    assert "scale_vt" not in graph and "scale=540:-1:flags=lanczos" in graph

    untouched = GifOptions(width=0, gpu_scale=True).filter_graph(1920, gpu_scale=True, source_height=1080)
    assert not untouched.startswith("scale") and untouched.startswith("fps=15,")


def test_hardware_mode_labels_are_user_facing():
    assert GifOptions(hardware="off").hardware_label == "关闭（纯 CPU）"
    assert "实测" in GifOptions(hardware="auto").hardware_label
    assert "VideoToolbox" in GifOptions(hardware="videotoolbox").hardware_label


def test_hardware_frames_are_downloaded_before_the_software_filters():
    # Dedicated VideoToolbox decoders emit hardware surfaces; palettegen
    # cannot read those, so they must be downloaded first.
    graph = GifOptions(preset_key="high").filter_graph(1920, hardware_frames=True)
    assert graph.startswith("hwdownload,scale=720:-1:flags=lanczos,fps=20,")

    # Without a scaling step something still has to convert the frame format.
    untouched = GifOptions(preset_key="high", width=0).filter_graph(1920, hardware_frames=True)
    assert untouched.startswith("hwdownload,format=yuv420p,fps=20,")

    # The GPU scaler downloads by itself, so no second hwdownload is added.
    gpu = GifOptions(preset_key="high", gpu_scale=True).filter_graph(
        1920, gpu_scale=True, source_height=1080, hardware_frames=True
    )
    assert gpu.startswith("scale_vt=w=720:h=406,hwdownload,format=yuv420p,fps=20,")
    assert gpu.count("hwdownload") == 1


def test_gpu_scaling_uploads_software_frames_first():
    """-hwaccel videotoolbox hands over software frames, so they are uploaded."""
    graph = GifOptions(preset_key="high").filter_graph(
        1920, gpu_scale=True, source_height=1080, hardware_frames=False
    )
    assert graph.startswith("hwupload,scale_vt=w=720:h=406,hwdownload,format=yuv420p,fps=20,")


def test_software_decoding_never_mentions_hardware_filters():
    graph = GifOptions(preset_key="high").filter_graph(1920)
    assert "hwdownload" not in graph and "hwupload" not in graph and "scale_vt" not in graph


def test_hwaccel_is_an_input_option_that_keeps_software_frames():
    command = GifOptions(preset_key="high").build_command(
        "ffmpeg", "in.webm", "out.gif", MediaInfo(10, 1920, 1080, "vp9"), hwaccel="videotoolbox"
    )
    graph = command[command.index("-filter_complex") + 1]

    assert command[command.index("-hwaccel") + 1] == "videotoolbox"
    assert command.index("-hwaccel") < command.index("-i"), "hwaccel must precede the input"
    assert "hwdownload" not in graph, "-hwaccel already hands over software frames"
    assert "-c:v" not in command


def test_decoder_argument_adds_the_download_to_the_command():
    command = GifOptions(preset_key="high").build_command(
        "ffmpeg", "in.webm", "out.gif", MediaInfo(10, 1920, 1080, "vp9"), decoder="vp9_videotoolbox"
    )
    graph = command[command.index("-filter_complex") + 1]
    assert graph.startswith("hwdownload,")
