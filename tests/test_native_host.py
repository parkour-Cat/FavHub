import ast
import io
import json
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

from favhub import native_host
from favhub.config import FavHubPaths
from favhub.native_host import (
    MAX_NATIVE_MESSAGE_BYTES,
    NativeHostError,
    iter_native_messages,
    load_runtime_descriptor,
    run_native_host,
    write_native_message,
)

SOURCE = Path(native_host.__file__)


def encode_native(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def decode_native(raw: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        (length,) = struct.unpack("<I", raw[offset : offset + 4])
        offset += 4
        messages.append(json.loads(raw[offset : offset + length].decode("utf-8")))
        offset += length
    return messages


# -- boundary: the relay must stay a relay ------------------------------------


def test_the_relay_never_imports_favhub_state_modules() -> None:
    """The MCP process owns the data root; a second opener would be locked out.

    This is a hard architectural boundary, so it is asserted on the source
    rather than trusted to review.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    forbidden = {
        "favhub.application",
        "favhub.database",
        "favhub.sync_module",
        "favhub.library",
        "favhub.item_store",
        "favhub.browser_ingest",
        "favhub.x_parsers",
        "favhub.bilibili_parsers",
        "favhub.zhihu_parsers",
    }
    assert not (imported & forbidden)


def test_the_relay_source_mentions_no_credential_names() -> None:
    text = SOURCE.read_text(encoding="utf-8").lower()
    for forbidden in ("cookie", "sessdata", "z_c0", "ct0", "bearer"):
        assert forbidden not in text


# -- framing ------------------------------------------------------------------


def test_messages_are_read_as_length_prefixed_json() -> None:
    stream = io.BytesIO(encode_native({"type": "session.claim"}) + encode_native({"type": "ping"}))
    assert [message["type"] for message in iter_native_messages(stream)] == [
        "session.claim",
        "ping",
    ]


def test_clean_eof_ends_the_stream_without_an_error() -> None:
    assert list(iter_native_messages(io.BytesIO(b""))) == []


def test_a_truncated_header_is_rejected() -> None:
    with pytest.raises(NativeHostError):
        list(iter_native_messages(io.BytesIO(b"\x01\x02")))


def test_a_truncated_body_is_rejected() -> None:
    stream = io.BytesIO(struct.pack("<I", 64) + b'{"type":')
    with pytest.raises(NativeHostError):
        list(iter_native_messages(stream))


def test_a_non_object_message_is_rejected() -> None:
    body = b"[1, 2, 3]"
    stream = io.BytesIO(struct.pack("<I", len(body)) + body)
    with pytest.raises(NativeHostError):
        list(iter_native_messages(stream))


def test_malformed_json_is_rejected() -> None:
    body = b"not json"
    stream = io.BytesIO(struct.pack("<I", len(body)) + body)
    with pytest.raises(NativeHostError):
        list(iter_native_messages(stream))


def test_an_oversize_frame_is_refused_without_ending_the_relay() -> None:
    oversize = MAX_NATIVE_MESSAGE_BYTES + 1
    good = json.dumps({"type": "session.heartbeat"}).encode("utf-8")
    stream = io.BytesIO(
        struct.pack("<I", oversize) + b"x" * oversize + struct.pack("<I", len(good)) + good
    )

    messages = list(iter_native_messages(stream))

    # The oversize frame is reported, and the frame after it still arrives. A
    # relay that raised here took the extension's only channel down with it,
    # so the run could not even report why it had stopped.
    assert len(messages) == 2
    assert messages[0] == {}
    assert messages[1]["type"] == "session.heartbeat"


def test_an_oversize_frame_is_never_held_in_memory() -> None:
    reads: list[int] = []

    class CountingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            reads.append(size)
            return super().read(size)

    oversize = MAX_NATIVE_MESSAGE_BYTES + 1
    stream = CountingStream(struct.pack("<I", oversize) + b"x" * oversize)

    list(iter_native_messages(stream))

    # The whole point of the cap is that an inflated header cannot make the
    # relay allocate what it claims; the body is discarded in bounded chunks.
    assert max(reads) < MAX_NATIVE_MESSAGE_BYTES


def test_the_relay_reports_an_oversize_batch_and_keeps_serving() -> None:
    oversize = MAX_NATIVE_MESSAGE_BYTES + 1
    good = json.dumps({"type": "session.heartbeat"}).encode("utf-8")
    stdin = io.BytesIO(
        struct.pack("<I", oversize) + b"x" * oversize + struct.pack("<I", len(good)) + good
    )
    stdout = io.BytesIO()
    stderr = io.StringIO()
    pipe = FakePipe([json.dumps({"result": {"ok": True}}).encode("utf-8")])

    exit_code = run_native_host(
        stdin,
        stdout,
        stderr,
        connect=lambda _descriptor: pipe,
        descriptor_loader=_descriptor,
    )

    assert exit_code == 0
    replies = decode_native(stdout.getvalue())
    assert replies[0]["error"]["code"] == "message_too_large"
    assert replies[1] == {"result": {"ok": True}}
    # The refused frame never reached FavHub, but the one after it did.
    assert len(pipe.sent) == 1
    assert "message_too_large" in stderr.getvalue()


def test_responses_are_written_with_an_exact_frame() -> None:
    stream = io.BytesIO()
    write_native_message(stream, {"result": {"ok": True}})
    raw = stream.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert decode_native(raw) == [{"result": {"ok": True}}]


# -- runtime descriptor -------------------------------------------------------


def test_a_missing_descriptor_reports_mcp_unavailable(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "root")
    paths.ensure()
    with pytest.raises(NativeHostError) as error:
        load_runtime_descriptor(paths)
    assert error.value.code == "mcp_unavailable"


def test_a_malformed_descriptor_reports_mcp_unavailable(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "root")
    paths.ensure()
    paths.browser_pipe_descriptor.write_text("{ not json", encoding="utf-8")
    with pytest.raises(NativeHostError) as error:
        load_runtime_descriptor(paths)
    assert error.value.code == "mcp_unavailable"


def test_a_descriptor_from_a_future_schema_is_refused(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "root")
    paths.ensure()
    paths.browser_pipe_descriptor.write_text(
        json.dumps(
            {
                "schemaVersion": 99,
                "pipe": r"\\.\pipe\favhub-x",
                "authKey": "YWJj",
                "pid": 1,
                "protocolVersion": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NativeHostError) as error:
        load_runtime_descriptor(paths)
    assert error.value.code == "extension_version_mismatch"


def test_a_valid_descriptor_round_trips(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "root")
    paths.ensure()
    paths.browser_pipe_descriptor.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pipe": r"\\.\pipe\favhub-abc",
                "authKey": "YWJj",
                "pid": 4321,
                "protocolVersion": 1,
            }
        ),
        encoding="utf-8",
    )
    descriptor = load_runtime_descriptor(paths)
    assert descriptor.pipe == r"\\.\pipe\favhub-abc"
    assert descriptor.auth_key == b"abc"
    assert descriptor.pid == 4321


# -- relay loop ---------------------------------------------------------------


class FakePipe:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.sent: list[bytes] = []

    def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv_bytes(self) -> bytes:
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def test_each_request_is_forwarded_and_its_reply_framed_back() -> None:
    pipe = FakePipe(
        [
            json.dumps({"requestId": "r-1", "result": {"status": "ok"}}).encode("utf-8"),
            json.dumps({"requestId": "r-2", "result": {"status": "done"}}).encode("utf-8"),
        ]
    )
    stdin = io.BytesIO(
        encode_native({"requestId": "r-1", "type": "session.claim"})
        + encode_native({"requestId": "r-2", "type": "session.finish"})
    )
    stdout = io.BytesIO()
    stderr = io.StringIO()

    assert (
        run_native_host(
            stdin,
            stdout,
            stderr,
            connect=lambda _descriptor: pipe,
            descriptor_loader=lambda: _descriptor(),
        )
        == 0
    )
    assert [json.loads(sent)["type"] for sent in pipe.sent] == [
        "session.claim",
        "session.finish",
    ]
    assert [message["result"]["status"] for message in decode_native(stdout.getvalue())] == [
        "ok",
        "done",
    ]


def test_an_unreachable_pipe_reports_mcp_unavailable_to_the_extension() -> None:
    def refuse(_descriptor: object) -> Any:
        raise OSError("pipe is gone")

    stdout = io.BytesIO()
    stderr = io.StringIO()
    code = run_native_host(
        io.BytesIO(encode_native({"requestId": "r-1", "type": "session.claim"})),
        stdout,
        stderr,
        connect=refuse,
        descriptor_loader=lambda: _descriptor(),
    )
    assert code == 1
    messages = decode_native(stdout.getvalue())
    assert messages[0]["error"]["code"] == "mcp_unavailable"


def test_a_framing_error_is_reported_once_and_ends_the_relay() -> None:
    pipe = FakePipe([])
    stdout = io.BytesIO()
    stderr = io.StringIO()
    code = run_native_host(
        io.BytesIO(struct.pack("<I", 10) + b"{"),
        stdout,
        stderr,
        connect=lambda _descriptor: pipe,
        descriptor_loader=lambda: _descriptor(),
    )
    assert code == 1
    messages = decode_native(stdout.getvalue())
    assert len(messages) == 1
    assert messages[0]["error"]["code"] == "invalid_message"


def test_the_relay_never_echoes_the_offending_payload() -> None:
    stdout = io.BytesIO()
    secret = "sensitive-value-from-the-page"
    body = json.dumps({"type": "session.claim", "payload": secret}).encode("utf-8")
    run_native_host(
        io.BytesIO(struct.pack("<I", len(body) + 50) + body),
        stdout,
        io.StringIO(),
        connect=lambda _descriptor: FakePipe([]),
        descriptor_loader=lambda: _descriptor(),
    )
    assert secret not in stdout.getvalue().decode("utf-8")


def _descriptor() -> Any:
    from favhub.browser_pipe import PipeDescriptor

    return PipeDescriptor(pipe=r"\\.\pipe\favhub-test", auth_key=b"k", pid=1, protocol_version=1)


def test_the_entry_point_is_declared() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    assert 'favhub-native-host = "favhub.native_host:main"' in text


@pytest.mark.skipif(sys.platform != "win32", reason="AF_PIPE is Windows-only")
def test_main_reports_a_missing_descriptor_without_traceback(tmp_path: Path) -> None:
    stdout = io.BytesIO()
    stderr = io.StringIO()
    code = native_host.main(
        ["--root", str(tmp_path / "root")],
        stdin=io.BytesIO(encode_native({"requestId": "r-1", "type": "session.claim"})),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 1
    assert decode_native(stdout.getvalue())[0]["error"]["code"] == "mcp_unavailable"
    assert "Traceback" not in stderr.getvalue()


def test_chrome_launches_the_relay_with_no_arguments_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Native Messaging manifest names an executable and nothing else.

    Chrome supplies only the calling origin and, on Windows, a window handle —
    there is no way to pass `--root`. Requiring it made argparse exit 2 before
    the relay read a byte, which the extension could only report as a
    disconnect, so the argv Chrome actually uses is pinned here.
    """
    chrome_argv = ["chrome-extension://abjlifflomnolgbngicokdhphnnggmim/", "--parent-window=0"]
    known, _unknown = native_host._build_parser().parse_known_args(chrome_argv)
    assert known.root is None

    # An empty LOCALAPPDATA keeps this off whatever is installed on the machine
    # running the suite; the point is the argv, not the descriptor.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    stdout, stderr = io.BytesIO(), io.StringIO()
    code = native_host.main(
        chrome_argv,
        stdin=io.BytesIO(encode_native({"requestId": "r-1", "type": "session.claim"})),
        stdout=stdout,
        stderr=stderr,
    )
    # The relay must answer in Chrome's framing rather than die on its command
    # line: argparse exiting here would raise SystemExit and write nothing.
    assert code == 1
    assert decode_native(stdout.getvalue())[0]["error"]["code"] == "mcp_unavailable"
    assert "Traceback" not in stderr.getvalue()


def test_without_a_flag_the_relay_uses_the_root_setup_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`favhub setup` already records the chosen root; the relay reads that."""
    from favhub.config import InstallPaths, save_install_config

    local_app_data = tmp_path / "LocalAppData"
    installed = InstallPaths.from_local_app_data(local_app_data)
    chosen = tmp_path / "chosen-root"
    save_install_config(installed, chosen)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert native_host.resolve_data_root(None) == chosen.resolve()
    # An explicit flag still wins, so a second root stays testable by hand.
    assert native_host.resolve_data_root(tmp_path / "other") == tmp_path / "other"


def test_an_uninstalled_machine_reports_mcp_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    with pytest.raises(native_host.NativeHostError) as raised:
        native_host.resolve_data_root(None)
    assert raised.value.code == "mcp_unavailable"
