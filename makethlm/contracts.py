"""Artifact and output contract checking.

``requires`` validates upstream artifact fields before a task starts;
``produces`` validates the task's aggregate output afterwards. Both reduce to
the same question — does this string satisfy a named type — which is what this
module answers.
"""

from __future__ import annotations

import json

from .models import ARTIFACT_CONTRACT_TYPES

DEFAULT_CONTRACT_TYPE = "nonempty"


def value_matches(value: str, expected: str) -> bool:
    """Return whether a string value satisfies a supported contract type."""
    if expected == "text":
        return True
    if expected == "nonempty":
        return bool(value.strip())
    if expected == "integer":
        try:
            int(value)
        except ValueError:
            return False
        return True
    if expected == "number":
        try:
            float(value)
        except ValueError:
            return False
        return True
    if expected == "boolean":
        return value.strip().lower() in ("true", "false")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    if expected == "json":
        return True
    if expected == "object":
        return isinstance(parsed, dict)
    if expected == "array":
        return isinstance(parsed, list)
    return False


def split_artifact_contract(contract: str) -> tuple[str, str, str]:
    """Split ``artifact.field[:type]`` into its components."""
    possible_reference, separator, suffix = contract.rpartition(":")
    if separator and "." not in suffix:
        reference = possible_reference
        expected = suffix
    else:
        reference = contract
        expected = DEFAULT_CONTRACT_TYPE
    artifact, dot, field_name = reference.rpartition(".")
    if not dot or not artifact or not field_name:
        raise ValueError(f"invalid artifact contract {contract!r}; expected artifact.field[:type]")
    return artifact, field_name, expected.lower()


def required_artifact_error(
    contracts: list[str],
    artifacts: dict[str, dict[str, str]],
) -> str | None:
    """Return an actionable error for the first unmet input contract."""
    for contract in contracts:
        try:
            artifact, field_name, expected = split_artifact_contract(contract)
        except ValueError as e:
            return f"artifact contract failed: {e}"
        if expected not in ARTIFACT_CONTRACT_TYPES:
            return f"artifact contract failed: unknown type {expected!r} in {contract!r}"
        if artifact not in artifacts:
            return f"artifact contract failed: {artifact!r} is not available"
        values = artifacts[artifact]
        if field_name not in values:
            return (
                f"artifact contract failed: field {field_name!r} is not available on {artifact!r}"
            )
        if not value_matches(values[field_name], expected):
            return f"artifact contract failed: {artifact}.{field_name} is not {expected}"
    return None
