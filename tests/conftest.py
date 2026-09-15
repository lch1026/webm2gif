"""Shared fixtures: an isolated config directory and a fake ffmpeg binary."""

from __future__ import annotations

import stat
import textwrap

import pytest

#: The stand-in understands the ffmpeg flags this project uses, and can pretend
#: to be a VideoToolbox capable build so the hardware paths stay testable:
#:
#: * ``FAKE_FFMPEG_HW=0``            – advertise no hardware support at all
#: * ``FAKE_FFMPEG_HW_FAIL=1``       – VideoToolbox refuses to initialise
#: * ``FAKE_FFMPEG_HW_DECODE_FAIL=1``– only the hardware decode attempt fails (dedicated decoder or -hwaccel)
#: * ``FAKE_FFMPEG_FAIL=1``          – every conversion fails
#: * ``FAKE_FFMPEG_ARGS_FILE``       – append each argv line to that file
FAKE_FFMPEG = textwrap.dedent(
    """\
    #!/bin/sh
    # Minimal ffmpeg stand-in used by the test-suite.
    set -u

    all_args="$*"
    out=""
    input=""
    has_filter=0
    decoder=""
    prev=""
    for arg in "$@"; do
        if [ "$prev" = "-i" ]; then input="$arg"; fi
        if [ "$prev" = "-c:v" ]; then decoder="$arg"; fi
        if [ "$arg" = "-filter_complex" ]; then has_filter=1; fi
        out="$arg"
        prev="$arg"
    done

    if [ -n "${FAKE_FFMPEG_ARGS_FILE:-}" ]; then
        printf '%s\\n' "$all_args" >> "$FAKE_FFMPEG_ARGS_FILE"
    fi

    write_out() {
        if [ "$out" != "-" ] && [ -n "$out" ]; then
            printf 'GIF89a' > "$out"
        fi
    }

    hw_decoder_failed() {
        case "$decoder" in
            *_videotoolbox*) return 0 ;;
        esac
        # Conversions ask for the media engine through the generic input option;
        # the self-test runs ("-f null") are answered earlier and never get here.
        case "$all_args" in
            *"-hwaccel videotoolbox"*) return 0 ;;
        esac
        return 1
    }

    case "$all_args" in
        *-hwaccels*)
            echo "Hardware acceleration methods:"
            if [ "${FAKE_FFMPEG_HW:-1}" = "1" ]; then echo "videotoolbox"; fi
            exit 0
            ;;
        *-decoders*)
            echo "Decoders:"
            echo " V..... = Video"
            echo " V....D vp9                  Google VP9"
            echo " V....D vp8                  On2 VP8"
            if [ "${FAKE_FFMPEG_HW:-1}" = "1" ]; then
                echo " V....D vp9_videotoolbox     Google VP9 (VideoToolbox) (codec vp9)"
                echo " V....D vp8_videotoolbox     On2 VP8 (VideoToolbox) (codec vp8)"
            fi
            exit 0
            ;;
        *-filters*)
            echo "Filters:"
            echo " ... fps               V->V       Force constant framerate."
            echo " ... scale             V->V       Scale the input video size."
            if [ "${FAKE_FFMPEG_HW:-1}" = "1" ]; then
                echo " ... scale_vt          V->V       Scale video using VideoToolbox."
            fi
            exit 0
            ;;
        *-version*)
            echo "ffmpeg version 9.9.9-fake Copyright (c) 2000-2099 the FFmpeg developers"
            exit 0
            ;;
    esac

    if [ "${FAKE_FFMPEG_HW_FAIL:-0}" = "1" ]; then
        case "$all_args" in
            *videotoolbox*)
                echo "Failed setup for format videotoolbox_vld: hwaccel initialisation returned error" >&2
                exit 1
                ;;
        esac
    fi

    # The self-test generates its sample clip through the "lavfi" device.
    case "$all_args" in
        *lavfi*)
            write_out
            exit 0
            ;;
    esac

    if [ "$has_filter" = "0" ]; then
        # Decoding to /dev/null ("… -f null -") always works: that is how the
        # self test and the benchmark measure a decode.
        case "$all_args" in
            *"-f null"*) exit 0 ;;
        esac

        cat >&2 <<'PROBE'
    ffmpeg version 9.9.9-fake
      Duration: 00:00:12.50, start: 0.000000, bitrate: 800 kb/s
      Stream #0:0: Video: vp9 (Profile 0), yuv420p(tv), 1920x1080, SAR 1:1 DAR 16:9, 30 fps, 30 tbr, 1k tbn
    At least one output file must be specified
    PROBE
        exit 1
    fi

    if [ "${FAKE_FFMPEG_FAIL:-0}" = "1" ]; then
        echo "Invalid data found when processing input" >&2
        exit 1
    fi

    if [ "${FAKE_FFMPEG_HW_DECODE_FAIL:-0}" = "1" ] && hw_decoder_failed; then
        echo "Failed to initialise VideoToolbox session" >&2
        exit 1
    fi

    steps="${FAKE_FFMPEG_STEPS:-20}"
    delay="${FAKE_FFMPEG_STEP_DELAY:-0.02}"
    i=0
    while [ "$i" -lt "$steps" ]; do
        i=$((i + 1))
        printf 'frame=%s\\nfps=25.0\\nout_time=00:00:%02d.%06d\\nprogress=continue\\n' \\
            "$i" "$((i / 2))" "$((i % 2 * 500000))"
        sleep "$delay"
    done
    printf 'progress=end\\n'
    write_out
    """
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep settings out of the real user library."""
    monkeypatch.setenv("WEBM2GIF_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


@pytest.fixture
def fake_ffmpeg(tmp_path):
    """An executable that behaves enough like ffmpeg for our pipeline."""
    path = tmp_path / "ffmpeg-fake"
    path.write_text(FAKE_FFMPEG, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def sample_webm(tmp_path):
    path = tmp_path / "clip.webm"
    path.write_bytes(b"\x1a\x45\xdf\xa3" + b"fake webm payload" * 8)
    return path
