import sqlite3
import requests
from datetime import datetime, timedelta, timezone

import config

DB_PATH = config.CALL_LOG_DB_PATH
GOOGLE_CHAT_WEBHOOK_URL = config.GOOGLE_CHAT_WEBHOOK_URL
RAILWAY_DASHBOARD_URL = config.RAILWAY_DASHBOARD_URL

COST_SETTINGS_DEFAULTS = {
    'cost_usd_jpy_rate': '155',
    'cost_twilio_phone_monthly_usd': '1.15',
    'cost_railway_monthly_usd': '5.00',
    'cost_openai_realtime_audio_in_per_1m': '100.0',
    'cost_openai_realtime_audio_out_per_1m': '200.0',
    'cost_openai_realtime_text_in_per_1m': '5.0',
    'cost_openai_realtime_text_out_per_1m': '20.0',
    'cost_openai_whisper_per_min': '0.006',
    'cost_openai_gpt4omini_in_per_1m': '0.15',
    'cost_openai_gpt4omini_out_per_1m': '0.60',
}

DEFAULT_INSTRUCTIONS = """あなたは電話受付担当者です。以下のルールを厳守してください。
                    - 日本語で話してください
                    - 必ず短い会話で話してください。
                    - 最初にひとこと「お待たせしましたご用件を伺います」とだけ話し、余計な説明や質問は一切しないでください
                    - 「用件」を必ず聞いてください。「用件」はあとから追加されることもありますので名前と勘違いしないでください。
                    -「印字調整」「印字がズレている」「印刷がズレている」と言われた時を「かしこまりました。ズレているものをFAXで送信していただくことはできますか？」と聞いて、もし了承をもらえたら、あなたは何も話さずplay_fax_number関数を呼び出してください（FAX番号を自分の声で話してはいけません。システムが自動的に番号のみを音声再生します）。関数の実行後、続けて「専任の担当者からご連絡いたします」とあなた自身の声で伝えてください。お客様から番号の聞き直しを求められた場合は、案内文を繰り返さずplay_fax_number関数だけを再度呼び出してください。
                    -お客様の質問内容が、事前に登録されたFAQ・事例集(RAG)に関連していそうだと判断した場合は、まず「そちらの内容でしたら事例をお調べすることができます。検索してもよろしいですか？」とだけ聞いてください。お客様が「はい」等で同意したら、あなたは何も話さずsearch_faq関数を呼び出してください（検索クエリはお客様の質問内容を要約して自分で生成すること）。お客様が「いいえ」等で断った場合は、それ以上検索の提案はせず、通常の用件として扱い次に進んでください。
                    - search_faq関数の結果が返ってきたら、その内容を自然な話し言葉に直してお客様に伝えてください。読み上げ終わったら「以上ですが、他にご質問はございますか？」と確認し、新しい質問があればまた同じ判断（FAQに関連するか）からやり直してください。「いいえ」等であれば通常の受付終了フロー（お名前・折り返し先電話番号の聞き取り）に進んでください。
                    - search_faq関数がエラーを返した場合は、「恐れ入ります、うまく調べられませんでした」と一言お詫びし、通常の用件として扱って次に進んでください。

                    -もし印字のズレ・FAQ検索のいずれでもない問合せだった場合はすぐに簡単に内容を1回だけ復唱して、そのあとに次に進んでください。

                    -「では」と言って「お電話口のかたのお名前」「折り返し先の電話番号」も必ず聞いてください
                    -折り返し先の電話番号は「この番号まで」「今の番号まで」といわれることがありますのでその通りに復唱してください。
                    -お客様の会話は「お名前」から入ったりして順番が入れ替わることがあるので柔軟に聴取してください。
                    - 3点確認できたら「かしこまりました。折り返し手配しますのでお電話を切ってお待ちください。」と言って終了してください。

                    -復唱の途中でお客様が話し始めた場合は、すぐに話すのをやめてお客様の話を聞いてください。
                    -復唱した内容についてお客様から訂正があった場合は、「失礼いたしました」と謝罪し、訂正後の内容を最初から復唱し直してください。

                    -「あなた人間なの？」って聞かれることがあります。「私は対話型のAIです。びっくりさせてすみません。」と言ってください。

                    「すぐに連絡する」や「大至急連絡させます」はNGワードです。
                    もし「折り返し時間」について聞かれたら「恐れ入ります。可能な限り早めに手配しますのでお待ちください。申し訳ございません。」と言ってください。聞かれない場合は話さないでください。

                    -「【無音タイムアウト】」というメッセージが届いたら、他のことは一切言わずに「お声が聞こえませんので失礼いたします。ありがとうございました。」とだけ言ってください。

                    【業界専門用語】以下の単語が出た場合、正しく認識・復唱してください。
                    - 印字調整（いんじちょうせい）
                    - 電子保適（でんしほてき）
                    - e-JIBAI（イージバイ）：電子自賠責のこと
                    - 中古新規（ちゅうこしんき）
                    - OSS（オーエスエス）
                    - 登録不備（とうろくふび）

                    【FAQ検索(search_faq)の対象分野】お客様の質問がsearch_faqで検索可能かどうかは
                    以下の分野に該当するかで判断してください。ここに無い内容や、該当するか判断が
                    難しい内容については検索を提案せず、これまで通り用件として受け付けてください。
                    - TODO: 実際にfaq-search-appに収録されている分野をここに列挙する（例:
                      車両登録の手続き、電子保適の申請方法、OSS利用時のエラー対応 など）"""


def init_db():
    """テーブル作成（初回実行時）"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS call_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            call_id TEXT,
            phone_number TEXT,
            stage TEXT,
            status TEXT,
            error_message TEXT,
            duration_ms INTEGER,
            salesforce_case_id TEXT,
            recording_url TEXT
        )''')
        try:
            c.execute("ALTER TABLE call_events ADD COLUMN recording_url TEXT")
        except Exception:
            pass  # 既存DBへのマイグレーション：列が既にある場合はスキップ
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            call_id TEXT,
            service TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            audio_input_tokens INTEGER DEFAULT 0,
            audio_output_tokens INTEGER DEFAULT 0,
            duration_seconds REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0
        )''')
        conn.commit()
        conn.close()
        print("[OK] Log DB initialized")
    except Exception as e:
        print(f"[NG] DB init error: {e}")


def get_prompt(default: str = DEFAULT_INSTRUCTIONS) -> str:
    """DBに保存されたプロンプトを返す。未設定の場合は default を返す"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='ai_instructions'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def save_prompt(text: str):
    """プロンプトをDBに保存する"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('ai_instructions', ?)",
        (text,)
    )
    conn.commit()
    conn.close()


def log_usage(call_id, service, model, *, input_tokens=0, output_tokens=0,
              audio_input_tokens=0, audio_output_tokens=0, duration_seconds=0, cost_usd=0):
    """API使用量をDBに記録"""
    jst = timezone(timedelta(hours=9))
    timestamp = datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT INTO usage_log
            (timestamp, call_id, service, model, input_tokens, output_tokens,
             audio_input_tokens, audio_output_tokens, duration_seconds, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (timestamp, call_id, service, model, input_tokens, output_tokens,
             audio_input_tokens, audio_output_tokens, duration_seconds, cost_usd))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[NG] Usage log error: {e}")


def get_cost_settings():
    """コスト設定をDBから取得（デフォルト値でフォールバック）"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key LIKE 'cost_%'")
        rows = c.fetchall()
        conn.close()
        settings = dict(COST_SETTINGS_DEFAULTS)
        settings.update({k: v for k, v in rows})
        return settings
    except Exception:
        return dict(COST_SETTINGS_DEFAULTS)


def save_cost_setting(key, value):
    """コスト設定をDBに保存"""
    if not key.startswith('cost_'):
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_openai_cost_data():
    """OpenAIコストのDB集計データを返す"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("""
            SELECT
                strftime('%Y-%m', timestamp) as month,
                service,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(audio_input_tokens) as audio_input_tokens,
                SUM(audio_output_tokens) as audio_output_tokens,
                SUM(duration_seconds) as duration_seconds,
                SUM(cost_usd) as cost_usd
            FROM usage_log
            GROUP BY month, service
            ORDER BY month DESC
        """)
        cols = [d[0] for d in c.description]
        monthly = [dict(zip(cols, row)) for row in c.fetchall()]

        jst = timezone(timedelta(hours=9))
        current_month = datetime.now(jst).strftime('%Y-%m')
        c.execute("""
            SELECT
                strftime('%Y-%m-%d', timestamp) as day,
                service,
                SUM(cost_usd) as cost_usd
            FROM usage_log
            WHERE strftime('%Y-%m', timestamp) = ?
            GROUP BY day, service
            ORDER BY day
        """, (current_month,))
        cols = [d[0] for d in c.description]
        daily = [dict(zip(cols, row)) for row in c.fetchall()]

        conn.close()
        return {'monthly': monthly, 'daily': daily}
    except Exception as e:
        print(f"[NG] OpenAI cost data error: {e}")
        return {'monthly': [], 'daily': []}


def log_event(call_id, phone, stage, status, error_msg="", duration_ms=0, case_id=""):
    """ログをDBに記録 + エラーなら Google Chat 通知"""
    jst = timezone(timedelta(hours=9))
    timestamp = datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S')

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT INTO call_events
            (timestamp, call_id, phone_number, stage, status, error_message, duration_ms, salesforce_case_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (timestamp, call_id, phone, stage, status, error_msg, duration_ms, case_id))
        conn.commit()
        conn.close()
        print(f"[LOG] {stage}={status}")

        # エラーなら Google Chat 通知
        if status == 'FAILURE':
            notify_google_chat(call_id, phone, stage, error_msg, timestamp)
    except Exception as e:
        print(f"[NG] Log record error: {e}")


def notify_google_chat(call_id, phone, stage, error_msg, timestamp):
    """Google Chat に通知"""
    if not GOOGLE_CHAT_WEBHOOK_URL:
        print("[WARN] Google Chat Webhook URL not set (skip notification)")
        return

    stage_ja = {
        'to_arrived': 'Twilio着信',
        'ws_connected': 'OpenAI Realtime API connection',
        'case_created': 'Salesforce Case作成',
        'response_watchdog_escalation': '応答ウォッチドッグ縮退運転',
        'media_starvation': 'メディア入力途絶（Twilio→サーバー）',
    }

    text = f"""警告:ケース漏れの可能性があります！
発生時刻: {timestamp}
エラー内容: {stage_ja.get(stage, stage)}
電話番号: {phone}
Call ID: {call_id}
詳細: {error_msg}

URL:{RAILWAY_DASHBOARD_URL}"""

    try:
        requests.post(
            GOOGLE_CHAT_WEBHOOK_URL,
            json={"text": text},
            timeout=5
        )
        print("[OK] Google Chat notification sent")
    except Exception as e:
        print(f"[NG] Google Chat notification error: {e}")


def update_recording_url(call_id: str, recording_url: str):
    """録音URLをDBに保存"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "UPDATE call_events SET recording_url = ? WHERE call_id = ? AND stage = 'case_created' AND status = 'SUCCESS'",
            (recording_url, call_id)
        )
        conn.commit()
        conn.close()
        print(f"[OK] 録音URL更新: {call_id}")
    except Exception as e:
        print(f"[NG] 録音URL更新エラー: {e}")


def cleanup_old_logs():
    """7日以上前のログを削除"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        jst = timezone(timedelta(hours=9))
        seven_days_ago = (datetime.now(jst) - timedelta(days=7)).strftime('%Y-%m-%d')
        c.execute("DELETE FROM call_events WHERE timestamp < ?", (f"{seven_days_ago} 00:00:00",))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            print(f"[CLEANUP] {deleted} old logs deleted")
    except Exception as e:
        print(f"[NG] Log cleanup error: {e}")
