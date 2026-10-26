"""tests/test_tts_cache.py — 短句 PCM 缓存必须真的落盘。

背景（真 bug）：临时文件名写成 `tts_cache.npz.tmp`，而
np.savez_compressed 会自动给不以 .npz 结尾的名字追加 .npz，
实际落盘 `tts_cache.npz.tmp.npz`；随后 os.replace(tmp, path) 找不到
`tts_cache.npz.tmp` 而抛错，异常又被 except 吞掉——结果磁盘缓存**从未
生效**，每次开机都要重新预热十几秒，日志里还什么都不显示。

这里钉住三件事：
  1. save() 之后目标文件真实存在；
  2. 音色不一致时旧缓存作废（音频里带着旧音色，不能拿来用）；
  3. 不残留临时文件。
"""

from __future__ import annotations

import numpy as np

from voice.cache import UtteranceCache


def _cache(tmp_path, voice: str) -> UtteranceCache:
    return UtteranceCache(tmp_path / "tts_cache.npz", voice)


def test_save_writes_real_file(tmp_path) -> None:
    cache = _cache(tmp_path, "ruannuo-luoli")
    cache.put("我在，主人。", np.full(1600, 0.5, dtype=np.float32))
    cache.save()

    path = tmp_path / "tts_cache.npz"
    assert path.is_file(), (
        "缓存未落盘——检查临时文件名：np.savez_compressed 会给不以 .npz "
        "结尾的文件名自动追加 .npz，导致 os.replace 找不到源文件"
    )

    reloaded = _cache(tmp_path, "ruannuo-luoli")
    reloaded.load()
    got = reloaded.get("我在，主人。")
    assert got is not None and got.size == 1600


def test_cache_is_scoped_by_voice(tmp_path) -> None:
    first = _cache(tmp_path, "ruannuo-luoli")
    first.put("我在，主人。", np.ones(800, dtype=np.float32))
    first.save()

    other = _cache(tmp_path, "xiaojingyu")
    other.load()
    assert other.get("我在，主人。") is None, (
        "换了音色却读到了旧音色的音频——缓存必须按音色隔离"
    )


def test_no_temp_files_left_behind(tmp_path) -> None:
    cache = _cache(tmp_path, "default")
    cache.put("好呀，我陪你。", np.zeros(400, dtype=np.float32))
    cache.save()

    leftovers = sorted(p.name for p in tmp_path.glob("*.tmp*"))
    assert not leftovers, f"保存后残留临时文件：{leftovers}"
    assert sorted(p.name for p in tmp_path.glob("*.npz")) == ["tts_cache.npz"]
