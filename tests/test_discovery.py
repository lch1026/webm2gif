"""Finding inputs, and where their GIFs should go."""

from __future__ import annotations

from pathlib import Path

from webm2gif.converter import ConversionItem, assign_outputs, plan_items, summarize
from webm2gif.discovery import discover_webm_files, expand_inputs, is_webm, output_for


def make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "movies"
    (root / "nested").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a.webm").write_bytes(b"a")
    (root / "b.WEBM").write_bytes(b"b")
    (root / "notes.txt").write_text("skip me")
    (root / ".secret.webm").write_bytes(b"hidden file")
    (root / "nested" / "c.webm").write_bytes(b"c")
    (root / ".hidden" / "d.webm").write_bytes(b"d")
    return root


def test_discovers_webm_recursively_and_skips_hidden(tmp_path):
    root = make_tree(tmp_path)
    found = {path.name for path in discover_webm_files(root)}
    assert found == {"a.webm", "b.WEBM", "c.webm"}


def test_non_recursive_discovery_stays_on_top_level(tmp_path):
    root = make_tree(tmp_path)
    found = {path.name for path in discover_webm_files(root, recursive=False)}
    assert found == {"a.webm", "b.WEBM"}


def test_expand_inputs_accepts_files_and_folders_without_duplicates(tmp_path):
    root = make_tree(tmp_path)
    paths = [str(root), str(root / "a.webm"), str(root / "nested" / "c.webm")]
    names = [path.name for path in expand_inputs(paths)]
    assert sorted(names) == ["a.webm", "b.WEBM", "c.webm"]


def test_expand_inputs_ignores_non_webm_files(tmp_path):
    webm = tmp_path / "clip.webm"
    webm.write_bytes(b"1")
    (tmp_path / "clip.mp4").write_bytes(b"2")
    (tmp_path / "clip.gif").write_bytes(b"3")

    assert [path.name for path in expand_inputs([str(tmp_path / "clip.mp4"), str(webm)])] == ["clip.webm"]


def test_is_webm_is_case_insensitive(tmp_path):
    assert is_webm(tmp_path / "clip.webm")
    assert is_webm(tmp_path / "CLIP.WEBM")
    assert not is_webm(tmp_path / "clip.mp4")


def test_output_for_uses_source_folder_or_custom_folder(tmp_path):
    source = tmp_path / "clip.webm"
    assert output_for(source) == tmp_path / "clip.gif"
    assert output_for(source, tmp_path / "out") == tmp_path / "out" / "clip.gif"


def test_assign_outputs_numbers_collisions(tmp_path):
    first = tmp_path / "a" / "clip.webm"
    second = tmp_path / "b" / "clip.webm"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    items = [ConversionItem(source=first, output=Path()), ConversionItem(source=second, output=Path())]
    assign_outputs(items, tmp_path / "out")
    names = [item.output.name for item in items]
    assert names == ["clip.gif", "clip (2).gif"]


def test_existing_output_files_are_never_overwritten(tmp_path):
    source = tmp_path / "clip.webm"
    source.write_bytes(b"x")
    (tmp_path / "clip.gif").write_bytes(b"old")

    items = plan_items([source])
    assert items[0].output.name == "clip (2).gif"


def test_summarize_counts_each_status(tmp_path):
    items = plan_items([tmp_path / f"{i}.webm" for i in range(4)])
    items[0].status = "done"
    items[1].status = "failed"
    items[2].status = "cancelled"
    summary = summarize(items)
    assert (summary.done, summary.failed, summary.cancelled, summary.pending) == (1, 1, 1, 1)
    assert summary.total == 4
