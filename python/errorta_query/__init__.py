"""Errorta query pipeline: answer + judge + grounding (F001).

AIAR-independent seam package. The real adapter (``AiarPipeline``) lives in
``errorta_judge.aiar_adapter`` and satisfies the same ``Pipeline`` protocol.
No ``import aiar`` here.
"""

from . import grounding
from .models import AnswerResult, QueryResult, Retrieval, Verdict
from .pipeline import Pipeline, StubPipeline, default_pipeline
from .signature import normalize_prompt, prompt_signature

__version__ = "0.1.0-alpha.0"

__all__ = [
    "AnswerResult",
    "QueryResult",
    "Retrieval",
    "Verdict",
    "Pipeline",
    "StubPipeline",
    "default_pipeline",
    "normalize_prompt",
    "prompt_signature",
    "grounding",
    "__version__",
]
