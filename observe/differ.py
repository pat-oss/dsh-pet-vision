"""observe/differ.py — FrameDiffer：dHash 差异检测（纯 numpy）。"""

from __future__ import annotations

from PIL import Image


class FrameDiffer:
    """64bit dHash：灰度 → 9×8 缩放 → 行间像素比较。"""

    def __init__(self, hash_size: int = 8) -> None:
        self._hash_size = hash_size

    def dhash(self, img: Image.Image) -> int:
        size = self._hash_size
        small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
        pixels = list(small.tobytes())
        width = size + 1
        bits = 0
        for row in range(size):
            for col in range(size):
                left = pixels[row * width + col]
                right = pixels[row * width + col + 1]
                bits = (bits << 1) | int(left > right)
        return bits

    @staticmethod
    def distance(h1: int, h2: int) -> int:
        return bin(h1 ^ h2).count("1")
