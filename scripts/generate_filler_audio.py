"""
即時相槌（ENABLE_FILLER機能、改善指示書1-b）用の音声アセットを生成するスクリプト。

OpenAIのTTS API（audio.speech）で短い相槌フレーズを合成し、Twilio Media
Streamsが要求するμ-law 8kHzモノラルの生バイト列に変換して
assets/audio/ 以下に保存する。session側の音声（realtimeの"coral"ボイス）と
トーンを合わせるため、同じ"coral"ボイスを使う。

フレーズや声を変更したい場合はこのスクリプトを編集して再実行すればよい
（生成物はgit管理下のバイナリなので、再実行すれば上書きされる）。

実行方法:
    python scripts/generate_filler_audio.py
"""
import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+
    import audioop_lts as audioop

from openai import OpenAI

import config

PHRASE = "かしこまりました"
VOICE = "coral"  # openai_client.pyのbuild_session_updateと同じボイスに揃える
MODEL = "gpt-4o-mini-tts"
SPEED = 1.3  # 相槌はテンポよく聞こえた方が「即応感」が出るため、やや早口にする
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "audio", "aizuchi_kashikomarimashita.ulaw",
)
TARGET_SR = 8000


def main():
    client = OpenAI(api_key=config.OPENAI_API_KEY)

    print(f"[TTS] 合成中: phrase={PHRASE!r} voice={VOICE} model={MODEL} speed={SPEED}")
    response = client.audio.speech.create(
        model=MODEL,
        voice=VOICE,
        input=PHRASE,
        response_format="wav",
        speed=SPEED,
    )
    wav_bytes = response.read()

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    print(f"[TTS] 受信: channels={n_channels} sampwidth={sampwidth} framerate={framerate} bytes={len(pcm)}")

    # ステレオならモノラルへダウンミックス
    if n_channels == 2:
        pcm = audioop.tomono(pcm, sampwidth, 0.5, 0.5)

    # 16bit以外ならここでは想定外（OpenAI wavは通常16bit PCM）
    if sampwidth != 2:
        raise RuntimeError(f"想定外のsampwidth: {sampwidth}")

    # 8kHzへリサンプリング
    if framerate != TARGET_SR:
        pcm, _ = audioop.ratecv(pcm, sampwidth, 1, framerate, TARGET_SR, None)

    # 前後の無音をトリム（相槌は短く即応させたいため）
    pcm = _trim_silence(pcm, sampwidth)

    ulaw = audioop.lin2ulaw(pcm, sampwidth)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "wb") as f:
        f.write(ulaw)

    duration_sec = len(ulaw) / TARGET_SR
    print(f"[OK] 保存しました: {OUTPUT_PATH} ({len(ulaw)} bytes, {duration_sec:.3f}秒)")


def _trim_silence(pcm: bytes, sampwidth: int, threshold: int = 400, pad_ms: int = 30) -> bytes:
    """先頭・末尾の無音区間を振幅しきい値でトリムする（簡易版）。"""
    import array

    samples = array.array("h" if sampwidth == 2 else "b", pcm)
    n = len(samples)
    start = 0
    while start < n and abs(samples[start]) < threshold:
        start += 1
    end = n
    while end > start and abs(samples[end - 1]) < threshold:
        end -= 1

    pad_samples = int(TARGET_SR * pad_ms / 1000)
    start = max(0, start - pad_samples)
    end = min(n, end + pad_samples)
    if start >= end:
        return pcm  # 全て無音扱いになった場合は元データを返す（安全側）
    return samples[start:end].tobytes()


if __name__ == "__main__":
    main()
