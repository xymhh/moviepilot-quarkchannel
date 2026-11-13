"""bencode 与合成种子单元测试（纯标准库，不依赖 MoviePilot 运行时）。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _item in (str(HERE.parents[2] / "plugins.v3"), str(HERE)):
    if _item not in sys.path:
        sys.path.insert(0, _item)

import _stubs  # noqa: E402

_stubs._install_stubs()

from quarkchannel.bencode import build_torrent, encode, info_dict, info_hash  # noqa: E402
from quarkchannel.torrents import (  # noqa: E402
    MARKER,
    ShareEntry,
    build_sid,
    parse_marker,
    render_torrent,
    sign,
    verify,
)


def test_encode_basic_types():
    """基础类型编码符合 bencode 规范。"""
    assert encode(0) == b"i0e"
    assert encode(42) == b"i42e"
    assert encode(b"abc") == b"3:abc"
    assert encode("中文") == b"6:" + "中文".encode()
    assert encode([1, b"a"]) == b"li1e1:ae"


def test_encode_dict_sorted_keys():
    """字典键按字节序排序，保证 info_hash 稳定。"""
    assert encode({"b": 1, "a": 2}) == b"d1:ai2e1:bi1ee"


def test_info_dict_pieces_length():
    """pieces 长度按分片数量计算且受上限约束。"""
    files = [{"length": 1024, "path": ["a.mkv"]}]
    payload = info_dict("demo", files, piece_length=262144)
    assert payload["pieces"] == b"\x00" * 20
    big = info_dict("demo", [{"length": 262144 * 1000, "path": ["a.mkv"]}], max_pieces_bytes=200)
    assert len(big["pieces"]) <= 200


def test_build_torrent_and_hash_stable():
    """同一输入生成的种子与 hash 完全一致。"""
    files = [
        {"length": 1024, "path": ["S01E01.mkv"]},
        {"length": 2048, "path": ["S01E02.mkv"]},
    ]
    first = build_torrent("Demo Show", files, f"{MARKER}|abc|quark")
    second = build_torrent("Demo Show", files, f"{MARKER}|abc|quark")
    assert first == second
    digest = info_hash(first)
    assert len(digest) == 40
    int(digest, 16)


def test_render_and_parse_marker():
    """插件标记可被自己解析回来。"""
    entry = ShareEntry(
        sid="abc123",
        provider="quark",
        share_url="https://pan.quark.cn/s/abc123",
        title="测试剧集 S01",
        files=[{"name": "S01E01.mkv", "size": 1024, "fid": "1"}],
    )
    payload = render_torrent(entry)
    assert parse_marker(payload) == ("abc123", "quark")
    assert b"abc123" in payload


def test_parse_marker_rejects_foreign_torrent():
    """非本插件生成的种子不应被接管。"""
    payload = build_torrent("other", [{"length": 1, "path": ["a"]}], "hello")
    assert parse_marker(payload) is None


def test_sign_and_verify():
    """签名可用于匿名端点鉴权。"""
    sid = "deadbeef"
    secret = "s3cr3t"
    assert verify(sid, sign(sid, secret), secret) is True
    assert verify(sid, "wrong", secret) is False
    assert verify(sid, sign(sid, secret), "") is False


def test_build_sid_stable():
    """同一分享地址得到同一 id。"""
    assert build_sid("https://pan.quark.cn/s/x1") == build_sid("https://pan.quark.cn/s/x1")
    assert build_sid("https://pan.quark.cn/s/x1") != build_sid("https://pan.quark.cn/s/x2")


def test_folder_name_sanitized():
    """种子名去掉路径非法字符。"""
    entry = ShareEntry(sid="s", provider="quark", share_url="u", title='a/b:c*?')
    assert "/" not in entry.folder_name and ":" not in entry.folder_name


def test_info_hash_matches_bencode_info():
    """info_hash 与直接对 info 段做 sha1 一致。"""
    files = [{"length": 3, "path": ["a"]}]
    torrent = build_torrent("n", files, f"{MARKER}|s|quark")
    payload_info = encode(info_dict("n", files))
    expected = hashlib.sha1(payload_info).hexdigest()
    assert info_hash(torrent) == expected
