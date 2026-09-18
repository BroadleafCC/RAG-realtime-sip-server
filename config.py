import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# OpenAI（この新規プロジェクト専用のキー。既存システムとは共有しない＝コスト分離）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# 2026-07リリースの Realtime 2.1 系へ移行。p95レイテンシが約25%改善し、英数字認識・
# 無音/ノイズ処理・割り込み挙動も改善されている（いずれも本システムの弱点そのもの）。
# miniを既定にするのはコスト優先の運用判断。ただしminiは「function callingが発火
# しなくなった」というコミュニティ報告があるため、FAX番号案内(play_fax_number)が
# 動かない場合はRailwayの環境変数で gpt-realtime-2.1（非mini）へ、それでも駄目なら
# gpt-realtime-1.5 へ即座に切り戻すこと。
# 注意: Railway側に OPENAI_REALTIME_MODEL が明示設定されている場合、このデフォルトは
# 使われない（環境変数の値が優先される）。
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# Railway
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")

# 録音再生URLの署名鍵。未設定のままデプロイされると「録音URLだけ動かない」
# という気付きにくい不具合になるため、起動時点で明確に落とす（os.environ[]）。
RECORDING_LINK_SECRET = os.environ["RECORDING_LINK_SECRET"]
RECORDING_LINK_MAX_AGE_SEC = 7 * 24 * 60 * 60  # 7日間（168時間）固定。運用決定事項のため定数化

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

# 管理ダッシュボード(/admin/dashboard)。移植元と同じく、未設定の場合は
# プロンプト編集とコスト設定保存が誰にも通らない（画面と参照系は無認証）。
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

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

# 印字ズレ対応のFAX番号案内。モデルにその場で数字を読み上げさせると速度が
# 制御できないため、function calling（play_fax_number）で事前録音クリップを
# 再生する（call_session.py参照）。
FAX_AUDIO_PATH = os.getenv("FAX_AUDIO_PATH", "assets/audio/fax_number.ulaw")

# AgentSearch（Vertex AI Search / faq-search-app、Generative Answers有効）。
# 認証はgoogle-authのADC(Application Default Credentials)に委ねるため、
# サービスアカウントキーはコード側では読まない。Railwayでは環境変数
# GOOGLE_APPLICATION_CREDENTIALS_JSON にキーJSONの中身をそのまま貼り、
# 起動時にファイル化してGOOGLE_APPLICATION_CREDENTIALSへ渡す運用を想定
# （Railwayにはローカルの秘密ファイルを直接置けないため）。
AGENTSEARCH_PROJECT_ID = os.getenv("AGENTSEARCH_PROJECT_ID", "")
AGENTSEARCH_LOCATION = os.getenv("AGENTSEARCH_LOCATION", "global")
AGENTSEARCH_ENGINE_ID = os.getenv("AGENTSEARCH_ENGINE_ID", "")
# 指示書(agentsearch-integration-phase1.md 4章)の「タイムアウト値は未定、
# 仮に8秒」を踏襲。正式値はPhase 1完了後の検討課題。
AGENTSEARCH_TIMEOUT_SEC = _float("AGENTSEARCH_TIMEOUT_SEC", 8.0)

# search_faq検索中に流す保留音楽（μ-law 8kHz生データ）。ファイルが無い場合は
# 既存の_load_static_clipの仕様どおり再生をスキップし、無音のまま検索を待つ
# だけになる（動作は変わらない）。
HOLD_MUSIC_AUDIO_PATH = os.getenv("HOLD_MUSIC_AUDIO_PATH", "assets/audio/hold_music.ulaw")

# 検証専用: 指定秒数経過後、受信mediaフレームを意図的に無視して途絶を再現する。
# 0で無効（本番は必ず0）。
DEBUG_DROP_MEDIA_AFTER_SEC = _float("DEBUG_DROP_MEDIA_AFTER_SEC", 0.0)

# 受信フレームレートのログ間隔（秒）。0で無効。再現キャンペーン中は1、
# キャンペーン終了後は5に上げるか0で無効化する。
MEDIA_RATE_LOG_INTERVAL_SEC = _float("MEDIA_RATE_LOG_INTERVAL_SEC", 1.0)

# 待機モード（enter_standby_mode、システム操作の時間が欲しいと言われた場合）の
# タイムアウト設定。通常の無音タイムアウト(SILENCE_TIMEOUT_SEC)とは独立して
# 動作する（watchdogs.standby_watchdog参照。silence_watchdogは待機モード中は
# 自身を抑止する）。
STANDBY_CHECKPOINT_SEC = _int("STANDBY_CHECKPOINT_SEC", 45)
STANDBY_TIMEOUT_SEC = _int("STANDBY_TIMEOUT_SEC", 60)
# 初回のenter_standby_mode呼び出し1回+延長2回=合計3回まで許可する
STANDBY_MAX_ENTRIES = _int("STANDBY_MAX_ENTRIES", 3)
# 45秒チェックイン時にモデルへ送るトリガーメッセージ。プロンプト側の
# 「【待機45秒経過】というメッセージが届いたら...」の記述と文言を必ず一致させること。
STANDBY_CHECKPOINT_TRIGGER_TEXT = os.getenv("STANDBY_CHECKPOINT_TRIGGER_TEXT", "【待機45秒経過】")

# Smart Turn v3（発話終端検出モデル）。無音がVAD_SPEECH_END_MSに達するのを
# 待たずに、発話内容(音響的特徴)から「言い切ったか」を推論して早期に
# END_OF_SPEECHを発火させる。推論失敗/タイムアウト/incomplete判定時は
# 何もせず、既存のVAD_SPEECH_END_MS固定閾値にそのままフォールバックする
# （smart-turn-integration-instructions.md 参照。ただし同指示書の
# 「mode=default/longformの2モード」は本システムに存在しないため、
# 上限値は単一のVAD_SPEECH_END_MSのみを使う）。
SMART_TURN_ENABLED = os.getenv("SMART_TURN_ENABLED", "false").strip().lower() == "true"
# シャドーモード: 推論結果を[SMART-TURN]ログに出すだけで、実際の
# END_OF_SPEECH発火タイミングには反映しない。実通話で精度を確認してから
# falseに切り替える運用を想定（ロールアウト計画1〜2章）。
SMART_TURN_SHADOW_MODE = os.getenv("SMART_TURN_SHADOW_MODE", "true").strip().lower() == "true"
SMART_TURN_COMPLETE_THRESHOLD = _float("SMART_TURN_COMPLETE_THRESHOLD", 0.7)
SMART_TURN_INFER_TIMEOUT_MS = _int("SMART_TURN_INFER_TIMEOUT_MS", 200)
# 無音開始からこの時間が経過した最初のフレームで推論を1回だけキックする。
# VAD_SPEECH_END_MSより十分小さい値にすること（起動時にチェックする）。
SMART_TURN_TRIGGER_SILENCE_MS = _int("SMART_TURN_TRIGGER_SILENCE_MS", 200)
SMART_TURN_MAX_BUFFER_SEC = _float("SMART_TURN_MAX_BUFFER_SEC", 8.0)
SMART_TURN_MODEL_PATH = os.getenv("SMART_TURN_MODEL_PATH", "assets/models/smart-turn-v3.2-cpu.onnx")
