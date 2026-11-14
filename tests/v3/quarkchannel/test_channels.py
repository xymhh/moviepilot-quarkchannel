"""渠道存储单元测试：CRUD、排序、校验、迁移与统计。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarkchannel.channels import ChannelStore


def _pansou(name: str = "盘搜", priority: int = 50,
            base_url: str = "http://pansou:8888") -> dict:
    """构造合法的 pansou 渠道载荷。"""
    return {
        "name": name,
        "type": "pansou",
        "enabled": True,
        "priority": priority,
        "config": {
            "base_url": base_url,
            "token": "",
            "username": "",
            "password": "",
            "src": "all",
            "plugins": ["a"],
            "cloud_types": ["quark"],
        },
    }


def _tg(channel: str = "@demo", priority: int = 30) -> dict:
    """构造合法的 TG 渠道载荷。"""
    return {
        "name": channel,
        "type": "tg_channel",
        "enabled": True,
        "priority": priority,
        "config": {
            "channel": channel,
            "interval_minutes": 10,
            "scan_limit": 100,
            "backfill": False,
        },
    }


def test_create_lists_by_priority_and_assigns_ids(tmp_path: Path):
    """新增渠道返回完整对象，列表按 priority 倒序。"""
    store = ChannelStore(tmp_path)
    low = store.create(_pansou("低", 10))
    high = store.create(_tg("@high", 90))
    mid = store.create(_pansou("中", 50))
    assert len({low["id"], high["id"], mid["id"]}) == 3
    assert [item["name"] for item in store.list()] == ["@high", "中", "低"]
    assert low["stats"] == {"last_scan": 0.0, "last_error": "", "hits": 0}


def test_get_update_delete_and_missing(tmp_path: Path):
    """读取、局部更新、删除以及不存在时的返回值。"""
    store = ChannelStore(tmp_path)
    item = store.create(_pansou())
    channel_id = item["id"]
    assert store.get(channel_id)["name"] == "盘搜"
    assert store.get("missing") is None
    assert store.update("missing", {"enabled": False}) is None
    assert store.delete("missing") is False

    updated = store.update(channel_id, {"name": "新盘搜", "priority": 70,
                                        "enabled": False})
    assert updated["name"] == "新盘搜"
    assert updated["priority"] == 70
    assert updated["enabled"] is False
    assert updated["id"] == channel_id
    assert store.delete(channel_id) is True
    assert store.list() == []


def test_record_stat_accumulates_and_clears_error(tmp_path: Path):
    """统计命中数累加，并记录扫描时间与错误。"""
    store = ChannelStore(tmp_path)
    channel_id = store.create(_pansou())["id"]
    store.record_stat(channel_id, hits=2, error="旧错误")
    row = store.get(channel_id)
    assert row["stats"]["hits"] == 2
    assert row["stats"]["last_error"] == "旧错误"
    assert row["stats"]["last_scan"] > 0

    store.record_stat(channel_id, hits=3, error="")
    row = store.get(channel_id)
    assert row["stats"]["hits"] == 5
    assert row["stats"]["last_error"] == ""


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "pansou", "config": {"base_url": "http://x"}}, "名称"),
        ({"name": "x", "type": "bad", "config": {}}, "类型"),
        ({"name": "x", "type": "pansou", "config": {}}, "base_url"),
        ({"name": "x", "type": "tg_channel", "config": {}}, "channel"),
    ],
)
def test_create_rejects_invalid_payload(tmp_path: Path, payload, message):
    """名称、类型与类型专属必填项都会校验。"""
    store = ChannelStore(tmp_path)
    with pytest.raises(ValueError, match=message):
        store.create(payload)


def test_update_rejects_invalid_config(tmp_path: Path):
    """局部更新同样执行完整校验，不能把合法渠道改坏。"""
    store = ChannelStore(tmp_path)
    channel_id = store.create(_pansou())["id"]
    with pytest.raises(ValueError, match="base_url"):
        store.update(channel_id, {"config": {"base_url": ""}})


def test_config_normalizes_lists_and_defaults(tmp_path: Path):
    """旧式逗号串配置统一成列表，非法 src 回退 all。"""
    store = ChannelStore(tmp_path)
    item = store.create({
        "name": "盘搜",
        "type": "pansou",
        "config": {
            "base_url": " http://pansou:8888/ ",
            "src": "bad",
            "plugins": "a, b，c",
            "cloud_types": "quark,115",
        },
    })
    config = item["config"]
    assert config["base_url"] == "http://pansou:8888"
    assert config["src"] == "all"
    assert config["plugins"] == ["a", "b", "c"]
    assert config["cloud_types"] == ["quark", "115"]


def test_tg_config_defaults(tmp_path: Path):
    """TG 渠道缺失轮询参数时使用安全默认值。"""
    store = ChannelStore(tmp_path)
    item = store.create({
        "name": "@demo", "type": "tg_channel",
        "config": {"channel": "@demo"},
    })
    assert item["config"]["interval_minutes"] == 10
    assert item["config"]["scan_limit"] == 100
    assert item["config"]["backfill"] is False


def test_migrate_old_pansou_and_tg_channels(tmp_path: Path):
    """旧全局配置迁移成盘搜与逐行 TG 渠道。"""
    store = ChannelStore(tmp_path)
    count = store.migrate_from_config({
        "pansou_url": "http://pansou:8888",
        "pansou_token": "token",
        "pansou_user": "user",
        "pansou_password": "pass",
        "pansou_src": "tg",
        "pansou_plugins": "a,b",
        "pansou_cloud_types": "quark,115",
        "tg_channels": "@one\nhttps://t.me/two\n",
        "tg_interval_minutes": 20,
        "tg_scan_limit": 50,
        "tg_backfill": True,
    })
    assert count == 3
    rows = store.list()
    assert [item["priority"] for item in rows] == [50, 30, 30]
    pansou = next(item for item in rows if item["type"] == "pansou")
    assert pansou["name"] == "盘搜"
    assert pansou["config"]["base_url"] == "http://pansou:8888"
    assert pansou["config"]["token"] == "token"
    assert pansou["config"]["plugins"] == ["a", "b"]
    assert pansou["config"]["cloud_types"] == ["quark", "115"]
    tg_rows = [item for item in rows if item["type"] == "tg_channel"]
    assert {item["config"]["channel"] for item in tg_rows} == {
        "@one", "https://t.me/two",
    }
    assert all(item["config"]["interval_minutes"] == 20 for item in tg_rows)
    assert all(item["config"]["scan_limit"] == 50 for item in tg_rows)
    assert all(item["config"]["backfill"] is True for item in tg_rows)


def test_migrate_is_idempotent_and_preserves_ids(tmp_path: Path):
    """迁移只执行一次，重新加载后 id 与数据不变。"""
    config = {"pansou_url": "http://pansou:8888", "tg_channels": "@one"}
    store = ChannelStore(tmp_path)
    assert store.migrate_from_config(config) == 2
    first_ids = sorted(item["id"] for item in store.list())
    assert store.migrate_from_config(config) == 0

    reloaded = ChannelStore(tmp_path)
    assert reloaded.migrate_from_config(config) == 0
    assert sorted(item["id"] for item in reloaded.list()) == first_ids


def test_migrate_without_old_config_returns_zero(tmp_path: Path):
    """没有旧配置时不创建文件，也不产生渠道。"""
    store = ChannelStore(tmp_path)
    assert store.migrate_from_config({}) == 0
    assert store.list() == []
    assert not (tmp_path / "channels.json").exists()


def test_persistence_keeps_stats_and_order(tmp_path: Path):
    """落盘后重新加载保留 id、统计和排序结果。"""
    store = ChannelStore(tmp_path)
    first = store.create(_pansou("低", 1))["id"]
    second = store.create(_tg("@high", 99))["id"]
    store.record_stat(first, hits=7)
    reloaded = ChannelStore(tmp_path)
    assert [item["id"] for item in reloaded.list()] == [second, first]
    assert reloaded.get(first)["stats"]["hits"] == 7


def test_list_returns_independent_copies(tmp_path: Path):
    """调用方修改列表结果不会污染存储内部状态。"""
    store = ChannelStore(tmp_path)
    channel_id = store.create(_pansou())["id"]
    rows = store.list()
    rows[0]["name"] = "被篡改"
    rows[0]["config"]["base_url"] = "http://bad"
    assert store.get(channel_id)["name"] == "盘搜"
    assert store.get(channel_id)["config"]["base_url"] == "http://pansou:8888"


def test_file_shape_is_stable(tmp_path: Path):
    """JSON 顶层固定为 items，便于前端与后续版本兼容。"""
    store = ChannelStore(tmp_path)
    store.create(_tg())
    payload = json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))
    assert list(payload) == ["items"]
    assert payload["items"][0]["type"] == "tg_channel"
