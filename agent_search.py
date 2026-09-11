"""
AgentSearch（Vertex AI Search / Discovery Engine）連携。

agentsearch-integration-phase1.md のFAQ検索機能を実現するfunction calling定義と
実処理をまとめる。Realtime API側の分岐（FAQらしい質問だけこの関数を呼ぶ）は
openai_client.py のtools登録とinstructionsの記述に委ねており、本ファイルは
「呼ばれた後どう検索してどう返すか」だけを担当する。

認証はgoogle-authのADC(Application Default Credentials)任せにする。Railwayには
ローカルの秘密ファイルを直接置けないため、サービスアカウントキーJSONの中身を
環境変数 GOOGLE_APPLICATION_CREDENTIALS_JSON にそのまま渡し、起動時（本モジュール
import時）にファイル化してGOOGLE_APPLICATION_CREDENTIALSへ差し替える運用にしている
（ローカル開発ではGOOGLE_APPLICATION_CREDENTIALSに直接キーファイルのパスを設定すれば
この処理はスキップされる）。

注意: Discovery EngineのREST APIエンドポイント形状（answerメソッドのURL・リクエスト
/レスポンスの厳密なフィールド名）は実機未検証。Vertex AI Search側で作成した
serving configの実際のIDに合わせてAGENTSEARCH_ENGINE_ID等を設定した上で、
最初の呼び出しはログ（logger.info の生レスポンス）で構造を確認しながら検証すること
（openai_client.pyのaudio/pcmu仮説と同様の「実機で確認するまでは仮」の位置づけ）。
"""
import asyncio
import json
import logging
import tempfile
import time

import requests

import config

logger = logging.getLogger("agent_search")

DISCOVERY_ENGINE_API_BASE = "https://discoveryengine.googleapis.com/v1"
_OAUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _materialize_credentials_json():
    """GOOGLE_APPLICATION_CREDENTIALS_JSON（キーJSON本文）が設定されている場合、
    一時ファイルに書き出してGOOGLE_APPLICATION_CREDENTIALSへ差し替える。
    Railwayの環境変数にはファイルを置けないための回避策（CALL_LOG_DB_PATHの
    ようなボリュームパスではなく、プロセス起動時に都度/tmpへ書く方式でよい。
    キー自体は再生成可能な機密情報であり永続化の必要がないため）。"""
    import os

    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    if not raw:
        return
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return  # ローカル開発等で明示指定済みなら優先する
    fd, path = tempfile.mkstemp(prefix="gcp-sa-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    logger.info("[AGENTSEARCH] GOOGLE_APPLICATION_CREDENTIALS_JSON を一時ファイル化しました")


_materialize_credentials_json()


# search_faqのfunction calling定義（GA版Realtime API session.update用）。
# 呼び出し条件（FAQらしい質問のときだけ呼ぶ／それ以外は今まで通り受付する）は
# descriptionに明記し、AI自身の自然言語判断に委ねる。play_fax_number（openai_client.py）
# と同じ「instructionsのテキストだけで分岐させる」設計を踏襲している。
SEARCH_FAQ_TOOL = {
    "type": "function",
    "name": "search_faq",
    "description": (
        "お客様の質問がRAGで収録されている内容に関連していると判断できる"
        "場合にのみ呼び出す。検索用に質問内容を要約したクエリを自分で生成"
        "して渡すこと。FAQでこたえられるか判断がつかない内容ではこの関数"
        "を呼ばず、これまで通り要件を聞き取って折り返しにすること。なお、"
        "この関数はお客様が検索の実施に同意した場合にのみ呼び出す（同意"
        "確認の会話フローはinstructions側の指示に従うこと）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "お客様の質問内容を検索しやすい形に要約したクエリ文字列",
            }
        },
        "required": ["query"],
    },
}


class AgentSearchError(Exception):
    """AgentSearch呼び出し失敗時（タイムアウト・認証エラー・APIエラー等）。
    call_session.py側でこれを捕捉し、モデルには検索失敗を伝えて通常の
    受付フローへ促す想定（instructions側に失敗時の振る舞いを別途明記する）。"""


_credentials = None


def _get_access_token() -> str:
    """ADCからアクセストークンを取得する。google.auth.default()は初回に
    重い処理を伴うためモジュールグローバルにキャッシュし、以降は有効期限
    切れの場合のみrefreshする。"""
    global _credentials
    import google.auth
    import google.auth.transport.requests

    if _credentials is None:
        _credentials, _ = google.auth.default(scopes=_OAUTH_SCOPES)

    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())

    return _credentials.token


def _search_faq_sync(query: str) -> str:
    """Discovery Engine（Vertex AI Search）のanswerメソッドを同期呼び出しする。
    asyncio.to_threadでオフロードして呼ばれる前提（既存のsalesforce_case呼び出し
    と同じパターン。call_session.py参照）。"""
    if not config.AGENTSEARCH_PROJECT_ID or not config.AGENTSEARCH_ENGINE_ID:
        raise AgentSearchError("AGENTSEARCH_PROJECT_ID / AGENTSEARCH_ENGINE_ID が未設定です")

    token = _get_access_token()
    url = (
        f"{DISCOVERY_ENGINE_API_BASE}/projects/{config.AGENTSEARCH_PROJECT_ID}"
        f"/locations/{config.AGENTSEARCH_LOCATION}/collections/default_collection"
        f"/engines/{config.AGENTSEARCH_ENGINE_ID}"
        f"/servingConfigs/default_search:answer"
    )
    payload = {
        "query": {"text": query},
        "answerGenerationSpec": {"ignoreAdversarialQuery": True},
    }

    started = time.monotonic()
    logger.info("[AGENTSEARCH] query=%r 呼び出し開始 timeout_sec=%s", query, config.AGENTSEARCH_TIMEOUT_SEC)
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.AGENTSEARCH_TIMEOUT_SEC,
        )
    except requests.Timeout as e:
        raise AgentSearchError(f"AgentSearchタイムアウト（{config.AGENTSEARCH_TIMEOUT_SEC}秒）") from e
    except requests.RequestException as e:
        raise AgentSearchError(f"AgentSearchリクエスト失敗: {e}") from e

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("[AGENTSEARCH] query=%r status=%s elapsed_ms=%d", query, resp.status_code, elapsed_ms)

    if resp.status_code != 200:
        raise AgentSearchError(f"AgentSearch APIエラー status={resp.status_code} body={resp.text[:500]}")

    data = resp.json()
    logger.debug("[AGENTSEARCH] raw response=%s", json.dumps(data, ensure_ascii=False)[:2000])

    answer_text = (data.get("answer") or {}).get("answerText")
    if not answer_text:
        raise AgentSearchError("AgentSearchのレスポンスにanswerTextが含まれていません")

    return answer_text


async def search_faq(query: str) -> str:
    """search_faq function callingのハンドラ本体。call_session.pyの
    _handle_function_callから呼ばれる想定。

    戻り値はfunction_call_output（モデルへ返す文字列）にそのまま使えるよう
    JSON文字列で返す。成功時は{"answer": "..."}、失敗時は{"error": "..."}とし、
    どちらの場合の振る舞い（読み上げ変換して続けるか、検索失敗を詫びて通常の
    受付フローに戻るか）もinstructions側の記述に委ねる。"""
    try:
        answer_text = await asyncio.to_thread(_search_faq_sync, query)
        return json.dumps({"answer": answer_text}, ensure_ascii=False)
    except AgentSearchError as e:
        logger.warning("[AGENTSEARCH] 検索失敗: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
