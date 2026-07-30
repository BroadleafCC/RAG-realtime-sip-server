# AI電話受付 v2（VAD自前化）

OpenAI Realtime APIのサーバー側VADが `speech_started` は発火するのに
`speech_stopped` が届かずスタックする問題への対策として、発話区切り判定を
自前のSilero VADで行う新系統。詳細な設計意図は `C:\Users\31001\.claude\plans\distributed-conjuring-eich.md` を参照。

既存プロジェクト（`realtime-sip-server`、Twilio SIPトランク + OpenAI Call API）
とは別の、新規Railwayサービス・新規Twilio電話番号で動かす。既存システムには
一切変更を加えていない。

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env
# .env を編集して OPENAI_API_KEY 等を設定
```

ローカルWindows開発では `CALL_LOG_DB_PATH=./call_log.db` を設定すること
（本番の `/data/call_log.db` はRailwayボリューム前提のパス）。

## 単体テスト（音声・ネットワーク不要）

```bash
python tests/test_vad.py
python tests/test_audio_convert.py
```

## ローカル起動 + ngrok動作確認

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
ngrok http 8000
```

ngrokが払い出したURL（`https://xxxx.ngrok-free.app`）をTwilioのテスト用電話番号の
Voice webhookに `https://xxxx.ngrok-free.app/voice` として設定する。

## デプロイ（Railway）

1. Railwayで新規サービスを作成（既存サービスとは別。既存には触れない）
2. `.env.example` の内容を環境変数として設定（`OPENAI_API_KEY` は本プロジェクト
   専用のキーを使うこと。既存システムとキーを共有しない＝コスト分離）
3. ログDB用のボリュームを `/data` にマウント
4. デプロイ後、新規Twilio電話番号のVoice webhookをRailwayの公開ドメイン
   （`https://{RAILWAY_PUBLIC_DOMAIN}/voice`）に向ける

## ディレクトリ構成

| ファイル | 役割 |
|---|---|
| `main.py` | FastAPIアプリ本体（`/voice`, `/media-stream`, `/recording/{sid}`, `/health`） |
| `config.py` | 環境変数・チューニング値の一元管理 |
| `call_session.py` | 1通話ぶんのオーケストレーター（5タスク並行実行） |
| `vad.py` | 発話区切り判定の状態機械（純粋・同期・単体テスト可） |
| `vad_model.py` | Silero VAD (ONNX, torch非依存) ラッパー |
| `audio_convert.py` | mu-law→PCM16変換（VAD解析用サイドタップ専用） |
| `openai_client.py` | OpenAI Realtime WebSocket接続ラッパー |
| `twilio_client.py` | Twilio REST操作・Media Streams送信フレーム構築 |
| `call_logger.py` | ログDB・Google Chat通知・コスト集計（既存プロジェクトから移植） |
| `salesforce_case.py` | Salesforceケース作成・録音/Whisperパイプライン（既存から移植） |
| `watchdogs.py` | 無音／最大通話時間／応答ウォッチドッグの3独立監視ループ |

## 既知の未検証事項（実機での初回テストで確認すること）

- `openai_client.py` の `session.update` に指定している音声フォーマット
  `{"type": "audio/pcmu"}` はGA版APIドキュメント調査に基づく仮説。
  `session.updated` イベントのログ（`[SESSION ECHO]`）で実際に通っているか
  確認すること
- `response.output_audio.delta` というイベント名も同様に未検証（このトランス
  ポートでの音声中継は既存プロジェクトに前例がなく完全新規実装のため）
