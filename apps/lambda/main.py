import os
import json
from loguru import logger
from cleanarr.cleanup import MediaCleanup
from cleanarr.webhook_app import process_sqs_queue_messages


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
        logger.info("Starting Lambda execution for Cleanarr cleanup")
        queue_summary = process_sqs_queue_messages(max_messages=_queue_max_messages_from_env(), force_deletions=True)
        if queue_summary.get('enabled'):
            logger.info(f"Processed queued webhook events: {queue_summary}")

        cleaner = MediaCleanup()
        cleaner.run()
        return {
            "statusCode": 200,
            "body": json.dumps("Cleanup successful")
        }
    except Exception as e:
        logger.error(f"Handler error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(str(e))
        }

if __name__ == "__main__":
    lambda_handler(None, None)
