"""Provider-neutral primitives for parallel LLM fix generation.

This module deliberately contains no credentials. Provider instances are injected by
the application, which keeps keys out of source control and makes the batch layer
usable with mocks during development.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional
import hashlib
import logging
import os
import time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueKeySlot:
    """A credential and model assignment with a per-run issue allowance."""

    key_id: str
    client: Any = field(compare=False, repr=False)
    provider: str = "groq"
    model: str = ""


class IssueKeyPool:
    """Allocate distinct fix issues across keys with strict per-key/run caps.

    Claiming a slot consumes one issue allowance. A retry or another iteration
    does not get a fresh allowance for the same run.
    """

    def __init__(self, slots: Iterable[IssueKeySlot], per_key_limit: int = 10,
                 run_limit: int = 50):
        self.slots = list(slots)
        self.per_key_limit = max(1, int(per_key_limit))
        self.run_limit = max(1, int(run_limit))
        self.usage = {slot.key_id: 0 for slot in self.slots}
        self.total = 0
        self._cursor = 0

    def claim(self) -> Optional[IssueKeySlot]:
        if self.total >= self.run_limit or not self.slots:
            return None
        for _ in range(len(self.slots)):
            slot = self.slots[self._cursor % len(self.slots)]
            self._cursor += 1
            if self.usage[slot.key_id] < self.per_key_limit:
                self.usage[slot.key_id] += 1
                self.total += 1
                return slot
        return None

    @property
    def remaining(self) -> int:
        return min(self.run_limit - self.total,
                   sum(max(0, self.per_key_limit - used) for used in self.usage.values()))


@dataclass(frozen=True)
class ModelSpec:
    """One configured model endpoint. ``client`` is supplied at runtime."""

    provider: str
    model: str
    client: Any = field(compare=False, repr=False)
    enabled: bool = True
    weight: float = 1.0


@dataclass
class ModelResult:
    provider: str
    model: str
    value: Any = None
    error: Optional[str] = None
    elapsed_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.value is not None


@dataclass
class BatchResult:
    results: List[ModelResult]
    winner: Optional[ModelResult]
    agreement: float
    strategy: str


class MultiModelBatch:
    """Run a request across configured models and select a stable result.

    Selection favors the configured primary provider, then weighted agreement.
    Candidate identity is based on normalized output so callers can return full
    structured results without coupling this class to a particular fixer schema.
    """

    def __init__(self, models: Iterable[ModelSpec], primary_provider: str = "",
                 max_workers: Optional[int] = None):
        self.models = [m for m in models if m.enabled]
        self.primary_provider = (primary_provider or "").strip().lower()
        self.max_workers = max_workers or max(1, len(self.models))

    @staticmethod
    def _identity(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("fixed_code") or value.get("code") or value
        if not isinstance(value, str):
            value = repr(value)
        normalized = "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").strip().splitlines())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def run(self, request: Any, generate: Callable[[ModelSpec, Any], Any]) -> BatchResult:
        if not self.models:
            return BatchResult([], None, 0.0, "no_models")

        results: List[ModelResult] = []

        def invoke(spec: ModelSpec) -> ModelResult:
            started = time.monotonic()
            try:
                value = generate(spec, request)
                if value is None or value == {}:
                    return ModelResult(spec.provider, spec.model, error="empty_response",
                                       elapsed_ms=int((time.monotonic() - started) * 1000))
                return ModelResult(spec.provider, spec.model, value=value,
                                   elapsed_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:  # provider failures should not cancel other candidates
                logger.info("Model %s/%s failed: %s", spec.provider, spec.model, type(exc).__name__)
                return ModelResult(spec.provider, spec.model, error=type(exc).__name__,
                                   elapsed_ms=int((time.monotonic() - started) * 1000))

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self.models))) as pool:
            futures = [pool.submit(invoke, spec) for spec in self.models]
            for future in as_completed(futures):
                results.append(future.result())

        successful = [r for r in results if r.succeeded]
        if not successful:
            return BatchResult(results, None, 0.0, "all_failed")

        groups: Dict[str, List[ModelResult]] = {}
        for result in successful:
            groups.setdefault(self._identity(result.value), []).append(result)
        winning_group = max(groups.values(), key=lambda group: (
            sum(next(m.weight for m in self.models if m.provider == r.provider and m.model == r.model)
                for r in group),
            any(r.provider.lower() == self.primary_provider for r in group),
            len(group),
        ))
        winner = next((r for r in winning_group if r.provider.lower() == self.primary_provider), winning_group[0])
        return BatchResult(results, winner, len(winning_group) / len(successful),
                           "consensus" if len(winning_group) > 1 else "primary_or_weighted")


def configured_batch_size() -> int:
    """Read a bounded parallelism limit from environment configuration."""
    try:
        return max(1, min(8, int(os.getenv("LLM_BATCH_SIZE", "2"))))
    except (TypeError, ValueError):
        return 2
