"""イベントループのブロックを、ループ外（別スレッド）から検知する最終安全網。

2026-08-05障害：Silero同期推論がイベントループをブロックし、全asyncioタスク
（音声中継・無音タイマー・全ウォッチドッグ）が同時に凍結した。ログはVADの
SPEECH_STARTEDで途切れ、finallyにすら到達せず、無音でも切電されなかった。
ループ上で動くウォッチドッグはブロック中は無力なため、ループに依存しない
監視が要る。

設計：
- ループ側で `heartbeat_loop()` が定期的に最終ビート時刻を更新する。
- 監視は daemon スレッドで回し、最終ビートが stall_sec 以上古ければ
  「ループがブロックされている」と判断する。
- 検知時の対処は「全スレッドのスタックをログに吐き、プロセスを異常終了させて
  Railwayに再起動させる」。ループが固まっている時点でそのプロセス上の全通話が
  既に無反応であり、個別通話だけの復帰は不可能。再起動が唯一の復帰手段。

誤発火だけは避ける必要がある（進行中の他通話を巻き添えにするため）。そのため
- 閾値は正常系では絶対に到達しない値（既定15秒。`await`はループを塞がない）
- ハートビートが動いていない間（起動前・シャットダウン後・テスト実行中）は
  監視を無効化する
の二重で守る。
"""
import asyncio
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger("loop_heartbeat")

_lock = threading.Lock()
_last_beat_monotonic = time.monotonic()
# ハートビート発信中のみ監視を有効にする。起動直後やシャットダウン後に
# 「ビートが古い」だけでプロセスを落とさないための安全弁。
_monitoring_active = False
_monitor_thread: threading.Thread | None = None


def _beat() -> None:
    global _last_beat_monotonic
    with _lock:
        _last_beat_monotonic = time.monotonic()


def _set_active(active: bool) -> None:
    global _monitoring_active, _last_beat_monotonic
    with _lock:
        _monitoring_active = active
        if active:
            # 有効化した瞬間を起点にする（起動に時間がかかっても誤発火しない）。
            _last_beat_monotonic = time.monotonic()


def _should_declare_stall(stall_sec: float) -> float | None:
    """ブロックと判断すべきならビートの経過秒数を、そうでなければNoneを返す。

    プロセスを落とす判断そのものを純粋な関数として切り出してあるので、
    単体テストで安全に検証できる（判断とos._exitを分けるのが要点）。
    """
    with _lock:
        if not _monitoring_active:
            return None
        age = time.monotonic() - _last_beat_monotonic
    return age if age >= stall_sec else None


async def heartbeat_loop(interval_sec: float = 1.0):
    """イベントループ上で回すハートビート発信。ループが生きていれば
    interval_sec ごとに最終ビートを更新する。ブロックされると更新が止まる。"""
    _set_active(True)
    try:
        while True:
            _beat()
            await asyncio.sleep(interval_sec)
    finally:
        # キャンセル（シャットダウン）時は監視も止める。止め忘れると
        # 終了処理中のビート停止をブロックと誤認してしまう。
        _set_active(False)


def _dump_all_thread_stacks() -> None:
    """全スレッドのスタックをログに吐く（どこでブロックしているか特定するため）。
    今回の障害でログが途切れて何も残らなかった反省から、次回同種の事象では
    原因箇所が即座に分かるようにする。"""
    for thread_id, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        logger.error("[LOOP-STALL] thread_id=%s stack:\n%s", thread_id, stack)


def _monitor(stall_sec: float, poll_sec: float = 1.0):
    """別スレッド（daemon）で回す監視。"""
    while True:
        time.sleep(poll_sec)
        age = _should_declare_stall(stall_sec)
        if age is None:
            continue
        logger.error(
            "[LOOP-STALL] イベントループが %.1f秒 応答していません。"
            "ブロックとみなしプロセスを再起動します（全スレッドのスタックを出力）。",
            age,
        )
        _dump_all_thread_stacks()
        # Railwayはプロセス終了時に自動再起動する。ループが固まっている以上
        # graceful shutdownは走らないので、os._exit で即時終了する
        # （クリーンアップをスキップするのは意図的）。
        os._exit(1)


def start_monitor(stall_sec: float) -> None:
    """監視スレッドを起動する（daemon）。main.pyのlifespanから呼ぶ。
    多重起動はしない（lifespanが複数回走る環境でもスレッドは1本）。"""
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(
        target=_monitor, args=(stall_sec,), name="loop-heartbeat-monitor", daemon=True,
    )
    _monitor_thread.start()
    logger.info("[LOOP-STALL] ループ監視を開始しました（stall閾値=%.0f秒）", stall_sec)


def stop() -> None:
    """シャットダウン時に呼ぶ。監視スレッド自体はdaemonなので残るが、
    無効化しておくことで終了処理中の誤発火を防ぐ。"""
    _set_active(False)
