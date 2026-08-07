"""Tests for artifact and output contract checking."""

from __future__ import annotations

import pytest

from makethlm.contracts import (
    required_artifact_error,
    split_artifact_contract,
    value_matches,
)


class TestValueMatches:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("anything", "text"),
            ("", "text"),
            ("x", "nonempty"),
            ("42", "integer"),
            ("-7", "integer"),
            ("1.5", "number"),
            ("42", "number"),
            ("true", "boolean"),
            ("FALSE", "boolean"),
            ('{"a": 1}', "json"),
            ("[1, 2]", "json"),
            ('{"a": 1}', "object"),
            ("[1, 2]", "array"),
        ],
    )
    def test_accepts(self, value, expected):
        assert value_matches(value, expected)

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("   ", "nonempty"),
            ("4.5", "integer"),
            ("many", "number"),
            ("yes", "boolean"),
            ("not json", "json"),
            ("[1, 2]", "object"),
            ('{"a": 1}', "array"),
            ("{}", "unknown-type"),
        ],
    )
    def test_rejects(self, value, expected):
        assert not value_matches(value, expected)


class TestSplitArtifactContract:
    def test_with_explicit_type(self):
        assert split_artifact_contract("build.stdout:object") == ("build", "stdout", "object")

    def test_defaults_to_nonempty(self):
        assert split_artifact_contract("build.stdout") == ("build", "stdout", "nonempty")

    def test_lowercases_the_type(self):
        assert split_artifact_contract("build.stdout:OBJECT")[2] == "object"

    def test_rejects_missing_field(self):
        with pytest.raises(ValueError, match="expected artifact.field"):
            split_artifact_contract("build")


class TestRequiredArtifactError:
    def _artifacts(self, **values):
        return {"build": {"stdout": "", "response": "", **values}}

    def test_satisfied_contract_returns_none(self):
        artifacts = self._artifacts(stdout='{"ok": true}')
        assert required_artifact_error(["build.stdout:object"], artifacts) is None

    def test_missing_artifact(self):
        error = required_artifact_error(["missing.stdout"], {})
        assert error is not None and "is not available" in error

    def test_missing_field(self):
        error = required_artifact_error(["build.nope"], self._artifacts())
        assert error is not None and "field 'nope'" in error

    def test_wrong_type(self):
        error = required_artifact_error(["build.stdout:object"], self._artifacts(stdout="text"))
        assert error is not None and "is not object" in error

    def test_unknown_type(self):
        error = required_artifact_error(["build.stdout:banana"], self._artifacts())
        assert error is not None and "unknown type" in error

    def test_malformed_contract(self):
        error = required_artifact_error(["build"], self._artifacts())
        assert error is not None and "expected artifact.field" in error

    def test_first_failure_is_reported(self):
        artifacts = self._artifacts(stdout="text")
        error = required_artifact_error(["missing.stdout", "build.stdout:object"], artifacts)
        assert error is not None and "missing" in error

    def test_no_contracts_is_fine(self):
        assert required_artifact_error([], {}) is None
