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

# 挨拶の即時再生（改善指示書「挨拶即時再生」）: 挨拶は事前生成済みクリップを
# Media Streamの`start`受信直後に再生し、OpenAI接続を待たない。
GREETING_AUDIO_PATH = os.getenv("GREETING_AUDIO_PATH", "assets/audio/greeting_o_matase.ulaw")
# OpenAI接続がこの秒数以内に完了しない場合は縮退運転（案内→切電→要折り返し
# ケース作成）に切り替える。
OPENAI_CONNECT_TIMEOUT_SEC = _int("OPENAI_CONNECT_TIMEOUT_SEC", 5)
DEGRADED_AUDIO_PATH = os.getenv("DEGRADED_AUDIO_PATH", "assets/audio/degraded_konzatsu.ulaw")

# メディア入力途絶の検知（2026-08-07障害対応）。通常時は無音でも50フレーム/秒
# 届くため、この秒数フレームが来なければ確実に異常。
MEDIA_STARVATION_TIMEOUT_SEC = _float("MEDIA_STARVATION_TIMEOUT_SEC", 5.0)
ENABLE_MEDIA_STARVATION_WATCHDOG = os.getenv("ENABLE_MEDIA_STARVATION_WATCHDOG", "true").strip().lower() == "true"

# 縮退運転・タイムアウト切電の案内クリップ。ファイルが無い場合は既存の
# _load_static_clip の仕様どおり再生をスキップして処理は続行される。
SILENCE_GOODBYE_AUDIO_PATH = os.getenv("SILENCE_GOODBYE_AUDIO_PATH", "assets/audio/silence_goodbye.ulaw")
ESCALATION_AUDIO_PATH = os.getenv("ESCALATION_AUDIO_PATH", "assets/audio/escalation_kikitorenai.ulaw")

# 検証専用: 指定秒数経過後、受信mediaフレームを意図的に無視して途絶を再現する。
# 0で無効（本番は必ず0）。
DEBUG_DROP_MEDIA_AFTER_SEC = _float("DEBUG_DROP_MEDIA_AFTER_SEC", 0.0)

# 受信フレームレートのログ間隔（秒）。0で無効。再現キャンペーン中は1、
# キャンペーン終了後は5に上げるか0で無効化する。
MEDIA_RATE_LOG_INTERVAL_SEC = _float("MEDIA_RATE_LOG_INTERVAL_SEC", 1.0)
