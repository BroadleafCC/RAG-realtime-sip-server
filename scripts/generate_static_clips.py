"""
事前生成する静的音声クリップ（挨拶・即時相槌・縮退運転案内）をまとめて生成
するスクリプト。

OpenAIのTTS API（audio.speech）で各フレーズを合成し、Twilio Media Streams
が要求するμ-law 8kHzモノラルの生バイト列に変換してassets/audio/以下に
保存する。session側の音声（realtimeの"coral"ボイス）とトーンを合わせるため、
全クリップで同じ"coral"ボイスを使う（改善指示書「挨拶即時再生」1章の
「クリップの声はモデルの応答と同じボイスで生成すること」要件）。

フレーズや声を変更したい場合はこのスクリプトのCLIPS定義を編集して再実行
すればよい（生成物はgit管理下のバイナリなので、再実行すれば上書きされる）。
挨拶クリップのテキストはopenai_client.GREETING_TEXTと必ず一致させること
（モデルの会話履歴に注入する文言と実際に再生される音声を揃えるため）。

実行方法:
    python scripts/generate_static_clips.py
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
from openai_client import GREETING_TEXT

VOICE = "coral"  # openai_client.pyのbuild_session_updateと同じボイスに揃える
MODEL = "gpt-4o-mini-tts"
TARGET_SR = 8000
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "audio")

CLIPS = [
    {
        "name": "挨拶",
        "text": GREETING_TEXT,
        "speed": 1.3,  # 3.3秒は間延びして聞こえたため、2.5秒程度になるよう早める
        "output": "greeting_o_matase.ulaw",
    },
    {
        "name": "即時相槌",
        "text": "かしこまりました",
        "speed": 1.3,  # 相槌はテンポよく聞こえた方が「即応感」が出るため早口にする
        "output": "aizuchi_kashikomarimashita.ulaw",
    },
    {
        "name": "縮退運転案内",
        "text": "恐れ入ります。ただいま電話が大変混み合っております。折り返しご連絡いたしますので、お電話を切ってお待ちください。",
        "speed": 1.0,
        "output": "degraded_konzatsu.ulaw",
    },
    # 以下2本は2026-08-07障害対応で追加。切電案内をOpenAI非依存にするため、
    # 無音・最大通話時間・縮退運転（応答なし／メディア入力途絶）の各経路で
    # これらのクリップを再生する（watchdogs._play_goodbye_clip）。
    {
        "name": "無音・最大時間の切電案内",
        "text": "お声が聞こえませんので失礼いたします。ありがとうございました。",
        "speed": 1.0,
        "output": "silence_goodbye.ulaw",
    },
    {
        "name": "聞き取り不能時の縮退運転案内",
        "text": "お電話が遠いようで、うまく聞き取れませんでした。この番号の担当者から改めてご連絡いたしますので、恐れ入りますが一度お電話をお切りください。",
        "speed": 1.0,
        "output": "escalation_kikitorenai.ulaw",
    },
    # 印字ズレ対応のFAX番号案内（function calling化対応で追加）。モデルに
    # その場で数字を読み上げさせると速度が制御できないため、事前録音した
    # このクリップをcall_session._handle_function_callが再生する。番号のみを
    # 収録し、「専任の担当者から〜」の案内文は含めない（数字を含まないため
    # モデル自身に喋らせ、聞き直し要求時にクリップだけを短く再生できるように
    # するため）。テキストはcall_session.FAX_SPOKEN_TEXTと必ず一致させること。
    {
        "name": "FAX番号案内",
        "text": "ゼロ、サン、ゴー、ナナ、ハチ、イチ、サン、ニー、ロク、ゼロ。",
        "speed": 0.75,  # 番号を正確に聞き取れるよう、通常のクリップよりゆっくり読み上げる
        "output": "fax_number.ulaw",
    },
]


def synthesize(client: OpenAI, text: str, speed: float) -> bytes:
    """TTSで合成し、μ-law 8kHzモノラルの生バイト列（無音トリム済み）を返す。"""
    response = client.audio.speech.create(
        model=MODEL,
        voice=VOICE,
        input=text,
        response_format="wav",
        speed=speed,
    )
    wav_bytes = response.read()

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    print(f"  [TTS] 受信: channels={n_channels} sampwidth={sampwidth} framerate={framerate} bytes={len(pcm)}")

    if n_channels == 2:
        pcm = audioop.tomono(pcm, sampwidth, 0.5, 0.5)
    if sampwidth != 2:
        raise RuntimeError(f"想定外のsampwidth: {sampwidth}")

    if framerate != TARGET_SR:
        pcm, _ = audioop.ratecv(pcm, sampwidth, 1, framerate, TARGET_SR, None)

    pcm = _trim_silence(pcm, sampwidth)
    return audioop.lin2ulaw(pcm, sampwidth)


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


def main():
    client = OpenAI(api_key=config.OPENAI_API_KEY)
    os.makedirs(ASSETS_DIR, exist_ok=True)

    for clip in CLIPS:
        print(f"[TTS] 合成中: name={clip['name']} phrase={clip['text']!r} voice={VOICE} speed={clip['speed']}")
        ulaw = synthesize(client, clip["text"], clip["speed"])
        output_path = os.path.join(ASSETS_DIR, clip["output"])
        with open(output_path, "wb") as f:
            f.write(ulaw)
        duration_sec = len(ulaw) / TARGET_SR
        print(f"  [OK] 保存しました: {output_path} ({len(ulaw)} bytes, {duration_sec:.3f}秒)")


if __name__ == "__main__":
    main()
