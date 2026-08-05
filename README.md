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
| `main.py` | FastAPIアプリ本体（`/voice`, `/media-stream`, `/call-status`, `/recording/{sid}`, `/health`） |
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
| `loop_heartbeat.py` | イベントループのブロックをループ外（別スレッド）から検知する最終安全網 |
| `assets/audio/` | 挨拶・即時相槌・縮退運転案内の事前生成済み音声アセット |
| `scripts/generate_static_clips.py` | 上記音声アセットの生成/再生成スクリプト（OpenAI TTS使用） |

## 応答速度・音声頭切れチューニング

- `VAD_SPEECH_END_MS`（既定500ms）: 発話終了とみなすまでの無音継続時間。短くする
  ほど応答は速くなるが、語尾の「間」を誤って区切るリスクが上がる。
- `RESPONSE_LEAD_SILENCE_MS`（既定250ms）: 各応答の音声冒頭に挿入する無音の長さ。
  出力ストリームが立ち上がる瞬間の頭切れ対策。
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

## VAD推論とイベントループ（2026-08-05障害の恒久対策）

**症状**：挨拶再生後に話しかけてもAIが無反応になり、無音が続いても切電されない。
ログは `[VAD] event=SPEECH_STARTED` で完全に途切れ、`finally` の `[DISCONNECT]`
すら出ない（＝例外ではなくブロック）。録音には26秒分の音声が残っていた。

**原因**：`SileroVad.feed()` の同期onnx推論をイベントループ上で直接実行していた。
`feed()` はバッファ蓄積型のため、入力ペースが消費ペースを上回ると1回のfeedで
推論を連続実行 → ループを長くブロック → その間さらに音声が溜まる、という悪循環に
入り、ループが復帰しなくなる。ループが止まれば `vad.TurnDetector.update()` も
3つのウォッチドッグも同時に凍る（＝発話終了検知も切電も止まる）。

対策は3本柱：

1. **推論を専用スレッドプールへ退避**（`VAD_INFERENCE_IN_THREAD`、既定ON）。
   `call_session._vad_feed` が `run_in_executor(_vad_executor, ...)` で実行する。
   `asyncio.to_thread` の既定プールを使わないのは、録音開始リトライ
   （最大約2.4秒スリープ）と同居させると巻き添えで推論が待たされるため。
   **`SileroVad` はスレッドセーフではない**ので、`pump_twilio_to_openai` の単一
   whileループから逐次 `await` する形（＝同一通話で `feed` が重ならない）を
   必ず維持すること。
2. **VADバッファの上限**（`VAD_MAX_BUFFER_SEC`、既定1.0秒）。超過分は古い側から
   捨てる。リアルタイム音声では遅れた古い音声を処理し続けるより最新に追いつく
   ほうが正しい。破棄が起きると `[VAD-DROP]` を5秒に1回まで間引いて出す。
   **通常の通話では出ない**。常態的に出るなら①が効いていない兆候。
3. **ループ・ハートビート監視**（`LOOP_HEARTBEAT_ENABLED`、既定ON）。
   ループ上のウォッチドッグはループが固まれば一緒に死ぬため、監視だけを
   daemonスレッドに置く。ハートビートが `LOOP_HEARTBEAT_STALL_SEC`（既定15秒）
   途絶えたら全スレッドのスタックを吐いて `os._exit(1)` し、Railwayに再起動
   させる。ループが固まった時点でそのプロセス上の全通話が既に無反応なので、
   再起動が唯一の復帰手段。**閾値を15秒より短くしないこと**（誤発火は進行中の
   他通話を巻き添えにする）。起動前・シャットダウン後は監視を無効化してあり、
   その状態では絶対に発火しない。

**今後の原則**：ローカル推論などの同期CPU処理を `async` 関数内で直接呼ばない。
必ず `run_in_executor` / `to_thread` に逃がす（録音RESTでは既にこのパターンを
使っていたのに、VAD推論に適用が漏れていた）。

## pump停止（awaitハング）の検知と強制復帰

**症状**（VAD推論スレッド化の後も再発）：`[VAD] SPEECH_STARTED` を最後にログが
途絶え、AIが無反応のまま切電もされない。

**前回診断の訂正**：ハートビート監視（`[LOOP-STALL]`）が稼働していたにも
かかわらず33秒以上発火しなかった＝**イベントループは生きていた**。よって
「ループブロック」は誤診で、実体は `pump_twilio_to_openai` タスクが特定の
`await` から返ってこない**awaitハング**。ハートビートは発火しないことで
「ループは無実」を証明した（診断器として機能したので維持している）。

**切電されなかった理由＝安全網の設計盲点**：`silence_watchdog` はVAD状態が
IDLEでないとcontinueする。pumpが止まると `TurnDetector.update()` が呼ばれず
状態はSPEAKINGに固着し、無音ウォッチドッグは構造的に発火できない。唯一残る
`max_duration_watchdog` も、その中の `_say_and_wait_for_goodbye` が
**OpenAI経由**のため、OpenAIソケットが半死だと安全網自体がハングして
ケース作成にすら到達しなかった。

対策：

| 対策 | 内容 |
|---|---|
| 送信タイムアウト | `OpenAiRealtimeSocket._send` に `OPENAI_SEND_TIMEOUT_SEC`（既定3秒）。全送信メソッドがこの1経路を通るので漏れない。超過で `OpenAiSendTimeout` |
| 受信タイムアウト | `twilio_ws.receive_text()` に `TWILIO_RECV_TIMEOUT_SEC`（既定10秒）。mediaは20ms間隔で常時届くので10秒無受信は異常 |
| VAD推論タイムアウト | `VAD_FEED_TIMEOUT_SEC`（既定2秒）。当該フレームのVADをスキップして継続し、3回連続で縮退 |
| pump進捗ウォッチドッグ | `PUMP_STALL_SEC`（既定10秒）。**状態ではなく進捗**（`last_frame_processed_at`）だけを見るため、状態固着でも必ず発火する |
| 原因特定器 | 発火時に `task.get_stack()` でpumpタスクのスタックを `[PUMP-STALL] pump stack` としてダンプ。**止まっているawaitの行を名指しする** |
| 縮退経路 | `call_session.degrade_and_end`：クリップ再生→切電→ケース作成→`CallEnded`。**OpenAIへ一切送信しない**（故障を疑う相手に依存しない） |

**再発時の読み方**：`[PUMP-STALL] pump stack` の最深フレームが原因を確定する。
`append_audio` ならOpenAI側の不良セッション（2回とも起動後1本目の通話で発生
＝「1発目」相関があり本命仮説）、`receive_text` ならTwilio側、`_vad_feed` なら
onnx側へ調査が分岐する。

**原則**：外部I/Oのawaitには必ずタイムアウトを付ける。WebSocket送信はフロー
制御で例外を出さずに永久ブロックしうるため、try/exceptでは捕らえられない。
ウォッチドッグは「状態」ではなく「進捗」を監視する。

## 切断主体の推定記録（`DISCONNECT_TRACKING_ENABLED`）

「つながった瞬間に切れた」通話が、サーバーのバグなのか発信側の都合なのかを
毎回ログと録音の突き合わせで手作業判定していたため、通話終了時に1行で
分類ログを出す（既定OFF。検証時のみ `DISCONNECT_TRACKING_ENABLED=true`）。

**Twilioは「発信側が切ったか着信側が切ったか」を返さない。** ここで行うのは
観測可能な事実の合成による『推定』であり、断定ではない。判定材料は
サーバー内部状態が主で、Twilioのステータスコールバック（`/call-status`）は
Durationの裏取り用の補助情報でしかない（届かなくても分類は成立し、
`duration=?` になるだけ）。分類ログの出力位置は「通話後処理へ入る直前」に
固定してあり、コールバックの到着は待たない。

| カテゴリ | 意味 |
|---|---|
| `SERVER_INITIATED_HANGUP` | 無音/最大通話時間/応答ウォッチドッグ/接続失敗でサーバー側から切った |
| `PRE_CONNECT_HANGUP` | `[OA-CONNECT] session確立` 到達前に終了（接続前切れ・要録音確認） |
| `HANGUP_DURING_GREETING` | OA接続後・挨拶クリップの再生完了mark前に終了 |
| `HANGUP_NO_SPEECH` | 挨拶は流れたがVADの発話検知ゼロで終了（無言切り） |
| `SHORT_CALL_AFTER_SPEECH` | 発話ありだがDurationが5秒未満（途中切れの可能性） |
| `NORMAL_COMPLETION` | 会話成立後の終了 |

再生の進捗判定には**markのみ**を使う（`response.done`はサーバー側の生成完了
でしかなく、電話口での再生完了より大きく先行するため根拠にならない）。
分岐は早期returnのみで書く（elifチェーンで安全装置が死んだ前科があるため）。

ログ例:
`[DISCONNECT] call_sid=CAxxx category=PRE_CONNECT_HANGUP duration=0s end_reason=twilio_stop oa_established=False any_speech=False greeting_done=False last_mark=- response_count=0 sip=- :: 推定 ...`

## 録音の取得（Call SID 直引き・録音開始のリトライ）

- **録音開始（`RECORDING_RETRY_ENABLED`、既定true）**: 通話が in-progress へ
  完全に遷移する前に `recordings.create()` を叩くとTwilioは21220
  （Requested resource is not eligible for recording）で拒否する。
  `twilio_client.start_recording` は21220に対して 300/600/900ms のバックオフで
  最大4回リトライし、成功したRecording SIDを返す。待機に`time.sleep`を使うため
  **必ず `asyncio.to_thread` 経由で呼ぶこと**（イベントループを塞がない）。
- **録音の特定は Call SID 紐付けに固定**: 「`start_recording`が返したSID」→
  「`calls(call_sid).recordings`」の順で引く。時間窓（`DateCreated>`）＋直近N件で
  探す実装は、録音開始に失敗した通話が**直前の別通話の録音を掴む**
  （＝他人の通話内容がケースに入る）事故を起こしたため撤去済み。
  **時間窓で録音を探す実装を今後書かないこと。**
- **録音が無い通話**: 他通話の録音で埋めず、`[POSTCALL] 録音無し` を出して
  ケースの対応備考に「録音取得失敗のため通話内容なし」と明記する。
  ケース自体は従来どおり作成する（案件を落とさない設計思想は維持）。

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
