"""
Salesforceケース作成 + 録音/Whisperパイプライン。

既存プロジェクト main.py の104-294行目（search_account_by_phone /
attach_recording_to_case / create_salesforce_case）をほぼそのまま移植。

- extract_phone_from_sip_headers は移植しない。発信元番号は
  CallSession.caller_number（Twilio Media Streamsの start.customParameters
  から取得）を呼び出し側が渡す。
- create_salesforce_case に escalation フラグを追加。応答ウォッチドッグの
  縮退運転パスから呼ばれた場合、Subjectに【AI応対不良・要確認】を付与する。
- この関数群は同期（blocking）実装のまま。呼び出し側（call_session.py）が
  `await asyncio.to_thread(create_salesforce_case, ...)` でイベントループを
  塞がないようにする。内部の録音待ちポーリング（最大120秒）は、その
  to_thread実行スレッドの中からさらに threading.Thread で切り離す
  （このスレッドは既にイベントループ外にあるため、asyncio.to_threadではなく
  threading.Threadが素直で安全）。
"""
import io
import json
import logging
import re
import threading
import time as time_module
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI
from simple_salesforce import Salesforce

import config
from call_logger import get_cost_settings, log_event, log_usage, update_recording_url

logger = logging.getLogger("salesforce_case")

client = OpenAI(api_key=config.OPENAI_API_KEY)


def _sf_connect() -> Salesforce:
    return Salesforce(
        username=config.SF_USERNAME,
        password=config.SF_PASSWORD,
        security_token=config.SF_TOKEN,
        domain=config.SF_DOMAIN,
    )


def normalize_phone(e164: str) -> str:
    """E.164形式（+81始まり、Twilioから渡される発信元番号の形式）を
    国内形式（0始まり）に変換する。例: '+819092415397' -> '09092415397'

    SOSL検索は`+`を不正文字として扱いリクエスト自体が失敗するため、
    Salesforce検索・ケース保存に渡す前に必ずこれを通す。
    """
    if not e164:
        return e164
    num = e164.strip()
    if num.startswith('+81'):
        num = '0' + num[3:]
    return re.sub(r'\D', '', num)


def search_account_by_phone(sf_instance, phone):
    search_query = (
        f"FIND {{{phone}}} IN PHONE FIELDS "
        f"RETURNING Account(Id, Name, Phone)"
    )
    result = sf_instance.search(search_query)
    records = result.get('searchRecords', []) if isinstance(result, dict) else result
    return records[0] if records else None


def attach_recording_to_case(case_id: str, call_id: str = ""):
    """通話終了後、Twilio録音が完了するまでポーリングしてWhisperで文字起こしし、ケースを更新する"""
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        logger.warning("Twilio認証情報が未設定のため録音URLをスキップします")
        return
    try:
        from twilio.rest import Client as TwilioClient
        twilio = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

        POLL_INTERVAL = 5
        MAX_WAIT = 120
        completed = []
        waited = 0
        time_module.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        while waited <= MAX_WAIT:
            if call_id:
                # CallSidで直接引く。時刻ベースの一覧取得だと通話が近接した
                # 場合に別通話の録音を拾いうる（実発生あり）ため、構造的に
                # 混入しない形にする。
                recordings = twilio.calls(call_id).recordings.list(limit=5)
            else:
                # フォールバック（原則到達しない）: 従来の時刻ベース
                since = datetime.now(timezone.utc) - timedelta(minutes=30)
                recordings = twilio.recordings.list(date_created_after=since, limit=5)
            completed = [r for r in recordings if r.status == "completed"]
            if completed:
                logger.info("録音完了を検出 (%s秒後)", waited)
                break
            logger.info("録音未完了 (%s秒経過)...", waited)
            time_module.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

        if not completed:
            logger.warning("完了済みのTwilio録音が見つかりませんでした")
            return
        rec = completed[0]
        proxy_url = (
            f"https://{config.RAILWAY_PUBLIC_DOMAIN}"
            f"/recording/{rec.sid}?token={config.RECORDING_ACCESS_TOKEN}"
        )

        remarks = f"【録音URL】\n{proxy_url}"
        try:
            audio_url = (
                f"https://api.twilio.com/2010-04-01/Accounts"
                f"/{config.TWILIO_ACCOUNT_SID}/Recordings/{rec.sid}.mp3"
            )
            audio_resp = requests.get(
                audio_url,
                auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
                timeout=60
            )
            if audio_resp.status_code == 200:
                audio_buf = io.BytesIO(audio_resp.content)
                audio_buf.name = "recording.mp3"
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_buf,
                    language="ja"
                )
                whisper_text = result.text.strip()
                logger.info("Whisper文字起こし完了: %s", whisper_text[:60])
                remarks = f"【通話全文（音声認識）】\n{whisper_text}\n\n【録音URL】\n{proxy_url}"
                try:
                    duration_sec = float(rec.duration or 0)
                    settings = get_cost_settings()
                    cost = duration_sec / 60 * float(settings.get('cost_openai_whisper_per_min', '0.006'))
                    log_usage(call_id, 'openai_whisper', 'whisper-1',
                              duration_seconds=duration_sec, cost_usd=cost)
                except Exception as ue:
                    logger.warning("Whisper usage log error: %s", ue)
            else:
                logger.error("録音ダウンロード失敗: HTTP %s", audio_resp.status_code)
        except Exception as e:
            logger.error("Whisperの文字起こしエラー: %s", e)

        sf = _sf_connect()
        sf.Case.update(case_id, {"SC_CorrespondenceRemarks__c": remarks}, headers={'Sforce-Auto-Assign': 'FALSE'})
        logger.info("対応備考更新完了: ケース %s", case_id)
        if call_id:
            update_recording_url(call_id, proxy_url)
    except Exception as e:
        logger.error("録音取得エラー: %s", e)


def create_salesforce_case(transcript_lines: list, call_id: str = "", phone_number: str = "",
                            escalation: bool = False):
    full_transcript = "\n".join(transcript_lines) if transcript_lines else "(会話記録なし)"

    try:
        summary_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "以下の通話内容を分析し、JSON形式で返してください。\n"
                    "{\n"
                    '  "subject": "問い合わせ内容を10文字以内で表す件名（例：印字調整、登録不備）",\n'
                    '  "contact_person": "お客様のお名前（カタカナ表記で末尾に様を付ける、例：ヒロセ様、不明な場合は空文字）",\n'
                    '  "destination_phone": "折り返し電話番号（ハイフンなしの数字のみ、不明な場合は空文字）"\n'
                    "}"
                )},
                {"role": "user", "content": full_transcript}
            ],
            response_format={"type": "json_object"}
        )
        extracted = json.loads(summary_response.choices[0].message.content)
        subject = extracted.get("subject", "通話ケース")
        contact_person = extracted.get("contact_person", "")
        destination_phone = extracted.get("destination_phone", "") or phone_number
        try:
            u = summary_response.usage
            if u:
                settings = get_cost_settings()
                cost = (
                    (u.prompt_tokens or 0) / 1_000_000 * float(settings.get('cost_openai_gpt4omini_in_per_1m', '0.15')) +
                    (u.completion_tokens or 0) / 1_000_000 * float(settings.get('cost_openai_gpt4omini_out_per_1m', '0.60'))
                )
                log_usage(call_id, 'openai_gpt4omini', 'gpt-4o-mini',
                          input_tokens=u.prompt_tokens or 0,
                          output_tokens=u.completion_tokens or 0,
                          cost_usd=cost)
        except Exception as ue:
            logger.warning("GPT-4o-mini usage log error: %s", ue)
    except Exception as e:
        logger.error("要約生成エラー: %s", e)
        subject = "通話ケース"
        contact_person = ""
        destination_phone = phone_number

    # SOSL検索・ケース保存の両方でこの正規化後の値を使う
    # （+81始まりのE.164形式のままだとSOSLが不正文字としてリクエストごと失敗する）
    destination_phone = normalize_phone(destination_phone)

    try:
        sf = _sf_connect()

        matched_account = None
        if destination_phone:
            logger.info("電話番号 '%s' で取引先を検索中...", destination_phone)
            matched_account = search_account_by_phone(sf, destination_phone)
            if matched_account:
                logger.info("取引先を検出: %s", matched_account['Name'])
            else:
                logger.warning("該当する取引先は見つかりませんでした")

        subject_prefix = "【AI応対不良・要確認】" if escalation else "【AI受付】"
        case_data = {
            'OwnerId': config.SF_CASE_OWNER_ID,
            'RecordTypeId': config.SF_CASE_RECORD_TYPE_ID,
            'Status': 'New',
            'Origin': 'Phone',
            'Subject': f"{subject_prefix}{subject}の件について",
            'SC_ContactPerson__c': contact_person,
            'SC_DestinationPhoneNo__c': destination_phone,
        }
        if matched_account:
            case_data['AccountId'] = matched_account['Id']

        result = sf.Case.create(case_data, headers={'Sforce-Auto-Assign': 'FALSE'})
        case_id = result['id']
        logger.info("Salesforceケース作成成功: %s", case_id)
        log_event(call_id, phone_number, 'case_created', 'SUCCESS', case_id=case_id)
        threading.Thread(
            target=lambda cid=call_id, csid=case_id: attach_recording_to_case(csid, cid),
            daemon=True,
        ).start()
        return case_id

    except Exception as e:
        logger.error("Salesforceケース作成エラー: %s", e)
        log_event(call_id, phone_number, 'case_created', 'FAILURE', str(e))
        return None
