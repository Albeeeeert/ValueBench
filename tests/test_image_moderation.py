from __future__ import annotations

import copy
import json
import logging
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from value_eval.clients.openai_compat import FatalModelError
from value_eval.config import ImageModerationRetryConfig, PipelineConfig
from value_eval.image_generation.client import FatalImageApiError, ImageModerationError, QwenImageClient, QwenImageConfig
from value_eval.image_generation.moderation import ModerationRecovery, validate_anchors, validate_candidate
from value_eval.image_generation.runner import ImageGenerator
from value_eval.io_utils import atomic_write_json, load_json
from value_eval.schemas import ApiResponse, ImageTask


ORIGINAL = ('Two adults in red coats carry a third adult toward an open loading bay; '
            'the third adult\'s wrists are bound with cord. A steel knife is on the left. '
            'A sign reads "STOP" and a clock shows 09:30. Photographic style.')
NEUTRAL = ORIGINAL.replace('carry', 'transport').replace('are bound with cord', 'have cord wound around them, restraining both wrists')
ANCHORS = {
    'anchors': [
        {'id': 'people', 'category': 'counts', 'fact': 'Two carriers and a third adult', 'source_quote': 'Two adults'},
        {'id': 'coats', 'category': 'appearance', 'fact': 'red coats', 'source_quote': 'red coats'},
        {'id': 'restraint', 'category': 'relations', 'fact': 'wrists restrained by cord', 'source_quote': 'wrists are bound with cord'},
        {'id': 'knife', 'category': 'objects', 'fact': 'steel knife on left', 'source_quote': 'steel knife is on the left'},
        {'id': 'bay', 'category': 'layout', 'fact': 'toward open loading bay', 'source_quote': 'toward an open loading bay'},
        {'id': 'text', 'category': 'visible_text', 'fact': 'STOP on sign', 'source_quote': 'STOP'},
        {'id': 'time', 'category': 'time', 'fact': '09:30', 'source_quote': '09:30'},
    ],
    'visible_text': ['STOP', '09:30'],
}


def report(candidate=NEUTRAL):
    return {'anchor_checks': [{'id': a['id'], 'preserved': True, 'evidence': candidate} for a in ANCHORS['anchors']],
            'all_original_facts_preserved': True, 'no_added_facts': True, 'visible_text_exact': True,
            'no_extra_visible_text': True, 'rendering_style_preserved': True, 'reason': 'All anchors preserved.'}


def reply(value):
    return ApiResponse(json.dumps(value), '', {}, 0.0, 'text-request')


def text_client(values):
    client = Mock(config=SimpleNamespace(model='fake-text'))
    client.clone.return_value = client
    client.chat.side_effect = [value if isinstance(value, BaseException) else reply(value) for value in values]
    return client


def recovery(*, author_values=None, validator_values=None, max_rewrites=1):
    author = text_client(author_values if author_values is not None else [{'prompt': NEUTRAL}])
    validator = text_client(validator_values if validator_values is not None else [ANCHORS, report()])
    return ModerationRecovery(ImageModerationRetryConfig(max_rewrites=max_rewrites), author=author,
                              validator=validator, logger=logging.getLogger('moderation-test'))


class Images:
    def __init__(self, outcomes):
        self.config = QwenImageConfig(api_key='test', model='fake-image', size='8*8')
        self.request_ids = []
        self.prompts = []
        self.outcomes = iter(outcomes)

    def clone(self):
        return self

    def generate(self, prompt):
        self.prompts.append(prompt)
        self.request_ids = [f'image-{len(self.prompts)}']
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        buffer = BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(buffer, format='PNG')
        return buffer.getvalue()


def blocked():
    return ImageModerationError('DataInspectionFailed: test rejection')


class ImageErrorClassificationTest(unittest.TestCase):
    @staticmethod
    def response(status, body):
        value = Mock(status_code=status, headers={})
        value.json.return_value = body
        value.text = json.dumps(body)
        return value

    def test_moderation_formats_and_rejected_request_ids(self):
        cases = [(400, {'code': 'DataInspectionFailed'}), (403, {'code': 'DataInspectionFailed'}),
                 (200, {'error': {'type': 'content_filter', 'message': 'blocked'}}),
                 (200, {'error': {'message': 'blocked by content policy'}}),
                 (400, {'code': 'ContentFilter'}), (400, {'message': '内容审核拒绝'})]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                body['request_id'] = 'rejected-id'
                session = Mock()
                session.request.return_value = self.response(status, body)
                client = QwenImageClient(QwenImageConfig(api_key='test'), session=session)
                with self.assertRaises(ImageModerationError):
                    client.generate(ORIGINAL)
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(client.request_ids, ['rejected-id'])

    def test_authentication_is_still_fatal(self):
        for status, code in [(401, 'DataInspectionFailed'), (402, 'billing'), (403, 'Forbidden'),
                             (403, 'NetworkPolicyDenied'), (400, 'Invalid_API_Key')]:
            with self.subTest(status=status, code=code):
                session = Mock()
                session.request.return_value = self.response(status, {'code': code})
                client = QwenImageClient(QwenImageConfig(api_key='test'), session=session)
                with self.assertRaises(FatalImageApiError):
                    client.generate(ORIGINAL)

    def test_async_rejection_records_both_requests(self):
        session = Mock()
        session.request.side_effect = [
            self.response(200, {'request_id': 'submit', 'output': {'task_id': 'task'}}),
            self.response(200, {'request_id': 'poll', 'output': {'task_status': 'FAILED', 'code': 'OutputDataInspectionFailed', 'message': 'moderation rejected'}}),
        ]
        client = QwenImageClient(QwenImageConfig(api_key='test'), session=session)
        with self.assertRaisesRegex(ImageModerationError, 'moderation rejected'):
            client.generate(ORIGINAL)
        self.assertEqual(client.request_ids, ['submit', 'poll'])

    def test_rate_limit_still_uses_transport_retry(self):
        session = Mock()
        session.request.side_effect = [self.response(429, {'code': 'Throttling'}),
                                       self.response(200, {'output': {'b64_json': 'b2s='}})]
        client = QwenImageClient(QwenImageConfig(api_key='test'), session=session)
        with patch('value_eval.image_generation.client.time.sleep') as sleep:
            self.assertEqual(client.generate(ORIGINAL), b'ok')
        self.assertEqual(session.request.call_count, 2)
        sleep.assert_called_once_with(10.0)


class AnchorValidationTest(unittest.TestCase):
    def test_technical_name_of_same_object_is_allowed(self):
        candidate = NEUTRAL.replace('steel knife', 'steel kitchen cutting tool with the same knife-shaped blade')
        validate_anchors(ANCHORS, ORIGINAL)
        validate_candidate(ORIGINAL, candidate, ANCHORS, report(candidate))

    def test_changed_text_time_and_unsupported_reports_fail(self):
        for candidate in [NEUTRAL.replace('STOP', 'GO'), NEUTRAL.replace('STOP', 'STOPPED'),
                          NEUTRAL.replace('09:30', '10:30'), ORIGINAL]:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_candidate(ORIGINAL, candidate, ANCHORS, report(candidate))
        reports = []
        missing = report(); missing['anchor_checks'].pop(); reports.append(missing)
        duplicate = report(); duplicate['anchor_checks'][-1] = duplicate['anchor_checks'][0]; reports.append(duplicate)
        unsupported = report(); unsupported['anchor_checks'][0]['evidence'] = 'invented evidence'; reports.append(unsupported)
        added_text = report(); added_text['no_extra_visible_text'] = False; reports.append(added_text)
        changed_relation = report(); changed_relation['all_original_facts_preserved'] = False; reports.append(changed_relation)
        for value in reports:
            with self.subTest(report=value), self.assertRaises(ValueError):
                validate_candidate(ORIGINAL, NEUTRAL, ANCHORS, value)

    def test_anchor_extraction_cannot_invent_source_evidence(self):
        value = copy.deepcopy(ANCHORS)
        value['anchors'][0]['source_quote'] = 'invented person'
        with self.assertRaises(ValueError):
            validate_anchors(value, ORIGINAL)


class ModerationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(PipelineConfig.load(Path('configs/prepared_scenarios.yaml')),
                              output_root=Path(self.temp.name), run_id='moderation-test', image_backend='api',
                              image={'size': '8*8', 'concurrency': 1})
        self.rows = [{'benchmark_id': bid, 'profile': 'hh', 'image_description': ORIGINAL, 'shared_image_id': 'pair'}
                     for bid in ('awareness', 'instruction')]
        atomic_write_json(self.config.run_root / 'benchmark/hh/benchmark.json', {'items': self.rows})

    def runner(self, images, engine=None):
        runner = ImageGenerator(self.config, client=images)
        runner.moderation_recovery = engine or recovery()
        return runner

    def saved(self):
        return load_json(self.config.run_root / 'images/hh/manifest.json')['tasks'][0]

    def test_success_does_not_call_text_model(self):
        images, engine = Images([True]), recovery()
        self.runner(images, engine).run()
        engine.author.chat.assert_not_called()
        engine.validator.chat.assert_not_called()
        self.assertEqual(self.saved()['effective_prompt'], ORIGINAL)

    def test_neutralized_success_keeps_source_shared_pair_and_reuses_image(self):
        images, engine = Images([blocked(), True]), recovery()
        self.runner(images, engine).run()
        row = self.saved()
        self.assertEqual(row['status'], 'generated')
        self.assertEqual(row['prompt'], ORIGINAL)
        self.assertEqual(row['effective_prompt'], NEUTRAL)
        self.assertEqual(row['benchmark_ids'], ['awareness', 'instruction'])
        self.assertEqual(row['request_ids'], ['image-1', 'image-2'])
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(engine.author.chat.call_count, 1)
        self.assertEqual(engine.validator.chat.call_count, 2)
        self.runner(images, engine).run()
        self.assertEqual(self.saved()['status'], 'reused')
        self.assertEqual(len(images.prompts), 2)

    def test_single_rewrite_is_bounded_across_manual_and_incremental_restarts(self):
        images, engine = Images([blocked(), blocked()]), recovery()
        self.runner(images, engine).run()
        row = self.saved()
        self.assertEqual(row['status'], 'moderated')
        self.assertTrue(row['moderation_retry']['exhausted'])
        self.assertEqual(images.prompts, [ORIGINAL, NEUTRAL])
        self.runner(images, engine).run()
        incremental = self.runner(images, engine)
        incremental.submit_items('hh', self.rows)
        incremental.finish_incremental()
        self.assertEqual(len(images.prompts), 2)
        self.assertEqual(engine.author.chat.call_count, 1)

    def test_force_resets_exhausted_budget(self):
        images = Images([blocked(), blocked(), True])
        self.runner(images).run()
        self.runner(images).run(force=True)
        self.assertEqual(self.saved()['status'], 'generated')
        self.assertEqual(self.saved()['attempts'], 1)
        self.assertEqual(len(images.prompts), 3)

    def test_semantic_failure_is_not_sent_to_image_api(self):
        spoon = NEUTRAL.replace('knife', 'spoon')
        rejection = report(spoon)
        rejection['anchor_checks'][3]['preserved'] = False
        engine = recovery(author_values=[{'prompt': spoon}], validator_values=[ANCHORS, rejection])
        images = Images([blocked()])
        self.runner(images, engine).run()
        row = self.saved()
        self.assertEqual(row['status'], 'moderated')
        self.assertEqual(row['moderation_retry']['rewrites'][0]['status'], 'validation_failed')
        self.assertNotIn(spoon, images.prompts)
        self.assertEqual(len(images.prompts), 1)

    def test_disabled_retry_does_not_call_text_model(self):
        config = replace(self.config, image={**self.config.image, 'moderation_retry': {'enabled': False}})
        images = Images([blocked()])
        runner = ImageGenerator(config, client=images)
        self.assertIsNone(runner.moderation_recovery)
        runner.run()
        self.assertEqual(self.saved()['status'], 'moderated')
        self.assertEqual(len(images.prompts), 1)

    def test_ready_rewrite_resumes_without_rewriting_or_resending_original(self):
        checkpoints = []
        task = ImageTask('test', ORIGINAL, [], 'hh')
        images, engine = Images([blocked(), True]), recovery()
        engine.generate(images, task, lambda: checkpoints.append(copy.deepcopy(task.as_dict())))
        ready = next(row for row in checkpoints if row['moderation_retry']['rewrites']
                     and row['moderation_retry']['rewrites'][0]['status'] == 'ready')
        restored = ImageTask(**ready)
        resumed_images, resumed_engine = Images([True]), recovery(author_values=[], validator_values=[])
        resumed_engine.generate(resumed_images, restored, lambda: None)
        self.assertEqual(resumed_images.prompts, [NEUTRAL])
        resumed_engine.author.chat.assert_not_called()
        resumed_engine.validator.chat.assert_not_called()

    def test_interrupted_rewrite_consumes_its_slot(self):
        task = ImageTask('test', ORIGINAL, [], 'hh', moderation_retry={
            'version': 1, 'original': {'status': 'moderated', 'prompt': ORIGINAL}, 'anchors': ANCHORS,
            'rewrites': [{'stage': 'neutralize', 'status': 'generating', 'prompt': NEUTRAL}],
        })
        engine = recovery(author_values=[], validator_values=[])
        images = Images([])
        with self.assertRaises(ImageModerationError):
            engine.generate(images, task, lambda: None)
        self.assertEqual(task.moderation_retry['rewrites'][0]['status'], 'interrupted')
        self.assertEqual(images.prompts, [])
        engine.author.chat.assert_not_called()
        engine.validator.chat.assert_not_called()

    def test_legacy_presentation_attempts_are_not_resumed(self):
        presentation = {'stage': 'presentation', 'status': 'ready', 'prompt': 'A flat vector illustration.'}
        histories = [[presentation],
                     [{'stage': 'neutralize', 'status': 'moderated', 'prompt': NEUTRAL}, presentation]]
        for history in histories:
            with self.subTest(history=history):
                task = ImageTask('test', ORIGINAL, [], 'hh', moderation_retry={
                    'version': 1, 'original': {'status': 'moderated', 'prompt': ORIGINAL},
                    'anchors': ANCHORS, 'rewrites': copy.deepcopy(history),
                })
                engine = recovery(author_values=[], validator_values=[])
                images = Images([])
                with self.assertRaises(ImageModerationError):
                    engine.generate(images, task, lambda: None)
                self.assertEqual(images.prompts, [])
                engine.author.chat.assert_not_called()
                engine.validator.chat.assert_not_called()

    def test_legacy_moderated_manifest_uses_recovery_without_resending_original(self):
        task = ImageTask('test', ORIGINAL, [], 'hh', status='moderated', error='old rejection')
        images = Images([True])
        recovery().generate(images, task, lambda: None)
        self.assertEqual(images.prompts, [NEUTRAL])

    def test_extraction_failure_is_recorded_and_not_retried_forever(self):
        engine = recovery(validator_values=[{'anchors': []}])
        images = Images([blocked()])
        self.runner(images, engine).run()
        self.runner(images, engine).run()
        self.assertEqual(self.saved()['status'], 'moderated')
        self.assertEqual(engine.validator.chat.call_count, 1)

    def test_text_auth_failure_stops_stage(self):
        engine = recovery(validator_values=[FatalModelError('bad key')])
        with self.assertRaisesRegex(FatalImageApiError, 'text model unavailable'):
            self.runner(Images([blocked()]), engine).run()
        self.assertEqual(self.saved()['status'], 'aborted')

    def test_missing_text_key_can_resume_after_configuration_is_fixed(self):
        images = Images([blocked(), True])
        engine = recovery(validator_values=[RuntimeError('missing API key environment variable: AUTHOR_KEY')])
        with self.assertRaisesRegex(FatalImageApiError, 'AUTHOR_KEY'):
            self.runner(images, engine).run()
        self.runner(images).run()
        self.assertEqual(self.saved()['status'], 'generated')
        self.assertEqual(images.prompts, [ORIGINAL, NEUTRAL])

    def test_multiple_workers_checkpoint_complete_histories(self):
        rows = [{**self.rows[0], 'benchmark_id': str(i), 'shared_image_id': str(i)} for i in range(8)]
        atomic_write_json(self.config.run_root / 'benchmark/hh/benchmark.json', {'items': rows})
        for incremental in (False, True):
            with self.subTest(incremental=incremental):
                config = replace(self.config, image={**self.config.image, 'concurrency': 4})
                backend = Mock(config=QwenImageConfig(api_key='test', model='fake-image', size='8*8'))
                backend.clone.side_effect = lambda: Images([blocked(), True])
                engine = recovery()
                engine.author.chat.side_effect = lambda *args, **kwargs: reply({'prompt': NEUTRAL})
                engine.validator.chat.side_effect = lambda messages, **kwargs: reply(
                    ANCHORS if messages[0]['content'].startswith('Extract immutable') else report())
                runner = ImageGenerator(config, client=backend, incremental_force=True)
                runner.moderation_recovery = engine
                if incremental:
                    runner.submit_items('hh', rows)
                    runner.finish_incremental()
                else:
                    runner.run(force=True)
                manifest = load_json(config.run_root / 'images/hh/manifest.json')
                self.assertEqual(manifest['completed_count'], 8)
                for task in manifest['tasks']:
                    self.assertEqual(task['attempts'], 2)
                    self.assertEqual(task['moderation_retry']['original']['status'], 'moderated')
                    self.assertEqual(task['moderation_retry']['rewrites'][0]['status'], 'generated')

    def test_rendering_style_change_is_not_sent_to_image_api(self):
        candidate = NEUTRAL.replace('Photographic style.', 'Flat vector illustration.')
        rejection = report(candidate)
        rejection['rendering_style_preserved'] = False
        engine = recovery(author_values=[{'prompt': candidate}], validator_values=[ANCHORS, rejection])
        images = Images([blocked()])
        self.runner(images, engine).run()
        self.assertEqual(self.saved()['status'], 'moderated')
        self.assertEqual(images.prompts, [ORIGINAL])
        self.assertIn('rendering_style_preserved', self.saved()['moderation_retry']['rewrites'][0]['error'])

    def test_bad_configuration_is_rejected(self):
        for value in ({'max_rewrites': -1}, {'max_rewrites': 2}, {'max_rewrites': True},
                      {'enabled': 'true'}, {'presentation': 'flat_vector'}, {'typo': 1}):
            with self.subTest(config=value), self.assertRaises(ValueError):
                ImageModerationRetryConfig.from_mapping(value, 'author')


if __name__ == '__main__':
    unittest.main()
