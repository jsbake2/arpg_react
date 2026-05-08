from __future__ import annotations

import logging
import queue
import threading
from typing import Protocol

log = logging.getLogger(__name__)

_SHUTDOWN = object()


class TTSPlayer(Protocol):
    def say(self, text: str) -> None: ...
    def close(self) -> None: ...


class Pyttsx3Player:
    """Speaks text via pyttsx3 on a single dedicated worker thread.

    pyttsx3's espeak-ng backend serializes calls and `runAndWait` blocks; we
    isolate it on a worker so the daemon's main loop never stalls.

    Engine init is lazy and on-the-worker — pyttsx3 / espeak-ng issues raised
    on import or first call do not propagate to the daemon's import path.
    Failures are logged once and degrade to no-op.
    """

    def __init__(self, voice: str | None = None, rate: int = 180) -> None:
        self._voice = voice
        self._rate = rate
        self._queue: queue.Queue = queue.Queue()
        self._engine_failed = False
        self._thread = threading.Thread(target=self._run, name="tts-worker", daemon=True)
        self._thread.start()

    def say(self, text: str) -> None:
        if self._engine_failed:
            return
        self._queue.put(text)

    def close(self) -> None:
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        engine = self._init_engine()
        if engine is None:
            self._engine_failed = True
            self._drain_queue_silently()
            return

        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                try:
                    engine.stop()
                except Exception:  # noqa: BLE001
                    pass
                return
            text = str(item)
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as exc:  # noqa: BLE001
                log.warning("tts say() failed: %s", exc)

    def _init_engine(self):
        try:
            import pyttsx3
        except ImportError as exc:
            log.warning("pyttsx3 not importable; TTS disabled: %s", exc)
            return None
        try:
            engine = pyttsx3.init()
        except Exception as exc:  # noqa: BLE001
            log.warning("pyttsx3 init failed (espeak-ng installed?); TTS disabled: %s", exc)
            return None
        try:
            if self._voice:
                engine.setProperty("voice", self._voice)
            engine.setProperty("rate", self._rate)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set tts voice/rate: %s", exc)
        return engine

    def _drain_queue_silently(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                return


class NullTTSPlayer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def say(self, text: str) -> None:
        self.calls.append(text)

    def close(self) -> None:
        pass
