from __future__ import annotations

import random
import unittest

from naiauto.core.api.models import GenerationRequest
from naiauto.services.generation_service import GenerationJob, GenerationService

RESOLUTION_CHOICES = ((1216, 832), (1024, 1024), (832, 1216))


class _ExpandingWildcards:
    def create_index_snapshot(self) -> None:
        pass

    def apply_wildcards_with_snapshot(self, prompt: str) -> str:
        return prompt.replace("__scene__", "1girl, full body, <res:portrait>")

    def advance_loopcard_indices(self) -> None:
        pass


class GenerationServiceResolutionDirectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = GenerationService(object(), rng=random.Random(7))

    def tearDown(self) -> None:
        self.service.shutdown()

    def prepare(
        self,
        prompt: str,
        *,
        action: str = "generate",
        randomize_resolution: bool = False,
    ) -> GenerationRequest:
        request = GenerationRequest(
            action=action,
            prompt=prompt,
            width=640,
            height=640,
            image=b"source" if action != "generate" else None,
        )
        job = GenerationJob(
            request=request,
            randomize_seed=False,
            randomize_resolution=randomize_resolution,
            resolution_choices=RESOLUTION_CHOICES if action == "generate" else (),
        )
        return self.service._prepare_request(job)

    def assertPrepared(self, prompt: str, expected_prompt: str, expected_size: tuple[int, int]) -> None:
        request = self.prepare(prompt)
        self.assertEqual(request.prompt, expected_prompt)
        self.assertEqual((request.width, request.height), expected_size)

    def test_portrait_directive_at_end(self) -> None:
        self.assertPrepared("1girl, <res:portrait>", "1girl", (832, 1216))

    def test_square_directive_at_start(self) -> None:
        self.assertPrepared("<res:square>, 1girl", "1girl", (1024, 1024))

    def test_wide_directive_between_prompt_segments(self) -> None:
        self.assertPrepared("1girl, <res:wide>, outdoors", "1girl, outdoors", (1216, 832))

    def test_no_directive_preserves_existing_resolution_behavior(self) -> None:
        self.assertPrepared("1girl, outdoors", "1girl, outdoors", (640, 640))

    def test_last_directive_wins_and_all_are_removed(self) -> None:
        self.assertPrepared(
            "1girl, <res:square>, <res:PORTRAIT>",
            "1girl",
            (832, 1216),
        )

    def test_directive_overrides_random_resolution(self) -> None:
        request = self.prepare("1girl, <RES:square>", randomize_resolution=True)
        self.assertEqual((request.width, request.height), (1024, 1024))

    def test_unknown_directive_is_left_untouched(self) -> None:
        self.assertPrepared("1girl, <res:banana>", "1girl, <res:banana>", (640, 640))

    def test_bare_aspect_word_is_not_a_directive(self) -> None:
        self.assertPrepared("1girl, portrait", "1girl, portrait", (640, 640))

    def test_image_conditioned_actions_keep_source_resolution_but_strip_directive(self) -> None:
        for action in ("img2img", "infill"):
            with self.subTest(action=action):
                request = self.prepare("1girl, <res:wide>", action=action)
                self.assertEqual(request.prompt, "1girl")
                self.assertEqual((request.width, request.height), (640, 640))

    def test_directive_from_wildcard_expansion_is_detected(self) -> None:
        self.service.shutdown()
        self.service = GenerationService(
            object(), wildcards=_ExpandingWildcards(), rng=random.Random(7)
        )
        request = self.prepare("__scene__")
        self.assertEqual(request.prompt, "1girl, full body")
        self.assertEqual((request.width, request.height), (832, 1216))


if __name__ == "__main__":
    unittest.main()
