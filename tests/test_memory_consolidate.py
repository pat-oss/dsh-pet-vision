"""tests/test_memory_consolidate.py — 记忆整理（自动记住上下文）的回归。

覆盖两段纯逻辑，都不碰网络：
- `parse_consolidate_json`：模型输出解析与容错（含截断 JSON、脏条目、非法 kind）；
- `NoteStore.apply_consolidation`：**先删后加**的顺序、双向子串匹配、
  短关键词拒绝、敏感内容拒绝、去重不膨胀。

为什么值得单独测：这两段直接决定"宠物会不会把错的东西记一辈子"。
写坏了不会报错，只会悄悄记错——属于典型的静默失效，必须用测试钉住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.memory import NoteStore
from core.prompts import MAX_CONSOLIDATE_ITEMS, parse_consolidate_json


def make_store(tmp_path: Path) -> NoteStore:
    return NoteStore(tmp_path / "notes.jsonl")


# ---------------- 解析 ----------------

def test_parse_normal() -> None:
    raw = json.dumps({
        "remember": [{"kind": "fact", "text": "主人在写毕业论文"},
                     {"kind": "prefer", "text": "主人喜欢喝冰美式"}],
        "drop": ["主人在写论文"],
    }, ensure_ascii=False)
    remember, drop = parse_consolidate_json(raw)
    assert remember == [("fact", "主人在写毕业论文"), ("prefer", "主人喜欢喝冰美式")]
    assert drop == ["主人在写论文"]


def test_parse_bad_kind_falls_back_to_fact() -> None:
    raw = '{"remember": [{"kind": "totally-wrong", "text": "主人在备考"}]}'
    remember, _ = parse_consolidate_json(raw)
    assert remember == [("fact", "主人在备考")]


def test_parse_string_items() -> None:
    """模型偶尔直接给字符串数组，别整批丢掉。"""
    raw = '{"remember": ["主人养了一只猫"], "drop": []}'
    remember, drop = parse_consolidate_json(raw)
    assert remember == [("fact", "主人养了一只猫")]
    assert drop == []


def test_parse_truncated_json_salvages_text() -> None:
    """被 max_tokens 截断的半个 JSON：能救几条算几条。"""
    raw = '{"remember": [{"kind": "fact", "text": "主人最近失眠"}'
    remember, drop = parse_consolidate_json(raw)
    assert remember == [("fact", "主人最近失眠")]
    assert drop == []


def test_parse_garbage_returns_empty() -> None:
    """彻底解析失败必须是两个空列表——调用方据此跳过写入，绝不能瞎记。"""
    assert parse_consolidate_json("我不知道该记什么") == ([], [])
    assert parse_consolidate_json("") == ([], [])
    assert parse_consolidate_json(None) == ([], [])


def test_parse_caps_item_count() -> None:
    """模型刷屏时截断在上限，不让一次整理塞爆笔记。"""
    items = [{"kind": "fact", "text": f"主人第{i}件事"} for i in range(20)]
    remember, _ = parse_consolidate_json(json.dumps({"remember": items},
                                                    ensure_ascii=False))
    assert len(remember) == MAX_CONSOLIDATE_ITEMS


def test_parse_skips_empty_text() -> None:
    raw = '{"remember": [{"kind": "fact", "text": ""}, {"kind": "fact", "text": "主人爱猫"}]}'
    remember, _ = parse_consolidate_json(raw)
    assert remember == [("fact", "主人爱猫")]


# ---------------- 应用（合并） ----------------

def test_apply_merge_replaces_old(tmp_path: Path) -> None:
    """整合的核心场景：旧笔记被合并成更完整的表述。"""
    store = make_store(tmp_path)
    store.add("主人在写论文", "task")
    added, removed = store.apply_consolidation(
        [("task", "主人在写毕业论文，下周三要交")], ["主人在写论文"])
    assert (added, removed) == (1, 1)
    texts = [n.text for n in store.all()]
    assert texts == ["主人在写毕业论文，下周三要交"]


def test_apply_deletes_before_adding(tmp_path: Path) -> None:
    """顺序回归：先加后删会把刚写进去的又删掉。"""
    store = make_store(tmp_path)
    store.add("主人喜欢喝咖啡", "prefer")
    # 新表述包含 drop 关键词；若先加后删，新条目会一起被删掉
    added, removed = store.apply_consolidation(
        [("prefer", "主人喜欢喝咖啡，每天早上都要一杯")], ["主人喜欢喝咖啡"])
    assert (added, removed) == (1, 1)
    assert [n.text for n in store.all()] == ["主人喜欢喝咖啡，每天早上都要一杯"]


def test_apply_drop_matches_loosely(tmp_path: Path) -> None:
    """模型给的 drop 常常是旧笔记的改写版，双向子串匹配才是能用。"""
    store = make_store(tmp_path)
    store.add("主人自称小明", "fact")
    added, removed = store.apply_consolidation([], ["小明"])
    assert (added, removed) == (0, 1)
    assert store.count() == 0


def test_apply_drop_with_rendered_label(tmp_path: Path) -> None:
    """真实观测到的形态：模型把渲染后的整行抄回来（带 `[关于主人] ` 前缀）。

    实测原文是 `"[主人在做的事] 主人在写论文"`，必须剥掉前缀才能稳定删掉，
    不能依赖"子串恰好包含"这种运气。
    """
    store = make_store(tmp_path)
    store.add("主人在写论文", "task")
    added, removed = store.apply_consolidation(
        [("task", "主人在赶毕业论文，下周三交初稿")], ["[主人在做的事] 主人在写论文"])
    assert (added, removed) == (1, 1)
    assert [n.text for n in store.all()] == ["主人在赶毕业论文，下周三交初稿"]


def test_apply_rejects_short_drop_keyword(tmp_path: Path) -> None:
    """短关键词（1 字）必须拒绝，否则一句"删主人"会把全部笔记清空。"""
    store = make_store(tmp_path)
    store.add("主人喜欢猫", "prefer")
    store.add("主人在备考", "task")
    added, removed = store.apply_consolidation([], ["主", "人"])
    assert (added, removed) == (0, 0)
    assert store.count() == 2


def test_apply_dedupes_identical_text(tmp_path: Path) -> None:
    """同一事实反复整理不该越堆越多。"""
    store = make_store(tmp_path)
    store.add("主人喜欢猫", "prefer")
    added, removed = store.apply_consolidation([("prefer", "主人喜欢猫")], [])
    assert (added, removed) == (1, 0)      # add 命中去重分支，仍算"处理成功"
    assert store.count() == 1


def test_apply_rejects_sensitive(tmp_path: Path) -> None:
    """模型把密码写进记忆时必须拦住——宁可少记。"""
    store = make_store(tmp_path)
    added, removed = store.apply_consolidation(
        [("fact", "主人的银行卡密码是123456")], [])
    assert (added, removed) == (0, 0)
    assert store.count() == 0


def test_apply_persists_to_disk(tmp_path: Path) -> None:
    """整理结果要真的落盘：重启后还在，才叫"记住了"。"""
    path = tmp_path / "notes.jsonl"
    store = NoteStore(path)
    store.apply_consolidation([("fact", "主人在学日语")], [])
    again = NoteStore(path)
    assert [n.text for n in again.all()] == ["主人在学日语"]


def test_apply_noop_does_not_touch_file(tmp_path: Path) -> None:
    """空结果不写盘（会话结束经常没东西可记，别每次刷一遍文件）。"""
    store = make_store(tmp_path)
    added, removed = store.apply_consolidation([], [])
    assert (added, removed) == (0, 0)
    assert not (tmp_path / "notes.jsonl").exists()
