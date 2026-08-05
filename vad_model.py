"""
Silero VAD (ONNX, torch非依存) の薄いラッパー。

依存関係の注意: 本家 `silero-vad` PyPIパッケージは torch/torchaudio を
無条件で要求するため使わない。`silero-vad-notorch`（同じロジックを
ONNXのみで動かすフォーク、torch/torchaudio非依存）を使用する。
"""
import numpy as np
from silero_vad_notorch import load_silero_vad

from audio_convert import VAD_CHUNK_SAMPLES, pcm16_to_float32

SAMPLE_RATE = 8000


class SileroVad:
    """1通話につき1インスタンス。複数通話間でインスタンスを共有しないこと
    （内部状態はストリーム単位で意味を持つ）。"""

    def __init__(self):
        self._model = load_silero_vad(onnx=True)
        self._buffer = np.zeros(0, dtype=np.float32)

    def reset(self):
        self._model.reset_states()
        self._buffer = np.zeros(0, dtype=np.float32)

    def feed(self, pcm16_bytes: bytes) -> list[tuple[float, float]]:
        """PCM16音声を追加し、256サンプル貯まるごとに推論する。

        戻り値: [(speech_probability, chunk_duration_sec), ...]
        20msフレーム(160サンプル@8kHz)と256サンプルのチャンクサイズは
        割り切れないため、呼び出しごとに0〜2個の結果が返る。
        呼び出し側は chunk_duration_sec を実経過時間として積算すること
        （フレーム数を1:1で仮定しない）。
        """
        samples = pcm16_to_float32(pcm16_bytes)
        self._buffer = np.concatenate([self._buffer, samples])

        results: list[tuple[float, float]] = []
        while len(self._buffer) >= VAD_CHUNK_SAMPLES:
            chunk = self._buffer[:VAD_CHUNK_SAMPLES]
            self._buffer = self._buffer[VAD_CHUNK_SAMPLES:]
            prob = float(self._model(chunk[np.newaxis, :], SAMPLE_RATE).item())
            results.append((prob, VAD_CHUNK_SAMPLES / SAMPLE_RATE))
        return results
