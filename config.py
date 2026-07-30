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
VAD_SPEECH_END_MS = _int("VAD_SPEECH_END_MS", 650)
BARGE_IN_MIN_MS = _int("BARGE_IN_MIN_MS", 500)
GRACE_AFTER_AI_END_MS = _int("GRACE_AFTER_AI_END_MS", 500)

SILENCE_TIMEOUT_SEC = _int("SILENCE_TIMEOUT_SEC", 10)
MAX_CALL_DURATION_SEC = _int("MAX_CALL_DURATION_SEC", 300)
RESPONSE_WATCHDOG_FIRST_SEC = _int("RESPONSE_WATCHDOG_FIRST_SEC", 5)
RESPONSE_WATCHDOG_SECOND_SEC = _int("RESPONSE_WATCHDOG_SECOND_SEC", 5)
