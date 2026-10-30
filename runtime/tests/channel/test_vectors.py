"""已知答案测试：向量由 `core/crates/bellis-channel/testdata/generate-vectors.ts` 从 Bellis 的
TypeScript 实现导出，见 `docs/modules/core-protocol.md` 的“本机通道”。"""

import asyncio
import json
from typing import Any

import pytest
from game_runtime.channel import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_MAX_PENDING_HANDSHAKES,
    HANDSHAKE_MAX_FRAME_BYTES,
    MAX_UDS_PATH_BYTES,
    PROOF_DOMAIN,
    SESSION_KEY_BYTES,
    ChannelError,
    Connection,
    FrameDecoder,
    FrameKind,
    JsonMessage,
    RpcError,
    RpcPeer,
    decode_session_key,
    encode_frame,
    encode_session_key,
)
from game_runtime.channel.address import PIPE_PREFIX, channel_name
from game_runtime.channel.connection import CLOSE_GRACE
from game_runtime.channel.frame import FRAME_HEADER_BYTES, Frame, dumps, loads
from game_runtime.channel.handshake import (
    ClientHandshake,
    ServerHandshake,
    b64url,
    is_accept,
    mac,
    parse_challenge,
    parse_hello,
    parse_proof,
)
from game_runtime.channel.rpc import DEFAULT_MAX_INFLIGHT, cancel_target, classify

type Vectors = dict[str, Any]


def test_constants_match_bellis(vectors: Vectors) -> None:
    constants = vectors["constants"]
    assert constants["proof_domain"] == PROOF_DOMAIN
    assert constants["frame_header_bytes"] == FRAME_HEADER_BYTES
    assert constants["handshake_max_frame_bytes"] == HANDSHAKE_MAX_FRAME_BYTES
    assert constants["default_max_frame_bytes"] == DEFAULT_MAX_FRAME_BYTES
    assert constants["default_handshake_timeout_ms"] == DEFAULT_HANDSHAKE_TIMEOUT * 1000
    assert constants["close_grace_ms"] == CLOSE_GRACE * 1000
    assert constants["default_max_pending_handshakes"] == DEFAULT_MAX_PENDING_HANDSHAKES
    assert constants["default_rpc_max_inflight"] == DEFAULT_MAX_INFLIGHT
    assert constants["session_key_bytes"] == SESSION_KEY_BYTES
    assert constants["max_uds_path_bytes"] == MAX_UDS_PATH_BYTES


def test_frames_encode(vectors: Vectors) -> None:
    for vector in vectors["frames"]:
        frame = encode_frame(FrameKind(vector["kind"]), bytes.fromhex(vector["payload_hex"]))
        assert frame.hex() == vector["frame_hex"]


def test_frames_decode(vectors: Vectors) -> None:
    for case in vectors["decoder"]:
        decoder = FrameDecoder(case["max_payload_bytes"])
        decoder.push(bytes.fromhex(case["input_hex"]))
        frames: list[dict[str, object]] = []
        error = None
        try:
            while (frame := decoder.read()) is not None:
                frames.append({"kind": frame.kind, "payload_hex": frame.payload.hex()})
        except ChannelError as caught:
            error = caught.code
        assert frames == case["frames"], case["name"]
        assert error == case["error"], case["name"]
        if case["buffered_bytes"] is not None:
            assert decoder.buffered_bytes == case["buffered_bytes"], case["name"]


def test_channel_names(vectors: Vectors) -> None:
    address = vectors["address"]
    random = bytes.fromhex(address["random_hex"])
    windows = address["windows"]
    assert PIPE_PREFIX + channel_name(windows["session_id"], random) == windows["path"]
    posix = address["posix"]
    assert channel_name(posix["session_id"], random) + ".sock" == posix["file_name"]
    for case in address["session_ids"]:
        if case["valid"]:
            channel_name(case["session_id"], random)
        else:
            with pytest.raises(ChannelError) as raised:
                channel_name(case["session_id"], random)
            assert raised.value.code == "address-invalid"


def test_session_keys(vectors: Vectors) -> None:
    for case in vectors["address"]["session_keys"]:
        if case["key_hex"] is None:
            with pytest.raises(ChannelError) as raised:
                decode_session_key(case["text"])
            assert raised.value.code == "address-invalid"
        else:
            key = decode_session_key(case["text"])
            assert key.hex() == case["key_hex"]
            assert encode_session_key(key) == case["text"]


def test_handshake_message_validation(vectors: Vectors) -> None:
    messages = vectors["messages"]
    for case in messages["hello"]:
        assert (parse_hello(json.loads(case["json"])) is not None) == case["valid"], case["json"]
    for case in messages["challenge"]:
        valid = parse_challenge(json.loads(case["json"])) is not None
        assert valid == case["valid"], case["json"]
    for case in messages["proof"]:
        assert (parse_proof(json.loads(case["json"])) is not None) == case["valid"], case["json"]
    for case in messages["accept"]:
        assert is_accept(json.loads(case["json"])) == case["valid"], case["json"]


def test_rpc_message_classification(vectors: Vectors) -> None:
    for case in vectors["messages"]["rpc"]:
        assert classify(json.loads(case["json"])) == case["kind"], case["json"]
    for case in vectors["messages"]["cancel_params"]:
        valid = cancel_target(json.loads(case["json"])) is not None
        assert valid == case["valid"], case["json"]


def test_payload_parsing(vectors: Vectors) -> None:
    for case in vectors["payloads"]:
        payload = bytes.fromhex(case["payload_hex"])
        if case["valid"]:
            assert loads(payload) == json.loads(case["value_json"]), case["name"]
        else:
            with pytest.raises(ChannelError) as raised:
                loads(payload)
            assert raised.value.code == case["error"], case["name"]


def test_json_encoding_matches_node() -> None:
    # 与 Node 的 JSON.stringify 一样：紧凑、不转义非 ASCII、保留键的顺序。
    assert dumps({"text": "你好", "a": [1, None]}) == '{"text":"你好","a":[1,null]}'.encode()


def _frames(frames: list[dict[str, Any]]) -> bytes:
    return b"".join(bytes.fromhex(frame["frame_hex"]) for frame in frames)


def _frame(hex_text: str) -> Frame:
    decoder = FrameDecoder(1 << 32)
    decoder.push(bytes.fromhex(hex_text))
    frame = decoder.read()
    assert frame is not None
    return frame


def test_session_handshake_bytes(vectors: Vectors) -> None:
    session = vectors["session"]
    key = bytes.fromhex(session["key_hex"])
    role = session["role"]
    hello, challenge, proof, accept = (frame["frame_hex"] for frame in session["handshake"])

    client = ClientHandshake(key, role, bytes.fromhex(session["client_nonce_hex"]))
    assert client.hello().hex() == hello
    assert client.on_challenge(_frame(challenge)).hex() == proof
    client.on_accept(_frame(accept))

    server = ServerHandshake(key, bytes.fromhex(session["server_nonce_hex"]))
    assert server.on_hello(_frame(hello)).hex() == challenge
    assert server.on_proof(_frame(proof)) == (role, bytes.fromhex(accept))

    client_nonce = b64url(bytes.fromhex(session["client_nonce_hex"]))
    server_nonce = b64url(bytes.fromhex(session["server_nonce_hex"]))
    record = dumps([PROOF_DOMAIN, "server", role, client_nonce, server_nonce]).decode()
    assert record == session["mac_record"]
    expected = json.loads(session["handshake"][1]["text"])["proof"]
    assert b64url(mac(key, "server", role, client_nonce, server_nonce)) == expected


class _MemoryTransport(asyncio.Transport):
    """记录写出字节的内存传输；关闭时通知协议，`StreamWriter.wait_closed` 随之返回。"""

    def __init__(self, protocol: asyncio.StreamReaderProtocol) -> None:
        super().__init__()
        self.written = bytearray()
        self._protocol = protocol
        self._closing = False

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if not self._closing:
            self.written += data

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            asyncio.get_running_loop().call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self.close()

    def can_write_eof(self) -> bool:
        return False

    def get_write_buffer_size(self) -> int:
        return 0


def _memory_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, _MemoryTransport]:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport = _MemoryTransport(protocol)
    protocol.connection_made(transport)
    writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_running_loop())
    return reader, writer, transport


async def _run_case(case: dict[str, Any], key: bytes) -> dict[str, Any]:
    reader, writer, transport = _memory_streams()
    reader.feed_data(b"".join(bytes.fromhex(chunk) for chunk in case["input_hex"]))
    if case["end"]:
        reader.feed_eof()
    options: dict[str, Any] = {
        "max_frame_bytes": case["max_frame_bytes"],
        "handshake_timeout": 1.0,
        "nonce": bytes.fromhex(case["nonce_hex"]),
    }
    delivered: list[str] = []
    close_error = None
    try:
        if case["side"] == "server":
            connection = await Connection.accept(reader, writer, key, **options)
        else:
            connection = await Connection.open(reader, writer, case["role"], key, **options)
    except ChannelError as error:
        result = error.code
    else:
        result = "open"
        while True:
            try:
                message = await connection.receive(timeout=0.05)
            except ChannelError as error:
                close_error = None if error.code == "timeout" else error.code
                break
            assert isinstance(message, JsonMessage)
            delivered.append(json.dumps(message.value, separators=(",", ":")))
        await connection.close()
    return {
        "result": result,
        "close_error": close_error,
        "delivered": [{"json": text} for text in delivered],
        "output": bytes(transport.written).hex(),
    }


def test_handshake_cases(vectors: Vectors) -> None:
    key = bytes.fromhex(vectors["session"]["key_hex"])
    for case in vectors["handshake_cases"]:
        outcome = asyncio.run(_run_case(case, key))
        expected = {
            "result": case["result"],
            "close_error": case["close_error"],
            "delivered": case["delivered"],
            "output": _frames(case["output"]).hex(),
        }
        assert outcome == expected, f"{case['side']} {case['name']}"


async def _wait_written(transport: _MemoryTransport, expected: bytes, step: str) -> None:
    async with asyncio.timeout(2):
        while len(transport.written) < len(expected):
            await asyncio.sleep(0.001)
    actual = bytes(transport.written[: len(expected)])
    del transport.written[: len(expected)]
    assert actual == expected, f"{step}: {actual!r}"


def _serve_bellis_handlers(peer: RpcPeer, notifications: list[object]) -> None:
    """与生成脚本里 Bellis 服务端登记的处理函数相同。"""

    def add(params: dict[str, Any]) -> object:
        return params["a"] + params["b"]

    def fail(_: dict[str, Any]) -> object:
        raise RpcError(1001, "不允许", {"reason": "x"})

    def crash(_: dict[str, Any]) -> object:
        raise RuntimeError("内部细节")

    async def hang(_: dict[str, Any]) -> object:
        await asyncio.Event().wait()
        return None

    peer.handle("math.add", add)
    peer.handle("a.fail", fail)
    peer.handle("a.crash", crash)
    peer.handle("a.hang", hang)
    peer.on_notification("host.heartbeat", notifications.append)


def _check_result(step: dict[str, Any], task: "asyncio.Task[object]") -> None:
    error = step["error"]
    exception = task.exception()
    if error is None:
        assert exception is None, step["name"]
        assert task.result() == step["result"], step["name"]
    elif "rpc" in error:
        assert isinstance(exception, RpcError), step["name"]
        expected = error["rpc"]
        assert (exception.code, exception.message, exception.data) == (
            expected["code"],
            expected["message"],
            expected["data"],
        )
    else:
        assert isinstance(exception, ChannelError), step["name"]
        assert exception.code == error["channel"], step["name"]


async def _run_session(vectors: Vectors, side: str) -> list[object]:
    """以本仓库的 `side` 一侧对 Bellis 另一侧的录音逐帧核对。"""
    session = vectors["session"]
    key = bytes.fromhex(session["key_hex"])
    reader, writer, transport = _memory_streams()
    handshake = session["handshake"]
    reader.feed_data(_frames([f for f in handshake if f["from"] != side]))
    if side == "server":
        nonce = bytes.fromhex(session["server_nonce_hex"])
        connection = await Connection.accept(reader, writer, key, nonce=nonce)
    else:
        nonce = bytes.fromhex(session["client_nonce_hex"])
        connection = await Connection.open(reader, writer, session["role"], key, nonce=nonce)
    await _wait_written(transport, _frames([f for f in handshake if f["from"] == side]), "握手")

    peer = RpcPeer(connection, timeout=1.0)
    notifications: list[object] = []
    if side == "server":
        _serve_bellis_handlers(peer, notifications)
    else:
        peer.handle("host.ping", lambda _: None)
    for step in session["rpc"]:
        task: asyncio.Task[object] | None = None
        if step["actor"] == side:
            match step["call"]:
                case "request":
                    timeout = step.get("timeout_ms")
                    request = peer.request(
                        step["method"],
                        step["params"],
                        timeout=None if timeout is None else timeout / 1000,
                    )
                    task = asyncio.create_task(request)
                case "notify":
                    peer.notify(step["method"], step["params"])
                case "raw":
                    connection.send(step["raw"])
                case "binary":
                    connection.send_binary(bytes.fromhex(step["binary_hex"]))
                case other:
                    raise AssertionError(other)
        for frame in step["frames"]:
            data = bytes.fromhex(frame["frame_hex"])
            if frame["from"] == side:
                await _wait_written(transport, data, step["name"])
            else:
                reader.feed_data(data)
        if task is not None:
            await asyncio.wait({task}, timeout=2)
            _check_result(step, task)
    assert not transport.written, bytes(transport.written)
    await peer.close()
    return notifications


def test_rpc_session_as_server(vectors: Vectors) -> None:
    notifications = asyncio.run(_run_session(vectors, "server"))
    assert notifications == [{"seq": 7}, {}]


def test_rpc_session_as_client(vectors: Vectors) -> None:
    asyncio.run(_run_session(vectors, "client"))
