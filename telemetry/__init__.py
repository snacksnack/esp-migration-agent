"""Observability for the agent's own runs (RC1-548): LLM Obs tracing and
run metrics. Decoration by contract - with no ddtrace installed and no
DD_API_KEY in the environment, every function here is a quiet no-op and the
migration pipeline behaves identically."""

from telemetry.llmobs import enable_llmobs, workflow
from telemetry.metrics import count, stage

__all__ = ["count", "enable_llmobs", "stage", "workflow"]
