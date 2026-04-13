import json
import os
import sys
import unittest
from unittest.mock import patch

# Ensure we can import the local package
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import webhook_app


class _FakeSqsClient:
    def __init__(self, messages):
        self._messages = list(messages)
        self.deleted = []

    def receive_message(self, **kwargs):
        max_messages = kwargs.get('MaxNumberOfMessages', 1)
        batch = self._messages[:max_messages]
        self._messages = self._messages[max_messages:]
        if not batch:
            return {}
        return {'Messages': batch}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append((QueueUrl, ReceiptHandle))


class TestWebhookQueueMode(unittest.TestCase):
    def setUp(self):
        self.client = webhook_app.APP.test_client()

    def test_webhook_enqueues_when_queue_mode_enabled(self):
        payload = {
            'event': 'media.scrobble',
            'Metadata': {'guid': 'plex://movie/123', 'ratingKey': '123'},
            'Account': {'id': 1, 'title': 'alice'},
        }

        with patch.object(webhook_app, 'WEBHOOK_SECRET', None), \
             patch.object(webhook_app, '_start_background_threads'), \
             patch.object(webhook_app, '_queue_enqueuing_enabled', return_value=True), \
             patch.object(webhook_app, '_enqueue_webhook_event', return_value=True), \
             patch.object(webhook_app, '_process_webhook_event_actions') as process_actions:
            response = self.client.post('/plex/webhook', json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['queued'])
        self.assertTrue(data['recorded'])
        process_actions.assert_not_called()

    def test_webhook_falls_back_to_direct_when_enqueue_fails(self):
        payload = {
            'event': 'media.scrobble',
            'Metadata': {'guid': 'plex://movie/456', 'ratingKey': '456'},
            'Account': {'id': 2, 'title': 'bob'},
        }

        with patch.object(webhook_app, 'WEBHOOK_SECRET', None), \
             patch.object(webhook_app, '_start_background_threads'), \
             patch.object(webhook_app, '_queue_enqueuing_enabled', return_value=True), \
             patch.object(webhook_app, '_enqueue_webhook_event', return_value=False), \
             patch.object(webhook_app, '_process_webhook_event_actions', return_value={'recorded': True}) as process_actions:
            response = self.client.post('/plex/webhook', json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['queued'])
        self.assertTrue(data['recorded'])
        process_actions.assert_called_once()

    def test_webhook_processes_directly_when_queue_mode_disabled(self):
        payload = {
            'event': 'media.scrobble',
            'Metadata': {'guid': 'plex://movie/789', 'ratingKey': '789'},
            'Account': {'id': 3, 'title': 'carol'},
        }

        with patch.object(webhook_app, 'WEBHOOK_SECRET', None), \
             patch.object(webhook_app, '_start_background_threads'), \
             patch.object(webhook_app, '_queue_enqueuing_enabled', return_value=False), \
             patch.object(webhook_app, '_enqueue_webhook_event') as enqueue_event, \
             patch.object(webhook_app, '_process_webhook_event_actions', return_value={'recorded': True}) as process_actions:
            response = self.client.post('/plex/webhook', json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['queued'])
        self.assertTrue(data['recorded'])
        enqueue_event.assert_not_called()
        process_actions.assert_called_once()

    def test_process_sqs_queue_messages_processes_and_deletes(self):
        fake_client = _FakeSqsClient([
            {
                'MessageId': 'msg-1',
                'ReceiptHandle': 'rh-1',
                'Body': json.dumps({'event': 'media.scrobble', 'metadata': {'guid': 'plex://movie/1'}}),
            }
        ])

        with patch.object(webhook_app, '_queue_polling_enabled', return_value=True), \
             patch.object(webhook_app, '_get_sqs_client', return_value=fake_client), \
             patch.object(webhook_app, 'WEBHOOK_QUEUE_URL', 'https://example.com/queue/cleanarr'), \
             patch.object(webhook_app, '_process_webhook_event_actions') as process_actions:
            summary = webhook_app.process_sqs_queue_messages(max_messages=1, force_deletions=True)

        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['received'], 1)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['deleted'], 1)
        self.assertEqual(summary['failed'], 0)
        process_actions.assert_called_once()

        args, kwargs = process_actions.call_args
        self.assertEqual(kwargs.get('async_mode'), False)
        self.assertEqual(kwargs.get('force_deletions'), True)
        self.assertEqual(args[0]['queue_message_id'], 'msg-1')

    def test_process_sqs_queue_messages_noop_when_polling_disabled(self):
        with patch.object(webhook_app, '_queue_polling_enabled', return_value=False):
            summary = webhook_app.process_sqs_queue_messages(max_messages=5)

        self.assertFalse(summary['enabled'])
        self.assertEqual(summary['reason'], 'queue polling disabled')

    def test_process_sqs_event_records_processes_delivered_records(self):
        records = [
            {
                'messageId': 'msg-2',
                'MessageId': 'msg-2',
                'body': json.dumps({'event': 'media.stop', 'metadata': {'guid': 'plex://episode/2'}}),
                'Body': json.dumps({'event': 'media.stop', 'metadata': {'guid': 'plex://episode/2'}}),
            }
        ]

        with patch.object(webhook_app, '_process_webhook_event_actions') as process_actions:
            summary = webhook_app.process_sqs_event_records(records, force_deletions=True)

        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['received'], 1)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['deleted'], 0)
        self.assertEqual(summary['failed'], 0)
        self.assertEqual(summary['failed_message_ids'], [])
        process_actions.assert_called_once()

        args, kwargs = process_actions.call_args
        self.assertEqual(kwargs.get('async_mode'), False)
        self.assertEqual(kwargs.get('force_deletions'), True)
        self.assertEqual(args[0]['queue_message_id'], 'msg-2')

    def test_process_sqs_event_records_tracks_failed_message_ids(self):
        records = [
            {
                'messageId': 'msg-3',
                'MessageId': 'msg-3',
                'body': json.dumps({'event': 'media.scrobble'}),
                'Body': json.dumps({'event': 'media.scrobble'}),
            }
        ]

        with patch.object(webhook_app, '_process_webhook_event_actions', side_effect=RuntimeError('boom')):
            summary = webhook_app.process_sqs_event_records(records, force_deletions=True)

        self.assertEqual(summary['received'], 1)
        self.assertEqual(summary['processed'], 0)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['failed_message_ids'], ['msg-3'])


if __name__ == '__main__':
    unittest.main()
