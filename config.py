import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# OpenAI（この新規プロジェクト専用のキー。既存システムとは共有しない＝コスト分離）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-1.5")

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# Railway
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
RECORDING_ACCESS_TOKEN = os.getenv("RECORDING_ACCESS_TOKEN", "")

# Salesforce
SF_USERNAME = os.getenv("SF_USERNAME")
SF_PASSWORD = os.getenv("SF_PASSWORD")
SF_TOKEN = os.getenv("SF_TOKEN", "")
SF_DOMAIN = os.getenv("SF_DOMAIN", "test")
SF_CASE_OWNER_ID = os.getenv("SF_CASE_OWNER_ID", "")
SF_CASE_RECORD_TYPE_ID = os.getenv("SF_CASE_RECORD_TYPE_ID", "")

# ログ・通知
GOOGLE_CHAT_WEBHOOK_URL = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")
RAILWAY_DASHBOARD_URL = os.getenv("RAILWAY_DASHBOARD_URL", "")

# ログDB（本番はRailwayボリューム、ローカル開発は環境変数で上書き）
CALL_LOG_DB_PATH = os.getenv("CALL_LOG_DB_PATH", "/data/call_log.db")

# VAD / タイムアウト チューニング（現場チューニング用に環境変数で調整可能）
VAD_THRESHOLD = _float("VAD_THRESHOLD", 0.5)
VAD_SPEECH_START_MS = _int("VAD_SPEECH_START_MS", 250)
# 650->500: 応答レイテンシ短縮のため終話判定を短縮（改善指示書 1-a）。
# 通常の語尾（「あの」「えっと」等の間を含む発話）を誤って切らないことを
# 実通話で確認済みの値。450はリスクが高いため採用しない。
VAD_SPEECH_END_MS = _int("VAD_SPEECH_END_MS", 500)
BARGE_IN_MIN_MS = _int("BARGE_IN_MIN_MS", 500)
GRACE_AFTER_AI_END_MS = _int("GRACE_AFTER_AI_END_MS", 500)

# Silero推論をイベントループから追い出す（2026-08-05障害の根本治療）。
# 同期onnx推論をイベントループ上で直接回すと、推論が詰まった際にループ全体
# （音声中継・無音タイマー・全ウォッチドッグ）が同時に凍結し、発話終了検知も
# 切電も止まる。根本修正のため既定でON。
VAD_INFERENCE_IN_THREAD = os.getenv("VAD_INFERENCE_IN_THREAD", "true").strip().lower() == "true"
# VAD推論専用スレッドプールのワーカー数。録音開始リトライ等の長時間タスクと
# 混ざらないよう分離するため、想定同時通話数程度を設定する。
VAD_EXECUTOR_MAX_WORKERS = _int("VAD_EXECUTOR_MAX_WORKERS", 4)
# VAD入力バッファの上限（秒）。入力が消費に追いつかない状況で溜め込み続けると
# 推論が実時間から遅れて発話終了判定が遅延するため、超過分は古い側から捨てる。
VAD_MAX_BUFFER_SEC = _float("VAD_MAX_BUFFER_SEC", 1.0)

SILENCE_TIMEOUT_SEC = _int("SILENCE_TIMEOUT_SEC", 10)
MAX_CALL_DURATION_SEC = _int("MAX_CALL_DURATION_SEC", 300)
RESPONSE_WATCHDOG_FIRST_SEC = _int("RESPONSE_WATCHDOG_FIRST_SEC", 5)
RESPONSE_WATCHDOG_SECOND_SEC = _int("RESPONSE_WATCHDOG_SECOND_SEC", 5)

# 応答冒頭の頭切れ対策（改善指示書 2-b）: 各応答の最初の音声フレームを送る前に
# 挿入する無音(μ-law 0xFF)の長さ。出力ストリームが立ち上がる間の頭切れを吸収する。
RESPONSE_LEAD_SILENCE_MS = _int("RESPONSE_LEAD_SILENCE_MS", 250)

# 即時相槌（改善指示書 1-b、オプション機能）。true にすると commit +
# response.create 直後に事前録音の短い相槌音声を即座に再生し、モデルの
# 本応答が生成されるまでの無音区間を埋める。
ENABLE_FILLER = os.getenv("ENABLE_FILLER", "false").strip().lower() == "true"
FILLER_AUDIO_PATH = os.getenv("FILLER_AUDIO_PATH", "assets/audio/aizuchi_kashikomarimashita.ulaw")

# 切断主体の推定記録（修正指示書パートA）。trueのときのみ /call-status を受け付け、
# 通話終了時に [DISCONNECT] 分類ログを1行出す。分類はサーバー内部状態
# （OA接続到達・VAD発話有無・mark進捗）が主で、Twilioのコールバックは補助情報。
# Twilioは「どちらが切ったか」を返さないため、あくまで推定である点に注意。
DISCONNECT_TRACKING_ENABLED = os.getenv("DISCONNECT_TRACKING_ENABLED", "false").strip().lower() == "true"

# 録音開始のリトライ（修正指示書パートB）。通話がin-progressへ完全に遷移する前に
# recordings.create()を叩くとTwilioは21220（Requested resource is not eligible
# for recording）で拒否する。安全側の修正のためデフォルトで有効。
RECORDING_RETRY_ENABLED = os.getenv("RECORDING_RETRY_ENABLED", "true").strip().lower() == "true"
RECORDING_MAX_RETRIES = _int("RECORDING_MAX_RETRIES", 4)
RECORDING_RETRY_BACKOFF_MS = _int("RECORDING_RETRY_BACKOFF_MS", 300)

# イベントループのブロック監視（最終安全網）。ループ外の別スレッドから
# ハートビートの鮮度を見張り、停止していればプロセスを落として再起動させる。
# 閾値は「正常な処理では絶対に到達しない値」であることが重要（誤発火すると
# 進行中の他通話を巻き添えにする）。15秒より短くしないこと。
LOOP_HEARTBEAT_ENABLED = os.getenv("LOOP_HEARTBEAT_ENABLED", "true").strip().lower() == "true"
LOOP_HEARTBEAT_STALL_SEC = _float("LOOP_HEARTBEAT_STALL_SEC", 15.0)

# 挨拶の即時再生（改善指示書「挨拶即時再生」）: 挨拶は事前生成済みクリップを
# Media Streamの`start`受信直後に再生し、OpenAI接続を待たない。
GREETING_AUDIO_PATH = os.getenv("GREETING_AUDIO_PATH", "assets/audio/greeting_o_matase.ulaw")
# OpenAI接続がこの秒数以内に完了しない場合は縮退運転（案内→切電→要折り返し
# ケース作成）に切り替える。
OPENAI_CONNECT_TIMEOUT_SEC = _int("OPENAI_CONNECT_TIMEOUT_SEC", 5)
DEGRADED_AUDIO_PATH = os.getenv("DEGRADED_AUDIO_PATH", "assets/audio/degraded_konzatsu.ulaw")
