"""管理ダッシュボード（既存プロジェクト realtime-sip-server の admin.py を移植）。

移植元はFlask Blueprintだったため、ルーティングとレスポンスの書き方のみ
FastAPI流に置き換えている。SQL・コスト集計ロジック・認証の範囲は移植元と
同一で、テンプレート(templates/admin_dashboard.html)は無改変で流用する
（Jinja構文を含まない純粋な静的HTMLで、データはすべてfetchでJSONを取りに
来る作りのため）。

重要: 各エンドポイントは意図的に `async def` ではなく `def` で定義している。
本サービスは同一プロセスで通話中のMedia Stream(WebSocket)を捌いており、
SQLiteアクセスやTwilio Usage API呼び出し（最大10秒×6回）をイベントループ上で
直接実行すると、その間すべての通話の音声中継が停止する。`def` で定義すると
FastAPIが自動的に別スレッドプールで実行するため、イベントループを止めない。
"""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests as http_requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

import config
from call_logger import (
    COST_SETTINGS_DEFAULTS,
    DB_PATH,
    get_cost_settings,
    get_openai_cost_data,
    get_prompt,
    save_cost_setting,
    save_prompt,
)

ADMIN_PASSWORD = config.ADMIN_PASSWORD
TWILIO_ACCOUNT_SID = config.TWILIO_ACCOUNT_SID or ""
TWILIO_AUTH_TOKEN = config.TWILIO_AUTH_TOKEN or ""

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_DASHBOARD_HTML = os.path.join(_TEMPLATE_DIR, "admin_dashboard.html")

_cost_cache = {'data': None, 'ts': 0}
COST_CACHE_TTL = 900  # 15分

router = APIRouter(prefix="/admin")


def _check_auth(request: Request) -> bool:
    password = request.headers.get('X-Admin-Password', '')
    return bool(ADMIN_PASSWORD) and password == ADMIN_PASSWORD


def require_auth(request: Request):
    """移植元の `@admin_bp.before_request` 相当。

    移植元は `request.path == '/admin/api/prompt'` のときだけ認証を要求して
    いた（ダッシュボード本体・通話ログ・エラー一覧・コスト参照は無認証）。
    その範囲をそのまま維持するため、このDependsはプロンプトAPIにのみ付ける。
    """
    if not _check_auth(request):
        # 移植元と同じくプレーンテキスト本文+401。画面側は res.status のみを
        # 見て本文を読まないため、本文の形式は揃える必要はないが踏襲する。
        raise _UnauthorizedError()


class _UnauthorizedError(Exception):
    """認証NGを401に変換するためのマーカー例外（main.pyでハンドラ登録）。"""


def get_jst_today():
    """Get today's date in JST"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime('%Y-%m-%d')


@router.get('/dashboard')
def dashboard():
    """Dashboard screen"""
    return FileResponse(_DASHBOARD_HTML, media_type='text/html')


@router.get('/api/status')
def get_status():
    """Get statistics (JSON)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        today = get_jst_today()

        # Cases created today
        c.execute(
            "SELECT COUNT(*) as cnt FROM call_events WHERE stage='case_created' AND status='SUCCESS' AND timestamp LIKE ?",
            (f"{today}%",)
        )
        cases_created = c.fetchone()['cnt']

        # Failed cases today
        c.execute(
            "SELECT COUNT(*) as cnt FROM call_events WHERE status='FAILURE' AND timestamp LIKE ?",
            (f"{today}%",)
        )
        failures = c.fetchone()['cnt']

        # Active calls: arrived in last 30 minutes AND not yet terminated
        c.execute("""
            SELECT COUNT(DISTINCT call_id) as cnt FROM call_events
            WHERE stage='to_arrived' AND status='SUCCESS'
            AND datetime(timestamp) > datetime('now', '-30 minutes', '+9 hours')
            AND call_id NOT IN (
                SELECT DISTINCT call_id FROM call_events
                WHERE status='FAILURE' OR stage='case_created'
            )
        """)
        active_calls = c.fetchone()['cnt']

        conn.close()

        return {
            'active_calls': active_calls,
            'cases_created_today': cases_created,
            'failures_today': failures
        }
    except Exception as e:
        print(f"[NG] Status fetch error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/logs')
def get_logs():
    """Get log list (JSON)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Get latest logs
        c.execute(
            "SELECT * FROM call_events ORDER BY timestamp DESC LIMIT 100"
        )
        logs = [dict(row) for row in c.fetchall()]
        conn.close()

        return logs
    except Exception as e:
        print(f"[NG] Log fetch error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/failures')
def get_failures():
    """Get case leak details"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute(
            "SELECT * FROM call_events WHERE status='FAILURE' ORDER BY timestamp DESC LIMIT 50"
        )
        failures = [dict(row) for row in c.fetchall()]
        conn.close()

        return failures
    except Exception as e:
        print(f"[NG] Case leak fetch error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/prompt', dependencies=[Depends(require_auth)])
def get_prompt_api():
    """現在のプロンプトを返す"""
    return {'instructions': get_prompt()}


@router.post('/api/prompt', dependencies=[Depends(require_auth)])
async def update_prompt(request: Request):
    """プロンプトを保存する

    リクエストボディの読み取りは非同期でしか行えないため、このエンドポイント
    のみ `async def` とする。save_prompt自体はSQLiteへの短い書き込み1回で、
    イベントループを実質的に止めない。
    """
    try:
        data = await request.json()
    except Exception:
        data = None
    if not data or 'instructions' not in data:
        return JSONResponse({'error': 'instructions が必要です'}, status_code=400)
    save_prompt(data['instructions'])
    return {'ok': True}


def _fetch_live_jpy_rate(fallback: float) -> tuple[float, bool]:
    """open.er-api.com からUSD/JPYレートを取得。失敗時はfallbackを返す"""
    try:
        resp = http_requests.get(
            'https://open.er-api.com/v6/latest/USD',
            timeout=5
        )
        if resp.ok:
            rate = resp.json().get('rates', {}).get('JPY')
            if rate:
                return float(rate), True
    except Exception:
        pass
    return fallback, False


def _fetch_twilio_monthly(jpy_rate: float):
    """Twilio Usage API から月別コストを取得（内部はUSD換算で統一）"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return []
    monthly = {}
    for category in ['calls-inbound', 'calls-sip-inbound', 'recordings']:
        try:
            resp = http_requests.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Usage/Records/Monthly.json",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                params={'Category': category},
                timeout=10
            )
            if not resp.ok:
                continue
            for r in resp.json().get('usage_records', []):
                month = r.get('start_date', '')[:7]
                if not month:
                    continue
                if month not in monthly:
                    monthly[month] = {'month': month, 'calls_usd': 0.0, 'recordings_usd': 0.0,
                                      'calls_count': 0, 'calls_minutes': 0.0}
                raw_price = abs(float(r.get('price') or '0'))
                price_unit = (r.get('price_unit') or 'USD').upper()
                # TwilioアカウントがJPY建ての場合はUSDに換算する
                price_usd = raw_price / jpy_rate if price_unit == 'JPY' else raw_price
                count = int(r.get('count') or '0')
                usage = float(r.get('usage') or '0')
                if 'calls' in category:
                    monthly[month]['calls_usd'] += price_usd
                    monthly[month]['calls_count'] += count
                    monthly[month]['calls_minutes'] += usage
                else:
                    monthly[month]['recordings_usd'] += price_usd
        except Exception as e:
            print(f"[WARN] Twilio Usage API error ({category}): {e}")
    return sorted(monthly.values(), key=lambda x: x['month'], reverse=True)


def _fetch_twilio_daily(jpy_rate: float, current_month: str):
    """Twilio Usage API から今月の日別コストを取得"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return []
    jst = timezone(timedelta(hours=9))
    start_date = current_month + '-01'
    end_date = datetime.now(jst).strftime('%Y-%m-%d')
    daily = {}
    for category in ['calls-inbound', 'calls-sip-inbound', 'recordings']:
        try:
            resp = http_requests.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Usage/Records/Daily.json",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                params={'Category': category, 'StartDate': start_date, 'EndDate': end_date},
                timeout=10
            )
            if not resp.ok:
                continue
            for r in resp.json().get('usage_records', []):
                day = r.get('start_date', '')
                if not day:
                    continue
                if day not in daily:
                    daily[day] = {'day': day, 'calls_usd': 0.0, 'recordings_usd': 0.0}
                raw_price = abs(float(r.get('price') or '0'))
                price_unit = (r.get('price_unit') or 'USD').upper()
                price_usd = raw_price / jpy_rate if price_unit == 'JPY' else raw_price
                if 'calls' in category:
                    daily[day]['calls_usd'] += price_usd
                else:
                    daily[day]['recordings_usd'] += price_usd
        except Exception as e:
            print(f"[WARN] Twilio Daily API error ({category}): {e}")
    return sorted(daily.values(), key=lambda x: x['day'], reverse=True)


@router.get('/api/costs')
def get_costs(refresh: str = ''):
    """コスト集計を返す（15分キャッシュ）"""
    global _cost_cache
    force = refresh == '1'
    now = time.time()
    if not force and _cost_cache['data'] and (now - _cost_cache['ts']) < COST_CACHE_TTL:
        return _cost_cache['data']
    try:
        settings = get_cost_settings()
        fallback_rate = float(settings.get('cost_usd_jpy_rate', '155'))
        jpy_rate, rate_is_live = _fetch_live_jpy_rate(fallback_rate)
        jst = timezone(timedelta(hours=9))
        current_month = datetime.now(jst).strftime('%Y-%m')

        openai_data = get_openai_cost_data()
        twilio_monthly = _fetch_twilio_monthly(jpy_rate)
        twilio_daily = _fetch_twilio_daily(jpy_rate, current_month)

        fixed_usd = (
            float(settings.get('cost_twilio_phone_monthly_usd', '1.15')) +
            float(settings.get('cost_railway_monthly_usd', '5.00'))
        )

        result = {
            'openai': openai_data,
            'twilio_monthly': twilio_monthly,
            'fixed': {
                'twilio_phone_monthly_usd': float(settings.get('cost_twilio_phone_monthly_usd', '1.15')),
                'railway_monthly_usd': float(settings.get('cost_railway_monthly_usd', '5.00')),
                'total_usd': fixed_usd,
            },
            'twilio_daily': twilio_daily,
            'jpy_rate': jpy_rate,
            'rate_is_live': rate_is_live,
            'current_month': current_month,
        }
        _cost_cache = {'data': result, 'ts': now}
        return result
    except Exception as e:
        print(f"[NG] Cost fetch error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/costs/settings')
def get_cost_settings_api():
    """コスト設定を返す"""
    return get_cost_settings()


@router.post('/api/costs/settings')
async def update_cost_settings(request: Request):
    """コスト設定を保存（管理者パスワード必要）"""
    if not _check_auth(request):
        return PlainTextResponse("Authentication required", status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    for key, value in (data or {}).items():
        if key in COST_SETTINGS_DEFAULTS:
            save_cost_setting(key, value)
    _cost_cache['ts'] = 0  # キャッシュ無効化
    return {'ok': True}
