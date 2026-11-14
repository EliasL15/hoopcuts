from typing import Literal

from pydantic import BaseModel, Field


Classification = Literal["made_basket", "missed_shot", "uncertain"]
ShotType = Literal["two_pointer", "three_pointer", "free_throw", "unknown"]
GameContext = Literal["game_play", "warmup_or_break", "uncertain"]


class ContextResult(BaseModel):
    classification: GameContext = "uncertain"
    confidence: float | None = Field(default=None, ge=0, le=1)
    checked: bool = False
    api_response: dict = Field(default_factory=dict)


class Detection(BaseModel):
    classification: Classification
    shot_type: ShotType = "unknown"
    model_confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None
    estimated_event_timestamp: float | None = Field(default=None, ge=0)
    evidence_timestamps: list[float] = Field(default_factory=list)
    api_response: dict = Field(default_factory=dict)
    refinement_sources: list[str] = Field(default_factory=list)


class Event(BaseModel):
    event_id: str
    timestamp_seconds: float = Field(ge=0)
    clip_start_seconds: float = Field(ge=0)
    clip_end_seconds: float = Field(ge=0)
    classification: Classification
    shot_type: ShotType = "unknown"
    model_confidence: float | None = Field(default=None, ge=0, le=1)
    needs_review: bool
    evidence_timestamps: list[float]
    reason: str | None = None
    source_windows: list[str] = Field(default_factory=list)
    clip_path: str | None = None
    included: bool = True
    game_context: GameContext | None = None
