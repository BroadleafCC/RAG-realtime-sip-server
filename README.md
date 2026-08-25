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
python tests/test_twilio_framing.py
python tests/test_call_session_prewarm.py
python tests/test_barge_in.py
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
| `assets/audio/` | 挨拶・即時相槌・縮退運転案内の事前生成済み音声アセット |
| `scripts/generate_static_clips.py` | 上記音声アセットの生成/再生成スクリプト（OpenAI TTS使用） |

## 応答速度・音声頭切れチューニング

- `VAD_SPEECH_END_MS`（既定500ms）: 発話終了とみなすまでの無音継続時間。短くする
  ほど応答は速くなるが、語尾の「間」を誤って区切るリスクが上がる。
- `AUDIO_PRIMING_MS`（既定250ms、旧名`RESPONSE_LEAD_SILENCE_MS`）: 各応答の音声
  冒頭に挿入する無音（プライミング）の長さ。出力ストリームが立ち上がる瞬間の
  頭切れ対策。0で無効化可能。頭切れ検証中は段階的に下げて`[HEAD-CLIP?]`ログが
  出ないことを確認すること（出たら1段階戻す）。
- `PRIMING_IDLE_THRESHOLD_MS`（既定1000ms）: 直前のTwilio送信からこの時間以内に
  始まる本応答はプライミングを省略する（`ENABLE_FILLER=true`で相槌がストリームを
  温めている場合等）。
- `ENABLE_FILLER` / `FILLER_AUDIO_PATH`: trueにすると、発話終了直後（応答生成の
  待ち時間）に短い相槌音声を即時再生し、無音区間を埋める（オプション機能。
  デフォルトはfalse）。フレーズや声を変えたい場合は
  `scripts/generate_static_clips.py` を編集して再実行する。
- `[AUDIO-STATS]` ログ: 応答ごとの音声中継バイト数・再生時間を1行で出力する
  診断ログ。`bytes_in`と`bytes_out`が一致しない場合は自サーバー内での音声欠落、
  `playback_sec`が`expected_sec`から大きく乖離する場合はTwilio側での遅延・欠落を
  示す。mark名とAudioStatsの対応はmark送信（フレーム送信完了）時点で
  `CallSession._audio_stats_by_mark` にスナップショットとして確定し、mark受信時に
  それを引く（`session._audio_stats` を直接見ると、次の応答が既に始まっている
  場合に別応答のresp_idを記録してしまうため）。

## バージイン（割り込み）

AI発話中にお客様が500ms以上継続して話すと、以下を指示書どおりの順序で行う
（`call_session._handle_barge_in`）:

1. `response.cancel`（当該応答がまだ生成中＝`response.done`未受信の場合のみ）
2. Twilioへ`clear`を送り再生キューを破棄
3. `ai_is_speaking`を即時Falseにし、以後届く当該再生ブロックのmarkを無効化
4. `conversation.item.truncate`でモデルの会話履歴を実際に聞こえたところ
   （`audio_end_ms`）まで切り詰める。この値は「音声送信開始からの経過時間」と
   「実際に送信したバイト数から計算した秒数」の小さい方を採用する近似値。
   Twilio側の実再生はサーバー送信より遅延するため実際より多めに見積もる
   ことになるが、切り詰めすぎて既に聞こえた内容を消すより安全側なので
   この近似でよい

挨拶・縮退運転案内クリップの再生中（`response.output_item.added`を経由しない
＝item_idを持たない）バージインでは、cancel/truncateは行わずclearのみ行う。

`ai_is_speaking`は`output_audio_buffer.started`イベントに加え、
`response.output_audio.delta`で実際に音声送信を開始した瞬間にも設定する
（前者のイベントだけに頼るとバージイン判定が効かないケースが実測されたため
の二重化）。

ログ: `[BARGE-IN] 発動 speech_ms=XXX 対象=mark名 truncate audio_end_ms=YYY`
（クリップ再生中は`(クリップ再生のためtruncate不要)`）、500ms未満で終わった
短い音は`[BARGE-IN] 抑止 speech_ms=XXX (<500ms)`。

## 受話直後の無音解消・挨拶の即時再生

挨拶（「お待たせしました。ご用件を伺います。」）は毎回同じ定型文のため、
モデルに生成させず事前生成クリップとして即時再生する。OpenAI WebSocket接続も
Media Streamの`start`受信を待たず `/voice` webhook受信時点で前倒しして裏で
開始しておく（`call_session.prewarm_openai_connection`）。

- 呼び出し順序: `/voice` webhook受信 → OpenAI接続・録音開始RESTを裏で並行
  開始 → TwiML即時返却 → Media Stream `start`受信 → 挨拶クリップ即時再生
  → （並行して）プリウォームされたOpenAI接続を回収
- `start`受信〜OpenAI session.updated完了までに届いたユーザー音声は
  `CallSession._pending_audio_queue` にローカルキューし、接続完了後に
  まとめて送る。VAD解析自体は接続状態と無関係にフレーム到着時点で行う
  （キュー中に発話終了を検知した場合は`_deferred_end_of_speech`を立て、
  接続完了直後にcommit+response.createする）
- 挨拶は「モデルに言わせる」のではなく、session.updated後に
  `conversation.item.create`（role=assistant）で会話履歴に注入するだけで
  response.createは送らない。これにより次のresponseはユーザー発話のcommit
  後にのみ発生する
- `OPENAI_CONNECT_TIMEOUT_SEC`（既定5秒）以内にOpenAI接続が完了しない場合は
  縮退運転に切り替える: `DEGRADED_AUDIO_PATH`の案内クリップ再生→切電→
  要折り返しフラグ付きSalesforceケース作成→Google Chat通知
- ログ: `[GREETING] start受信からクリップ送信まで=XXms`、
  `[OA-CONNECT] webhook起点=XXms start起点=XXms (session.updated完了まで)`、
  `[QUEUE] flush frames=N (XXms分)` で各段階の所要時間を追える

## 既知の未検証事項（実機での初回テストで確認すること）

- `openai_client.py` の `session.update` に指定している音声フォーマット
  `{"type": "audio/pcmu"}` はGA版APIドキュメント調査に基づく仮説。
  `session.updated` イベントのログ（`[SESSION ECHO]`）で実際に通っているか
  確認すること
- `response.output_audio.delta` / `response.output_item.added` というイベント名
  も同様に未検証（このトランスポートでの音声中継は既存プロジェクトに前例が
  なく完全新規実装のため）。特に`response.output_item.added`が届かない場合、
  バージイン時のitem_idが取得できず`conversation.item.truncate`が送られない
  （`[BARGE-IN]`ログで`(クリップ再生のためtruncate不要)`と誤って出る形で
  判別できる）
- 挨拶を会話履歴に注入する`conversation.item.create`（role=assistant）の
  contentタイプは`"output_text"`に修正済み（実機ログで
  `invalid_request_error: Invalid value: 'text'. Value must be 'output_text'`
  を確認し対応）。今後API仕様が変わった場合は`[OA EVENT] error`ログで検知できる
