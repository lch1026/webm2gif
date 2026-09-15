"""The headless entry point."""

from __future__ import annotations

from webm2gif import cli


def test_check_reports_missing_ffmpeg_when_nothing_is_available(monkeypatch):
    monkeypatch.delenv("WEBM2GIF_FFMPEG", raising=False)
    monkeypatch.setattr("webm2gif.ffmpeg.resource_root", lambda: __import__("pathlib").Path("/nonexistent"))
    monkeypatch.setattr("webm2gif.ffmpeg._SYSTEM_CANDIDATES", ())
    monkeypatch.setattr("webm2gif.ffmpeg.shutil.which", lambda _name: None)
    monkeypatch.setattr("webm2gif.ffmpeg._imageio_ffmpeg_path", lambda: None)
    assert cli.main(["--check"]) == 1


def test_check_reports_the_selected_binary(fake_ffmpeg, capsys):
    assert cli.main(["--check", "--ffmpeg", str(fake_ffmpeg)]) == 0
    output = capsys.readouterr().out
    assert "9.9.9-fake" in output and str(fake_ffmpeg) in output


def test_conversion_from_the_command_line(fake_ffmpeg, sample_webm, tmp_path, capsys):
    output_dir = tmp_path / "gifs"
    output_dir.mkdir()

    code = cli.main(["--cli", str(sample_webm), "-o", str(output_dir), "--ffmpeg", str(fake_ffmpeg)])

    assert code == 0
    assert (output_dir / "clip.gif").read_bytes() == b"GIF89a"
    assert "完成 1 个" in capsys.readouterr().out


def test_missing_inputs_returns_usage_error(fake_ffmpeg, tmp_path, capsys):
    code = cli.main(["--cli", str(tmp_path / "empty"), "--ffmpeg", str(fake_ffmpeg)])
    assert code == 2
    assert "没有找到" in capsys.readouterr().err


def test_options_are_forwarded(fake_ffmpeg, sample_webm, tmp_path):
    code = cli.main(
        [
            "--cli",
            str(sample_webm),
            "-o",
            str(tmp_path),
            "--ffmpeg",
            str(fake_ffmpeg),
            "--preset",
            "small",
            "--fps",
            "24",
            "--width",
            "source",
            "--once",
        ]
    )
    assert code == 0


def test_parse_width_accepts_keywords():
    assert cli.parse_width(None) is None
    assert cli.parse_width("source") == 0
    assert cli.parse_width("preset") is None
    assert cli.parse_width("480") == 480


# ------------------------------------------------------------- acceleration
def conversion_argv(path):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "-filter_complex" in line]
    return lines[-1].split()


def test_check_reports_the_hardware_situation(fake_ffmpeg, capsys):
    assert cli.main(["--check", "--ffmpeg", str(fake_ffmpeg)]) == 0
    output = capsys.readouterr().out
    assert "VideoToolbox" in output
    assert "NPU" in output, "the report should say that the Neural Engine cannot be used"


def test_hardware_decoding_is_used_by_default(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))

    code = cli.main(["--cli", str(sample_webm), "-o", str(tmp_path), "--ffmpeg", str(fake_ffmpeg)])

    argv = conversion_argv(args_file)
    assert code == 0
    assert argv[argv.index("-hwaccel") + 1] == "videotoolbox"
    assert argv.index("-hwaccel") < argv.index("-i")


def test_hw_off_keeps_conversion_on_the_cpu(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))

    code = cli.main(["--cli", str(sample_webm), "-o", str(tmp_path), "--ffmpeg", str(fake_ffmpeg), "--hw", "off"])

    assert code == 0 and "-c:v" not in conversion_argv(args_file)


def test_gpu_scale_flag_reaches_the_filter_graph(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))

    code = cli.main(
        ["--cli", str(sample_webm), "-o", str(tmp_path), "--ffmpeg", str(fake_ffmpeg), "--gpu-scale"]
    )

    argv = conversion_argv(args_file)
    assert code == 0
    assert "-init_hw_device" in argv and "scale_vt=w=" in " ".join(argv)


def test_jobs_flag_converts_a_batch_in_parallel(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    sources = [sample_webm]
    for index in range(2):
        extra = tmp_path / f"clip{index}.webm"
        extra.write_bytes(b"more fake webm")
        sources.append(extra)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    arguments = [str(path) for path in sources]
    code = cli.main(["--cli", *arguments, "-o", str(output_dir), "--ffmpeg", str(fake_ffmpeg), "-j", "3"])

    assert code == 0
    assert sorted(path.name for path in output_dir.iterdir()) == ["clip.gif", "clip0.gif", "clip1.gif"]


def test_quiet_mode_stays_silent_about_hardware(monkeypatch, fake_ffmpeg, sample_webm, tmp_path, capsys):
    code = cli.main(["--cli", str(sample_webm), "-o", str(tmp_path), "--ffmpeg", str(fake_ffmpeg), "-q"])
    assert code == 0
    assert "VideoToolbox" not in capsys.readouterr().out
