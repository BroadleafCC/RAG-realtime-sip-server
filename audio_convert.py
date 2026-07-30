"""
Twilio Media Streams から届く mu-law 8kHz 音声を Silero VAD 解析用に変換する。

重要: このモジュールは Twilio <-> OpenAI の本流音声パス（base64 mu-law の
そのまま中継）には一切使わない。VAD 解析用のサイドタップ専用。

silero-vad-notorch の ONNX モデルは 8kHz を直接サポートしている
（256サンプル/チャンク、sr=8000 で呼び出し可能）ため、16kHzへの
リサンプリングは行わない。リサンプリングフィルタが持ち込みうる
アーティファクトを避けつつ、状態管理も単純になる。
"""
try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+ では stdlib から削除された
    import audioop_lts as audioop

import numpy as np

# Silero VAD ONNX モデルが sr=8000 で要求する固定チャンクサイズ
VAD_CHUNK_SAMPLES = 256


def ulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """mu-law (8bit) -> PCM16 (little-endian) に変換する"""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm16_to_float32(pcm16_bytes: bytes) -> np.ndarray:
    """PCM16バイト列 -> Silero VADが期待する [-1.0, 1.0] float32 配列に変換する"""
    ints = np.frombuffer(pcm16_bytes, dtype="<i2")
    return ints.astype(np.float32) / 32768.0
