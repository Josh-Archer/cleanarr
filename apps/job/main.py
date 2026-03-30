import os

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


def main() -> None:
    queue_summary = process_sqs_queue_messages(max_messages=_queue_max_messages_from_env(), force_deletions=True)
    if queue_summary.get('enabled'):
        print(f"Processed queued webhook events: {queue_summary}")
    elif queue_summary.get('queue_mode') == 'sqs':
        print(f"Queue mode is sqs but polling did not run: {queue_summary.get('reason')}")

    MediaCleanup().run()


if __name__ == "__main__":
    main()

