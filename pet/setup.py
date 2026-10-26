"""pet/setup.py — 首次启动设置弹窗：API 密钥 + 唤醒词称呼 + 音色。

只在没有存过密钥时自动弹出；之后每次启动静默读取。
托盘菜单"重新设置…"可随时再打开。
麦克风测试按钮录 2 秒报能量，帮助确认唤醒词能听到你说话。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

# 主动互动频率预设：(显示文案, mode key)，key 与 core.types.ProactiveConfig 对应
PROACTIVE_MODES: tuple[tuple[str, str], ...] = (
    ("不主动互动（只看不说）", "off"),
    ("低频（5–10 次/小时）", "low"),
    ("中频（10–20 次/小时）", "mid"),
    ("高频（20–30 次/小时）", "high"),
    ("自定义（自己填区间）", "custom"),
)


def _gap_text(lo: int, hi: int) -> str:
    """把 次/小时 区间换算成"大约隔多久说一句"的人话。"""
    if hi <= 0:
        return "不主动开口"
    short, long = 3600 / hi, 3600 / lo
    if short >= 90:                      # 一分钟以上就按分钟说
        return f"大约每 {short / 60:.0f}–{long / 60:.0f} 分钟说一句"
    return f"大约每 {short:.0f}–{long:.0f} 秒说一句"


class _MicTestWorker(QThread):
    """录 2 秒报能量（独立线程，避免卡 UI）。"""

    done = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop = False

    def run(self) -> None:
        try:
            import sounddevice as sd
            peak = 0.0
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                blocksize=1600) as stream:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not self._stop:
                    data, _ = stream.read(1600)
                    x = np.asarray(data, dtype=np.float32) / 32768.0
                    if x.size:
                        peak = max(peak, float(np.sqrt(np.mean(x * x))))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(f"麦克风测试失败: {exc}")
            return
        if peak < 0.005:
            verdict = f"峰值 {peak:.3f}——几乎没声音，检查麦克风/权限"
        elif peak < 0.05:
            verdict = f"峰值 {peak:.3f}——声音偏小，建议离麦克风近一点"
        else:
            verdict = f"峰值 {peak:.3f}——正常，可以听到你说话"
        self.done.emit(verdict)

    def stop(self) -> None:
        self._stop = True


class SetupDialog(QDialog):
    """首次设置。accept 后由调用方读字段并落盘。"""

    def __init__(self, voices_dir: Path | None = None,
                 current_key: str = "", current_wake: str = "小鲸鱼",
                 current_address: str = "主人", current_voice: str = "default",
                 current_max_tokens: int | None = None,
                 current_proactive_mode: str = "low",
                 current_proactive_min: int = 5, current_proactive_max: int = 10,
                 current_fullscreen_quiet: bool = False,
                 first_run: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("小鲸鱼 · 首次设置" if first_run else "小鲸鱼 · 重新设置")
        self.setMinimumWidth(420)
        self._mic_worker: _MicTestWorker | None = None

        intro = QLabel(
            "欢迎把小鲸鱼领回家～\n"
            "填入 DeepSeek API 密钥后，她就能自己看屏幕、听你说话了。\n"
            "（密钥只存在本机，之后启动不再询问）" if first_run else
            "修改下面的设置，保存后立即生效。")
        intro.setWordWrap(True)

        form = QFormLayout()

        self.edt_key = QLineEdit(current_key)
        self.edt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edt_key.setPlaceholderText("sk-…（DeepSeek 开放平台申请）")
        key_row = QVBoxLayout()
        key_row.setSpacing(2)
        key_row.addWidget(self.edt_key)
        self.chk_show = QCheckBox("显示密钥")
        self.chk_show.toggled.connect(self._toggle_key_echo)
        key_row.addWidget(self.chk_show)
        key_box = QWidget()
        key_box.setLayout(key_row)
        form.addRow("API 密钥", key_box)

        self.edt_wake = QLineEdit(current_wake)
        self.edt_wake.setPlaceholderText("小鲸鱼")
        self.edt_wake.textChanged.connect(self._update_wake_hint)
        form.addRow("唤醒词（她的称呼）", self.edt_wake)
        self.hint_wake = QLabel()
        self.hint_wake.setWordWrap(True)
        self.hint_wake.setStyleSheet("font-size: 11px;")
        form.addRow("", self.hint_wake)
        self._update_wake_hint()

        self.edt_address = QLineEdit(current_address)
        self.edt_address.setPlaceholderText("主人")
        form.addRow("她这样称呼你", self.edt_address)

        self.cmb_voice = QComboBox()
        self._fill_voices(voices_dir, current_voice)
        form.addRow("音色", self.cmb_voice)

        # 主动互动频率：预设区间 + 自定义最低/最高（次/小时）。
        # 到点会随机隔一段时间说一句；画面有大变动时也会额外触发。
        self.cmb_proactive = QComboBox()
        for label, key in PROACTIVE_MODES:
            self.cmb_proactive.addItem(label, key)
        idx = self.cmb_proactive.findData(current_proactive_mode)
        self.cmb_proactive.setCurrentIndex(idx if idx >= 0 else 1)
        form.addRow("主动互动", self.cmb_proactive)

        self.spn_min = QSpinBox()
        self.spn_min.setRange(1, 120)
        self.spn_min.setValue(max(1, int(current_proactive_min or 5)))
        self.spn_max = QSpinBox()
        self.spn_max.setRange(1, 120)
        self.spn_max.setValue(max(1, int(current_proactive_max or 10)))
        custom_row = QHBoxLayout()
        custom_row.setSpacing(4)
        custom_row.addWidget(QLabel("最低"))
        custom_row.addWidget(self.spn_min)
        custom_row.addWidget(QLabel("次/小时　最高"))
        custom_row.addWidget(self.spn_max)
        custom_row.addWidget(QLabel("次/小时"))
        custom_row.addStretch(1)
        self.box_custom = QWidget()
        self.box_custom.setLayout(custom_row)
        form.addRow("自定义区间", self.box_custom)

        self.hint_proactive = QLabel()
        self.hint_proactive.setWordWrap(True)
        self.hint_proactive.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", self.hint_proactive)

        # 全屏（看视频/玩游戏/演示）时的态度。默认不静音：用户反馈"看视频 6 分钟
        # 一声不响"——因为之前全屏被整体判成免打扰。
        self.chk_fullscreen = QCheckBox("全屏看视频 / 玩游戏时也不要说话")
        self.chk_fullscreen.setChecked(bool(current_fullscreen_quiet))
        form.addRow("全屏时", self.chk_fullscreen)

        self.cmb_proactive.currentIndexChanged.connect(self._update_proactive_hint)
        self.spn_min.valueChanged.connect(self._update_proactive_hint)
        self.spn_max.valueChanged.connect(self._update_proactive_hint)
        self._update_proactive_hint()

        # 输出上限：留空 = 不限制（不向 API 传 max_tokens，由模型自然收尾）。
        # 推理模型的思考 token 也计入上限，设太小会出现"想满了正文为空"（无气泡无语音），
        # 所以默认给空、由用户按需收紧。
        self.edt_maxtok = QLineEdit("" if current_max_tokens is None
                                    else str(current_max_tokens))
        self.edt_maxtok.setPlaceholderText("留空 = 不限制（推荐）")
        self.edt_maxtok.setValidator(QIntValidator(1, 1_000_000, self))
        self.edt_maxtok.textChanged.connect(self._update_maxtok_hint)
        form.addRow("最大输出 token", self.edt_maxtok)
        self.hint_maxtok = QLabel()
        self.hint_maxtok.setWordWrap(True)
        self.hint_maxtok.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", self.hint_maxtok)
        self._update_maxtok_hint()

        self.btn_mic = QPushButton("测试麦克风（2 秒）")
        self.btn_mic.clicked.connect(self._test_mic)
        self.lbl_mic = QLabel("")
        self.lbl_mic.setStyleSheet("color: gray; font-size: 11px;")
        mic_row = QVBoxLayout()
        mic_row.setSpacing(2)
        mic_row.addWidget(self.btn_mic)
        mic_row.addWidget(self.lbl_mic)
        mic_box = QWidget()
        mic_box.setLayout(mic_row)
        form.addRow("麦克风", mic_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._validate)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

    # ---- 交互 ----

    def _update_wake_hint(self) -> None:
        """唤醒词长度实时提示：2 字词识别容错空间小，提前告知。"""
        n = len(self.edt_wake.text().strip())
        if n < 2:
            self.hint_wake.setText("至少 2 个字，比如「小鲸鱼」。")
            self.hint_wake.setStyleSheet("color: #c0392b; font-size: 11px;")
        elif n == 2:
            self.hint_wake.setText("两个字偏短：环境吵或说话快时可能漏识别，"
                                   "3 字以上更稳（仍可保存使用）。")
            self.hint_wake.setStyleSheet("color: #d35400; font-size: 11px;")
        else:
            self.hint_wake.setText("长度合适。读起来顺口、和日常用语区分度大的词效果最好。")
            self.hint_wake.setStyleSheet("color: gray; font-size: 11px;")

    def _update_proactive_hint(self) -> None:
        """主动互动实时说明：把"次/小时"翻译成"多久说一句"，并管好自定义框可用性。"""
        mode = self.cmb_proactive.currentData()
        custom = mode == "custom"
        self.box_custom.setEnabled(custom)
        for w in (self.spn_min, self.spn_max):
            w.setEnabled(custom)

        if mode == "off":
            self.hint_proactive.setText(
                "不会自己开口；你跟她说话时照常回应。也不再看屏幕（省电省花费）。")
            self.hint_proactive.setStyleSheet("color: gray; font-size: 11px;")
            return

        if custom:
            lo, hi = self.spn_min.value(), self.spn_max.value()
            if lo > hi:
                self.hint_proactive.setText(
                    f"最低 {lo} 比最高 {hi} 还大，请改成最低 ≤ 最高。")
                self.hint_proactive.setStyleSheet("color: #c0392b; font-size: 11px;")
                return
            self.hint_proactive.setText(
                f"自定义：{_gap_text(lo, hi)}（{lo}–{hi} 次/小时）。"
                "每小时最多不超过最高值。")
            self.hint_proactive.setStyleSheet("color: gray; font-size: 11px;")
            return

        presets = {"low": (5, 10), "mid": (10, 20), "high": (20, 30)}
        lo, hi = presets.get(mode, (5, 10))
        # 预设档位把自定义框同步过去：之后想微调直接切「自定义」就行
        if (self.spn_min.value(), self.spn_max.value()) != (lo, hi):
            for widget, value in ((self.spn_min, lo), (self.spn_max, hi)):
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
        self.hint_proactive.setText(
            f"{_gap_text(lo, hi)}；画面有大变动时还会额外开口。")
        self.hint_proactive.setStyleSheet("color: gray; font-size: 11px;")

    def _toggle_key_echo(self, checked: bool) -> None:
        self.edt_key.setEchoMode(QLineEdit.EchoMode.Normal if checked
                                 else QLineEdit.EchoMode.Password)

    def _update_maxtok_hint(self) -> None:
        """输出上限实时说明：留空=不限制；填小了要提醒正文可能被挤空。"""
        raw = self.edt_maxtok.text().strip()
        if not raw:
            self.hint_maxtok.setText("不限制，让模型自己说完（推荐）。")
            self.hint_maxtok.setStyleSheet("color: gray; font-size: 11px;")
            return
        try:
            n = int(raw)
        except ValueError:
            self.hint_maxtok.setText("请填正整数，或留空 = 不限制。")
            self.hint_maxtok.setStyleSheet("color: #c0392b; font-size: 11px;")
            return
        if n < 600:
            self.hint_maxtok.setText(
                f"{n} 偏小：她「思考」的 token 也算在里面，"
                "可能想满了却没正文（表现为只有气泡没有话，或干脆没反应）。")
            self.hint_maxtok.setStyleSheet("color: #c0392b; font-size: 11px;")
        elif n < 1200:
            self.hint_maxtok.setText(f"{n}：够日常对话，偶尔长回答会被截断。")
            self.hint_maxtok.setStyleSheet("color: #d35400; font-size: 11px;")
        else:
            self.hint_maxtok.setText(f"{n}：有余量，普通问答不会变慢（说完就停）。")
            self.hint_maxtok.setStyleSheet("color: gray; font-size: 11px;")

    def _fill_voices(self, voices_dir: Path | None, current: str) -> None:
        names: list[str] = []
        if voices_dir and Path(voices_dir).is_dir():
            names = sorted(p.name for p in Path(voices_dir).iterdir() if p.is_dir())
        if not names:
            names = ["default"]
        self.cmb_voice.addItems(names)
        idx = self.cmb_voice.findText(current)
        if idx >= 0:
            self.cmb_voice.setCurrentIndex(idx)

    def _test_mic(self) -> None:
        self.btn_mic.setEnabled(False)
        self.lbl_mic.setText("录音中……请随便说几句话")
        self._mic_worker = _MicTestWorker(self)
        self._mic_worker.done.connect(self._mic_done)
        self._mic_worker.start()

    def _mic_done(self, text: str) -> None:
        self.lbl_mic.setText(text)
        self.btn_mic.setEnabled(True)

    # ---- 校验与取值 ----

    def _problems(self) -> list[str]:
        """返回阻断性问题列表（纯逻辑，不弹窗）；空列表 = 可以保存。"""
        problems: list[str] = []
        key = self.edt_key.text().strip()
        wake = self.edt_wake.text().strip()
        if not key:
            problems.append("请填入 DeepSeek API 密钥。")
        if len(wake) < 2:
            problems.append("唤醒词至少 2 个字，比如「小鲸鱼」。")
        tok = self.edt_maxtok.text().strip()
        if tok and (not tok.isdigit() or int(tok) <= 0):
            problems.append("最大输出 token 要么留空（= 不限制），要么填正整数。")
        if self.cmb_proactive.currentData() == "custom" \
                and self.spn_min.value() > self.spn_max.value():
            problems.append("主动互动的「最低」不能大于「最高」。")
        return problems

    def _looks_like_bad_key(self) -> bool:
        key = self.edt_key.text().strip()
        return not key.startswith("sk-") and len(key) < 20

    def _validate(self) -> None:
        problems = self._problems()
        if problems:
            QMessageBox.warning(self, "还差一点", "\n".join(problems))
            return
        if self._looks_like_bad_key():
            if QMessageBox.question(self, "密钥看起来不太对",
                                    "这不像一个有效的 DeepSeek 密钥（通常以 sk- 开头）。"
                                    "仍要保存吗？") != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def values(self) -> dict:
        raw_tok = self.edt_maxtok.text().strip()
        max_tokens = int(raw_tok) if raw_tok.isdigit() and int(raw_tok) > 0 else None
        return {
            "api_key": self.edt_key.text().strip(),
            "wake_phrase": self.edt_wake.text().strip(),
            "user_address": self.edt_address.text().strip() or "主人",
            "voice_name": self.cmb_voice.currentText(),
            "max_tokens": max_tokens,          # None = 不限制
            "proactive_mode": self.cmb_proactive.currentData(),
            "proactive_min": self.spn_min.value(),
            "proactive_max": self.spn_max.value(),
            "fullscreen_quiet": self.chk_fullscreen.isChecked(),
        }
