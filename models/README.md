# models/ — 模型权重放这里（不进 Git）

三个模型加起来约 800 MB，所以**不随仓库分发**。首次运行前下好，否则会有功能缺失：

```bat
pip install huggingface_hub
python scripts/download_models.py
```

只想下某一个：`python scripts/download_models.py --only vosk`（可选 `moss` / `codec` / `vosk`）。

## 目录要求

```
models/
├── moss-tts-nano-onnx/                 # 语音合成主模型（641 MB）
│   ├── moss_tts_prefill.onnx
│   ├── moss_tts_local_decoder.onnx
│   ├── moss_tts_decode_step.onnx
│   ├── moss_tts_local_fixed_sampled_frame.onnx
│   ├── *.data
│   └── tokenizer.model
├── moss-audio-tokenizer-nano-onnx/     # 音频编解码器（86 MB）
│   ├── moss_audio_tokenizer_encode.onnx
│   ├── moss_audio_tokenizer_decode_step.onnx
│   └── *.data
├── vosk-small-cn/                      # 唤醒词识别（65 MB）
│   ├── am/  conf/  graph/  ivector/
└── moss-voices/                        # 音色参考音频（自己注册，见 README）
    └── <音色名>/reference.wav + meta.json
```

## 手动下载

| 模型 | 来源 | 放到 |
|---|---|---|
| MOSS-TTS-Nano-100M-ONNX | <https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX> | `moss-tts-nano-onnx/` |
| MOSS-Audio-Tokenizer-Nano-ONNX | <https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX> | `moss-audio-tokenizer-nano-onnx/` |
| vosk-model-small-cn-0.22 | <https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip> | 解压后把 `vosk-model-small-cn-0.22/` 里的内容放进 `vosk-small-cn/` |

HuggingFace 页面里点「Files and versions」逐个下载也行，或用命令行：

```bat
huggingface-cli download OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX --local-dir models/moss-tts-nano-onnx
huggingface-cli download OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX --local-dir models/moss-audio-tokenizer-nano-onnx
```

## 缺了会怎样

| 缺哪个 | 后果 |
|---|---|
| `vosk-small-cn` | 喊名字没反应（唤醒词识别不可用），打字对话仍然能聊 |
| `moss-tts-nano-onnx` / `moss-audio-tokenizer-nano-onnx` | 自动降级到 Windows SAPI5 机器人音，能说话但不好听 |
| `moss-voices/` 为空 | 同上，先用 SAPI；`python scripts/register_voice.py` 注册自己的音色 |

许可：MOSS 系列模型与运行时为 Apache-2.0，Vosk 模型为 Apache-2.0，详见仓库根目录 `NOTICE`。
