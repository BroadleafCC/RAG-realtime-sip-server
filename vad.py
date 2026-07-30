"""
発話区切り判定の状態機械。本プロジェクトの核。

非同期・WebSocket・OpenAI/Twilioの知識を一切持たない、純粋で同期的な
モジュール。既存システムで発生した「VAD状態が無音/最大時間/応答待ちの
elifチェーンに巻き込まれて動かなくなる」バグを構造的に再発させないため、
この状態機械はタイムアウト監視（watchdogs.py）から完全に独立している。

状態遷移は確率のみで駆動される。AWAITING_RESPONSE -> IDLE だけは例外で、
OpenAIの response.done を受けた呼び出し側が force_idle() を呼ぶことでのみ
起こる（発話確率では駆動しない）。
"""
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class VadState(Enum):
    IDLE = auto()
    SPEAKING = auto()
    AWAITING_RESPONSE = auto()


class VadEvent(Enum):
    SPEECH_STARTED = auto()
    END_OF_SPEECH = auto()
    BARGE_IN = auto()


@dataclass
class VadTransition:
    event: Optional[VadEvent]
    state: VadState
    prob: float
    elapsed_ms: float


class TurnDetector:
    def __init__(
        self,
        threshold: float = 0.5,
        speech_start_ms: int = 250,
        speech_end_ms: int = 650,
        barge_in_min_ms: int = 500,
    ):
        self.threshold = threshold
        self.speech_start_ms = speech_start_ms
        self.speech_end_ms = speech_end_ms
        self.barge_in_min_ms = barge_in_min_ms

        self.state = VadState.IDLE
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._barge_in_run_ms = 0.0
        self._barge_in_fired = False
        self._was_ai_speaking = False

    def force_idle(self) -> None:
        """response.done 受信時にのみ呼ぶ。次のAIターンに備えてバージイン
        判定もリセットする。"""
        self.state = VadState.IDLE
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._barge_in_run_ms = 0.0
        self._barge_in_fired = False

    def update(self, prob: float, frame_ms: float, ai_is_speaking: bool) -> VadTransition:
        is_speech = prob >= self.threshold

        # バージイン判定はメイン状態と並行かつ独立に集計する。
        # AI発話ターンが変わるたびに armed/fired フラグをリセットする。
        if ai_is_speaking:
            if not self._was_ai_speaking:
                self._barge_in_run_ms = 0.0
                self._barge_in_fired = False
            self._barge_in_run_ms = self._barge_in_run_ms + frame_ms if is_speech else 0.0
            self._was_ai_speaking = True

            if not self._barge_in_fired and self._barge_in_run_ms >= self.barge_in_min_ms:
                self._barge_in_fired = True
                self.state = VadState.SPEAKING
                self._silence_run_ms = 0.0
                self._speech_run_ms = 0.0
                return VadTransition(VadEvent.BARGE_IN, self.state, prob, self._barge_in_run_ms)

            # AI発話中はバージイン閾値に達するまでメイン状態機械を進めない。
            # ここで進めてしまうと、バージインに満たない短い割り込みが
            # IDLE->SPEAKING->(無音650ms)->END_OF_SPEECH まで進行し、
            # AIがまだ話している最中に誤ってcommitが送られてしまう。
            return VadTransition(None, self.state, prob, self._barge_in_run_ms)

        self._barge_in_run_ms = 0.0
        self._was_ai_speaking = False

        if self.state == VadState.IDLE:
            self._speech_run_ms = self._speech_run_ms + frame_ms if is_speech else 0.0
            if self._speech_run_ms >= self.speech_start_ms:
                self.state = VadState.SPEAKING
                self._silence_run_ms = 0.0
                return VadTransition(VadEvent.SPEECH_STARTED, self.state, prob, self._speech_run_ms)
            return VadTransition(None, self.state, prob, self._speech_run_ms)

        if self.state == VadState.SPEAKING:
            self._silence_run_ms = 0.0 if is_speech else self._silence_run_ms + frame_ms
            if self._silence_run_ms >= self.speech_end_ms:
                self.state = VadState.AWAITING_RESPONSE
                return VadTransition(VadEvent.END_OF_SPEECH, self.state, prob, self._silence_run_ms)
            return VadTransition(None, self.state, prob, self._silence_run_ms)

        # AWAITING_RESPONSE: 確率では遷移しない。force_idle() 待ち。
        return VadTransition(None, self.state, prob, 0.0)
