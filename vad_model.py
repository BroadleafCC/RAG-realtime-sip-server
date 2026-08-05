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
    （内部状態はストリーム単位で意味を持つ）。

    **スレッドセーフではない。** `feed()` は呼び出し側でワーカースレッドへ
    逃がすが（call_session._vad_feed）、同一インスタンスの `feed` を並行発火
    させてはならない。`pump_twilio_to_openai` の単一whileループから逐次
    `await` することで逐次実行が保証されている。この逐次性を壊さないこと。
    """

    def __init__(self, max_buffer_sec: float = 1.0):
        self._model = load_silero_vad(onnx=True)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._max_buffer_sec = max_buffer_sec
        self._dropped_samples_total = 0

    @property
    def dropped_samples_total(self) -> int:
        """バッファ上限を超えて破棄したサンプル数の累計（1通話ぶんの累積）。
        呼び出し側が監視ログに使う。"""
        return self._dropped_samples_total

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

        同期のonnx推論を含むため、イベントループ上で直接呼ばないこと
        （2026-08-05障害。クラスdocstring参照）。
        """
        samples = pcm16_to_float32(pcm16_bytes)
        self._buffer = np.concatenate([self._buffer, samples])

        # バッファ上限ガード。入力ペースが消費ペースを上回ると、1回のfeedで
        # while反復が増え→推論が長引き→さらに溜まる、という悪循環に入る。
        # リアルタイム音声では遅れた古い音声を処理し続けるより、最新に追いつく
        # ほうが正しいので、超過分は古い側から捨てる。
        max_buffer_samples = int(SAMPLE_RATE * self._max_buffer_sec)
        if len(self._buffer) > max_buffer_samples:
            dropped = len(self._buffer) - max_buffer_samples
            self._buffer = self._buffer[dropped:]
            # ここではログを出さない（毎フレームのログ洪水を避けるため）。
            # 破棄量を累計しておき、呼び出し側が間引いてログする。
            self._dropped_samples_total += dropped

        results: list[tuple[float, float]] = []
        while len(self._buffer) >= VAD_CHUNK_SAMPLES:
            chunk = self._buffer[:VAD_CHUNK_SAMPLES]
            self._buffer = self._buffer[VAD_CHUNK_SAMPLES:]
            prob = float(self._model(chunk[np.newaxis, :], SAMPLE_RATE).item())
            results.append((prob, VAD_CHUNK_SAMPLES / SAMPLE_RATE))
        return results
