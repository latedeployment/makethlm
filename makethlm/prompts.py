"""Prompts makethlm composes itself.

Contract repair, fan-out reporting, and judge merging each need a prompt built
around the task's own prompt, so they live together rather than inside the
runner's control flow.
"""

from __future__ import annotations

from .dispatcher import DispatchResult
from .models import TaskStep

MAX_REPAIR_ECHO_CHARS = 2000

_CONTRACT_REPAIR_HINTS = {
    "json": "a single valid JSON value",
    "object": "a single valid JSON object",
    "array": "a single valid JSON array",
    "integer": "a single integer with no other characters",
    "number": "a single number with no other characters",
    "boolean": "exactly true or false",
    "nonempty": "a non-empty answer",
    "text": "a text answer",
}


def format_fanout_response(results: list[tuple[str, DispatchResult]]) -> str:
    """Return every fan-out answer, labeled by provider."""
    sections = []
    for name, outcome in results:
        status = "" if outcome.success else " (failed)"
        sections.append(f"[{name}{status}]\n{outcome.response.strip()}")
    return "\n\n".join(sections)


def build_judge_prompt(prompt: str, answers: list[tuple[str, str]]) -> str:
    """Return the prompt asking a judge provider to merge fan-out answers."""
    sections = [f"--- answer from {name} ---\n{text.strip()}" for name, text in answers]
    joined = "\n\n".join(sections)
    return (
        f"{len(answers)} models were given the same task. Merge their answers into a "
        f"single best response.\n\n"
        f"Original task:\n{prompt}\n\n"
        f"{joined}\n\n"
        "Reply with the merged answer only. Prefer claims the models agree on, drop "
        "anything contradicted or unsupported, and do not mention the models or that "
        "a merge took place."
    )


def _last_prompt_index(steps: list[TaskStep]) -> int | None:
    """Return the 1-based index of the final prompt step, if any.

    Only that step is validated against ``produces`` during execution: it is
    the one whose response the contract can still be repaired through.
    """
    indexes = [i for i, step in enumerate(steps, 1) if step.kind not in ("echo", "shell")]
    return indexes[-1] if indexes else None


def build_repair_prompt(prompt: str, expected: str, previous: str) -> str:
    """Return a re-prompt asking the provider to satisfy an output contract."""
    wanted = _CONTRACT_REPAIR_HINTS.get(expected, f"a value of type {expected}")
    echoed = previous.strip()
    if len(echoed) > MAX_REPAIR_ECHO_CHARS:
        echoed = echoed[:MAX_REPAIR_ECHO_CHARS] + "\n[...truncated]"
    return (
        f"{prompt}\n\n"
        f"Your previous response did not satisfy the required output contract "
        f"produces={expected}. It must be {wanted}, with no prose, explanation, "
        f"or code fences around it.\n\n"
        f"Previous response:\n{echoed}\n\n"
        f"Reply with the corrected output only."
    )
