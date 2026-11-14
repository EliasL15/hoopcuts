"""Decisions API adapter. Requires the official openai SDK >= 3.26.0."""
from __future__ import annotations

import base64
import logging
import math
import time
from typing import Any

from .models import ContextResult, Detection
from .game_context import CONTEXT_CONFIDENCE
from .video import SampledFrame
from .retry import RequestGate, failure

log = logging.getLogger(__name__)


class DecisionsUnavailable(RuntimeError):
    pass


class DecisionsAdapter:
    def __init__(self, api_key: str | None = None, timeout: float = 60, retries: int = 1):
        try:
            import openai
            from openai import OpenAI
        except ImportError as exc:
            raise DecisionsUnavailable("Install requirements.txt to use the OpenAI Decisions API") from exc
        version = tuple(int(part) for part in openai.__version__.split(".")[:2])
        if version < (3, 26):
            raise DecisionsUnavailable(
                f"Installed openai SDK {openai.__version__} has no documented Decisions API. "
                "Upgrade with: python -m pip install -r requirements.txt"
            )
        self.client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        self.retries = retries
        self.last_attempts = 0
        self.last_request_id: str | None = None

    def _classification_request(self, frames: list[SampledFrame]) -> dict:
        text = "Basketball video frames in chronological order. Timestamps in seconds: " + ", ".join(
            f"{f.timestamp:.3f}" for f in frames
        ) + ". Determine whether the sequence visibly establishes a completed made basket. " \
            "Do not infer a make from an attempt, ball near rim, or celebration alone. Use uncertain when evidence is insufficient."
        content = [{"type": "input_text", "text": text}]
        for frame in frames:
            image = base64.b64encode(frame.jpeg).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{image}"})
        kwargs = {
            "model": "gpt-6-luna",
            "input": [{"role": "user", "content": content}],
            "questions": [
                {"type": "choice", "name": "outcome",
                 "instructions": "Classify only visible evidence across the supplied ordered frames. A made basket requires visual evidence of the ball passing through the hoop/net or equivalent clear confirmation. A missed shot requires sufficient evidence of a miss. Otherwise choose uncertain.",
                 "choices": [
                     {"value": "made_basket", "description": "Clear visual evidence the ball completed a basket."},
                     {"value": "missed_shot", "description": "Clear visual evidence an attempted shot did not score."},
                     {"value": "uncertain", "description": "Outcome cannot be established from these frames."},
                 ]},
                {"type": "choice", "name": "shot_type",
                 "instructions": "Select the shot type only if it is visually inferable; otherwise unknown.",
                 "choices": [{"value": x, "description": desc} for x, desc in [
                     ("two_pointer", "Made shot from inside the three-point line."),
                     ("three_pointer", "Made shot from outside the three-point line."),
                     ("free_throw", "Free throw attempt."),
                     ("unknown", "Not visually inferable."),
                 ]]},
            ],
        }
        return kwargs

    def classify(self, frames: list[SampledFrame]) -> tuple[Detection, dict[str, Any]]:
        return self._request(self._classification_request(frames), lambda raw: self._parse_detection(raw, frames))

    def classify_group(self, windows: dict[str, list[SampledFrame]]) -> tuple[dict[str, Detection], dict]:
        """Experimental: independent answers restricted to each window's evidence."""
        if not 1 <= len(windows) <= 8 or any(not frames for frames in windows.values()):
            raise ValueError("A group must contain 1-8 nonempty windows")
        unique = {frame.timestamp: frame for frames in windows.values() for frame in frames}
        kwargs = self._classification_request([unique[t] for t in sorted(unique)])
        questions = []
        base_questions = self._classification_request([])["questions"]
        for key, frames in windows.items():
            evidence = ", ".join(f"{frame.timestamp:.3f}" for frame in frames)
            for question in base_questions:
                questions.append({**question, "name": f"{question['name']}_{key}", "instructions":
                    f"For window {key}, use ONLY frames at seconds {evidence}. " + question["instructions"]})
        kwargs["questions"] = questions

        def parse(raw):
            answers = raw.get("answers", [])
            named = {answer.get("name"): answer for answer in answers}
            if len(named) != len(answers):
                raise ValueError("Duplicate grouped answer names")
            results = {}
            for key, frames in windows.items():
                mapped = []
                for name in ("outcome", "shot_type"):
                    answer = named.get(f"{name}_{key}")
                    if answer is None:
                        raise ValueError(f"Missing grouped answer: {name}_{key}")
                    mapped.append({**answer, "name": name})
                results[key] = self._parse_detection({"id": raw.get("id"), "answers": mapped}, frames)
            return results
        return self._request(kwargs, parse)

    @staticmethod
    def _parse_detection(raw, frames):
        answers = {a.get("name"): a for a in raw.get("answers", [])}
        outcome, shot = answers.get("outcome", {}), answers.get("shot_type", {})
        classification = outcome.get("choice")
        shot_type = shot.get("choice")
        if outcome.get("type") != "choice" or classification not in {"made_basket", "missed_shot", "uncertain"}:
            raise ValueError("Decisions API returned an invalid outcome answer")
        if shot_type not in {"two_pointer", "three_pointer", "free_throw", "unknown"}:
            shot_type = "unknown"
        confidence = outcome.get("confidence")
        detection = Detection(classification=classification, shot_type=shot_type,
            model_confidence=confidence, estimated_event_timestamp=frames[len(frames)//2].timestamp,
            evidence_timestamps=[f.timestamp for f in frames],
            reason=None, api_response=raw)
        return detection

    def classify_context(self, frames: list[SampledFrame], event_timestamp: float) -> tuple[ContextResult, dict[str, Any]]:
        text = (
            f"Basket candidate at {event_timestamp:.3f} seconds. These wider-context frames are in chronological "
            "order at seconds: " + ", ".join(f"{frame.timestamp:.3f}" for frame in frames) + ". "
            "Determine whether the candidate occurred during the actual basketball game or during warm-ups/practice. "
            "Use only visible evidence; recording timestamps are not game-clock readings."
        )
        content = [{"type": "input_text", "text": text}]
        content.extend({"type": "input_image", "image_url": "data:image/jpeg;base64," +
                        base64.b64encode(frame.jpeg).decode("ascii")} for frame in frames)
        kwargs = {
            "model": "gpt-6-luna",
            "input": [{"role": "user", "content": content}],
            "questions": [{"type": "choice", "name": "game_context",
                "instructions": (
                    "Classify the context of the candidate at the specified timestamp, not whether it scored. "
                    "Look for organized opposing-team play, official free-throw setup, referees administering play, "
                    "or a game sequence continuing into a buzzer-beater. Preserve genuine game free throws and "
                    "buzzer-beaters even when the clock is stopped or surrounding frames show a break. "
                    "Warmup_or_break covers halftime, pregame, between-period and postgame shooting practice. "
                    "Look for multiple balls, simultaneous shooting drills and casual repeated warm-up shots. "
                    "A stopped clock, uncontested shot, absence of defenders, uniforms or recording timestamp alone "
                    "does not establish a break. Choose uncertain when the game context cannot be established."
                ),
                "choices": [
                    {"value": "game_play", "description": "The candidate occurred during official game action, including a game free throw or buzzer-beater."},
                    {"value": "warmup_or_break", "description": "The candidate is shooting practice outside actual game action."},
                    {"value": "uncertain", "description": "The candidate's game context cannot be established."},
                ]}],
        }
        return self._request(kwargs, self._parse_context)

    @staticmethod
    def _parse_context(raw) -> ContextResult:
        answers = raw.get("answers", [])
        answer = next((item for item in answers if isinstance(item, dict) and
                       item.get("name") == "game_context"), {}) if isinstance(answers, list) else {}
        confidence = answer.get("confidence")
        valid_confidence = (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                            and math.isfinite(confidence) and 0 <= confidence <= 1)
        classification = answer.get("choice")
        if (answer.get("type") != "choice" or classification not in {"game_play", "warmup_or_break", "uncertain"}
                or not valid_confidence or confidence < CONTEXT_CONFIDENCE):
            classification = "uncertain"
        return ContextResult(classification=classification,
                             confidence=confidence if valid_confidence else None,
                             checked=True, api_response=raw)

    def _request(self, kwargs, parse):
        shared_gate = getattr(self, "gate", None)
        gate = shared_gate or RequestGate(1)
        last_error = None
        self.last_attempts = 0
        self.last_request_id = None
        for attempt in range(self.retries + 1):
            gate.acquire()
            started = time.monotonic()
            sent = False
            try:
                allowance = getattr(self, "allowance", None)
                if allowance:
                    allowance(kwargs)
                    performance = getattr(self, "performance", None)
                    if performance:
                        performance.add("shared_rate_wait", time.monotonic() - started)
                    started = time.monotonic()
                self.last_attempts = attempt + 1
                sent = True
                raw_endpoint = getattr(self.client.decisions, "with_raw_response", None)
                if raw_endpoint:
                    raw_response = raw_endpoint.create(**kwargs)
                    gate.observe_headers(raw_response.headers)
                    response = raw_response.parse()
                else:
                    response = self.client.decisions.create(**kwargs)
                self.last_request_id = getattr(response, "id", None)
                raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)
                result = parse(raw), raw.get("usage", {}) or {}
                gate.succeeded()
                return result
            except Exception as exc:
                if not sent:
                    raise
                last_error = failure(exc)
                gate.failed(last_error, attempt)
                if shared_gate is None and last_error.retryable and attempt < self.retries:
                    # Standalone adapters have no peers to coordinate with.
                    time.sleep(max(last_error.retry_after or 0, min(5 * 2 ** attempt, 60)))
                    gate.until = 0
                if attempt >= self.retries or not last_error.retryable:
                    break
            finally:
                performance = getattr(self, "performance", None)
                if performance and sent:
                    performance.request(time.monotonic() - started)
                gate.release()
        raise last_error
