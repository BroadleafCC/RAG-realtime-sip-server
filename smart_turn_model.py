"""Smart Turn v3（発話終端検出モデル）のONNX推論ラッパー。

Pipecat AI製のsmart-turn-v3(BSD-2-Clause)をベースにするが、Pipecatの
パイプラインフレームワーク(Frame/FrameProcessor/BaseTurnAnalyzer)には
依存しない。本プロジェクトは既にvad.py/call_session.py側で発話バッファリング・
非同期実行・タイムアウト・レース制御を持っているため、前処理(リサンプル・
8秒への切り詰め/ゼロパディング・ログメル特徴抽出)とONNX推論だけを
薄く提供する（smart_turn/THIRD_PARTY_NOTICE.md参照）。

vad_model.SileroVadと違い、状態を持たない(1プロセスにつき1インスタンスを
全通話で共有してよい)。呼び出し元(call_session.py)が
asyncio.to_thread()経由で呼ぶことを前提とし、このモジュール自体は
同期・ブロッキングなコードのみで構成する。
"""
try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+ では stdlib から削除された
    import audioop_lts as audioop

import logging
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort

from audio_convert import pcm16_to_float32
from smart_turn._whisper_features import compute_whisper_log_mel_features

logger = logging.getLogger("smart_turn_model")

_INPUT_SAMPLE_RATE = 8000
_MODEL_SAMPLE_RATE = 16000
_MODEL_MAX_SAMPLES = _MODEL_SAMPLE_RATE * 8  # 8秒分(128000サンプル)


@dataclass
class SmartTurnResult:
    prediction: int  # 1=complete, 0=incomplete（モデル自身の0.5閾値、ログ用）
    probability: float  # completeである確率


class SmartTurnDetector:
    """1プロセスにつき1インスタンス。ONNXセッションはスレッドセーフな
    Run()呼び出しを前提に複数通話間で共有する。"""

    def __init__(self, model_path: str | None = None, cpu_count: int = 1, session=None):
        """session を渡した場合はモデルファイルをロードせずそれを使う
        （テスト用のフェイクセッション注入口）。"""
        if session is not None:
            self._session = session
            return
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = cpu_count
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(model_path, sess_options=so)

    def predict(self, pcm16_8k_bytes: bytes) -> SmartTurnResult:
        """8kHz PCM16の発話バッファを受け取り、complete/incompleteを推論する。

        呼び出し側でasyncio.to_thread()に包むこと（ブロッキング処理）。
        """
        pcm16_16k, _ = audioop.ratecv(pcm16_8k_bytes, 2, 1, _INPUT_SAMPLE_RATE, _MODEL_SAMPLE_RATE, None)
        audio = pcm16_to_float32(pcm16_16k)
        audio = _truncate_or_pad(audio, _MODEL_MAX_SAMPLES)

        log_mel = compute_whisper_log_mel_features(audio, do_normalize=True)
        input_features = np.expand_dims(log_mel, axis=0)  # (1, 80, 800)

        outputs = self._session.run(None, {"input_features": input_features})
        probability = float(outputs[0][0, 0])
        prediction = 1 if probability > 0.5 else 0
        return SmartTurnResult(prediction=prediction, probability=probability)


def _truncate_or_pad(audio: np.ndarray, max_samples: int) -> np.ndarray:
    """直近max_samples分に切り詰める（末尾を残す）。不足分は先頭にゼロ
    パディングする（pipecat-ai local_smart_turn_v3.pyのtruncate_audio_to_last_n_secondsと同じ挙動）。"""
    if len(audio) > max_samples:
        return audio[-max_samples:]
    if len(audio) < max_samples:
        return np.pad(audio, (max_samples - len(audio), 0), mode="constant", constant_values=0)
    return audio


_detector: SmartTurnDetector | None = None


def preload(model_path: str, cpu_count: int = 1) -> None:
    """起動時に一度だけ呼ぶ（main.pyのlifespanから、SMART_TURN_ENABLED=true時のみ）。"""
    global _detector
    if _detector is None:
        _detector = SmartTurnDetector(model_path, cpu_count=cpu_count)
        logger.info("[SMART-TURN] モデルをロードしました path=%s", model_path)


def get_detector() -> SmartTurnDetector | None:
    """未ロード(SMART_TURN_ENABLED=falseだった、またはpreload失敗)の場合はNone。
    呼び出し側はNoneをフォールバック対象として扱うこと。"""
    return _detector
