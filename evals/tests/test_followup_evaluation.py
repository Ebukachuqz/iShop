"""F1/F7: task evidence, frozen configuration and actual processor consent."""
from dataclasses import replace

import pytest

from evals.cli.run import create_mock_evidence
from evals.runner.manifest import Episode, RunManifest
from evals.runner.runner import EvaluationRunner
from ishop.llm.base import LlmProviderError
from ishop.llm.fake import FakeLlmProvider


def manifest_for(runner, **episode_fields):
    episode = Episode(
        episode_id="case", split="dev", language_pair="eng", consent_allowed=True,
        **episode_fields,
    )
    return RunManifest.create("followup", "2026-09-12", "human_transcript",
                              "ishop-unicode-v1", [episode], configuration=runner.configuration,
                              data_kind="synthetic")


class FailingProvider(FakeLlmProvider):
    async def interpret_intent(self, request):
        raise LlmProviderError("Unavailable", "fake-offline-dev", retryable=False)


def test_f1_failed_checkout_is_not_task_success():
    runner = EvaluationRunner(create_mock_evidence(), FailingProvider())
    lines = ({"variant_id": "var_red_s", "quantity": 1},)
    manifest = manifest_for(runner, human_transcript="Take me to checkout",
                            initial_cart_lines=lines, expected_cart_lines=lines)
    row = runner.run(manifest).episodes[0]
    assert row.status == "error"
    assert row.fcem is True
    assert row.strict_success is False


def test_f7_direct_runner_rejects_tampered_manifest():
    runner = EvaluationRunner(create_mock_evidence())
    manifest = manifest_for(runner, human_transcript="Add shirt")
    with pytest.raises(ValueError, match="hash"):
        runner.run(replace(manifest, manifest_hash="tampered"))


def test_f7_actual_llm_processor_requires_consent():
    class RemoteProvider(FakeLlmProvider):
        def __init__(self):
            super().__init__()
            self._profile = replace(self.profile, provider_name="remote-company")

        async def interpret_intent(self, request):
            raise AssertionError("Unconsented transcript reached remote processor")

    runner = EvaluationRunner(create_mock_evidence(), RemoteProvider())
    manifest = manifest_for(runner, human_transcript="Add shirt")
    assert runner.run(manifest).episodes[0].status == "consent_blocked"
