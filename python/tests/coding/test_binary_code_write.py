"""Binary ``code_write`` channel — a DEV turn can persist real binary bytes
(a PNG/font/etc.) instead of only UTF-8 text.

This closes the structural gap that made a Godot game unfixable by the team: the
"placeholder pixel-art sprites" were written through the text-only channel, so
``tileset_overworld.png`` held ASCII text, not image bytes — Godot's importer
marked it ``valid=false`` and the game crashed at load, and no ``revise`` turn
could ever fix it because the channel could not carry a byte that wasn't UTF-8.

The DEV emits ``code_write`` with ``content_base64`` (or ``content`` +
``encoding: "base64"``); the controller decodes it to bytes and writes them
verbatim. Malformed base64 fails the tool event cleanly (no partial write). Text
writes are unchanged.
"""
import struct
import zlib
from base64 import b64encode
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.turn_controller import CodingTurnController
from errorta_council.coding.workspace import CodingWorkspace

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _minimal_png() -> bytes:
    """A genuinely valid 1x1 opaque-red PNG, built with correct chunk CRCs so it
    is a real image an engine can load (not a hand-waved byte blob)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    raw = b"\x00\xff\x00\x00"  # one filtered scanline: filter 0 + red pixel
    idat = zlib.compress(raw)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _store(project_id: str) -> LedgerStore:
    s = LedgerStore(project_id)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _workspace(project_id: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return ws


def test_content_base64_writes_real_bytes(tmp_errorta_home: Path) -> None:
    store = _store("binpng")
    task = store.add_task(title="add sprite", role="dev")
    ws = _workspace("binpng", store)
    png = _minimal_png()

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [{
            "tool": "code_write",
            "args": {"path": "assets/tile.png",
                     "content_base64": b64encode(png).decode("ascii")},
        }]},
    )

    assert summary.success_count == 1 and not summary.failed
    on_disk = (ws.root() / "assets" / "tile.png").read_bytes()
    # Byte-identical to the source image — NOT UTF-8 mangled, and a real PNG.
    assert on_disk == png
    assert on_disk.startswith(PNG_SIGNATURE)
    ev = store.list_tool_events()[0]
    assert ev["tool"] == "code_write" and ev["status"] == "succeeded"
    assert ev["intent"]["binary"] is True
    assert ev["intent"]["content_bytes"] == len(png)


def test_encoding_base64_on_content_field_also_works(tmp_errorta_home: Path) -> None:
    store = _store("binenc")
    task = store.add_task(title="add sprite", role="dev")
    ws = _workspace("binenc", store)
    png = _minimal_png()

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [{
            "tool": "code_write",
            "args": {"path": "b.png", "encoding": "base64",
                     "content": b64encode(png).decode("ascii")},
        }]},
    )

    assert summary.success_count == 1
    assert (ws.root() / "b.png").read_bytes() == png


def test_mime_wrapped_base64_decodes(tmp_errorta_home: Path) -> None:
    # Models often emit base64 wrapped across newlines; whitespace is stripped
    # before decoding so a wrapped payload still lands real bytes.
    store = _store("binwrap")
    task = store.add_task(title="add sprite", role="dev")
    ws = _workspace("binwrap", store)
    png = _minimal_png()
    wrapped = "\n".join(
        b64encode(png).decode("ascii")[i:i + 16]
        for i in range(0, len(b64encode(png).decode("ascii")), 16)
    )

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_write",
              "args": {"path": "w.png", "content_base64": wrapped}}]})

    assert summary.success_count == 1
    assert (ws.root() / "w.png").read_bytes() == png


def test_invalid_base64_fails_cleanly_without_writing(tmp_errorta_home: Path) -> None:
    store = _store("binbad")
    task = store.add_task(title="add sprite", role="dev")
    ws = _workspace("binbad", store)

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [{
            "tool": "code_write",
            "args": {"path": "bad.png", "content_base64": "not valid base64!!!"},
        }]},
    )

    assert summary.success_count == 0 and summary.failed
    assert not (ws.root() / "bad.png").exists()
    ev = store.list_tool_events()[0]
    assert ev["status"] == "failed" and "invalid_base64" in ev["error"]


def test_text_and_binary_in_one_turn(tmp_errorta_home: Path) -> None:
    store = _store("binmix")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("binmix", store)
    png = _minimal_png()

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [
            {"tool": "code_write", "args": {"path": "main.gd", "content": "extends Node\n"}},
            {"tool": "code_write",
             "args": {"path": "tile.png", "content_base64": b64encode(png).decode("ascii")}},
        ]},
    )

    assert summary.success_count == 2
    assert (ws.root() / "main.gd").read_text("utf-8") == "extends Node\n"
    assert (ws.root() / "tile.png").read_bytes() == png


def test_base64_wrapped_stub_over_text_file_is_still_blocked(tmp_errorta_home: Path) -> None:
    # A destructive "keep the file" stub must not dodge the F140 guard by being
    # emitted as base64: the guard runs on the incoming payload decoded to text
    # whenever the EXISTING file is text.
    from errorta_council.coding.schemas import TurnErrorCode

    store = _store("binstub")
    ws = _workspace("binstub", store)
    big_real_file = "extends Node\n" + "\n".join(f"func f{i}(): pass" for i in range(60))
    seed_branch = ws.start_task_branch("seed")
    ws.write_file("game.gd", big_real_file, task_id="seed")
    assert ws.merge_pr(seed_branch).get("merged")

    task = store.add_task(title="edit", role="dev")
    ws.start_task_branch(task.task_id)
    stub = b"PRESERVE_CURRENT_FILE_AND_APPLY stub"
    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_write",
              "args": {"path": "game.gd", "content_base64": b64encode(stub).decode("ascii")}}]})

    assert summary.success_count == 0 and summary.failed
    assert summary.failures == [("game.gd", TurnErrorCode.destructive_write_blocked.value)]


def test_binary_overwrite_is_not_blocked_as_destructive(tmp_errorta_home: Path) -> None:
    # The F140 destructive-write guard is a text-shape heuristic; a binary
    # overwrite of an existing asset (re-exporting a sprite) must not trip it.
    store = _store("binover")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("binover", store)
    ctl = CodingTurnController(store, ws)
    first = _minimal_png()

    ctl.execute_dev_turn(
        task=task, member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_write",
              "args": {"path": "s.png", "content_base64": b64encode(first).decode("ascii")}}]})
    # A different, larger valid PNG overwriting the first.
    second = first + b"\x00" * 64  # still starts with the PNG signature
    summary = ctl.execute_dev_turn(
        task=task, member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_write",
              "args": {"path": "s.png", "content_base64": b64encode(second).decode("ascii")}}]})

    assert summary.success_count == 1 and not summary.failed
    assert (ws.root() / "s.png").read_bytes() == second
