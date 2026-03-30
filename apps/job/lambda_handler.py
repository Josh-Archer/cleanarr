import os
import sys


def _queue_max_messages_from_env():
    raw = os.environ.get('CLEANARR_WEBHOOK_QUEUE_MAX_MESSAGES')
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None

def lambda_handler(event, context):
    try:
        from cleanarr.cleanup import MediaCleanup
        from cleanarr.webhook_app import process_sqs_queue_messages

        queue_summary = process_sqs_queue_messages(max_messages=_queue_max_messages_from_env(), force_deletions=True)
        if queue_summary.get('enabled'):
            print(f"Processed queued webhook events: {queue_summary}")

        cleaner = MediaCleanup()
        cleaner.run()
        return {
            'statusCode': 200,
            'body': 'Cleanup executed successfully.'
        }
    except Exception as e:
        print(f"Error during cleanup execution: {e}", file=sys.stderr)
        return {
            'statusCode': 500,
            'body': str(e)
        }
