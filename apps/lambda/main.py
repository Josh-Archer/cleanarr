import os
import json
import base64
from urllib.parse import urlencode
from loguru import logger
from cleanarr.webhook_app import APP, process_sqs_event_records


def _is_http_event(event):
    if not isinstance(event, dict):
        return False
    return any(key in event for key in ('body', 'rawPath', 'rawQueryString', 'requestContext', 'httpMethod'))


def _http_response_from_event(event):
    client = APP.test_client()
    headers = dict(event.get('headers') or {})

    body = event.get('body')
    if body is None:
        body_bytes = b''
    elif event.get('isBase64Encoded'):
        body_bytes = base64.b64decode(body)
    elif isinstance(body, bytes):
        body_bytes = body
    elif isinstance(body, str):
        body_bytes = body.encode('utf-8')
    else:
        body_bytes = json.dumps(body).encode('utf-8')

    query_string = event.get('rawQueryString') or ''
    if not query_string:
        query_params = event.get('queryStringParameters') or {}
        if isinstance(query_params, dict) and query_params:
            query_string = urlencode([(key, value) for key, value in query_params.items() if value is not None], doseq=True)

    request_context = event.get('requestContext') or {}
    http_context = request_context.get('http') or {}
    method = (http_context.get('method') or event.get('httpMethod') or 'POST').upper()
    path = event.get('rawPath') or event.get('path') or '/plex/webhook'

    response = client.open(
        path=path,
        method=method,
        headers=headers,
        query_string=query_string,
        data=body_bytes,
        content_type=headers.get('content-type') or headers.get('Content-Type'),
    )
    return {
        'statusCode': response.status_code,
        'headers': dict(response.headers),
        'body': response.get_data(as_text=True),
        'isBase64Encoded': False,
    }

def lambda_handler(event, context):
    try:
        logger.info("Starting Lambda execution for Cleanarr queued webhook processing"); logger.info(f"Event keys: {list(event.keys()) if isinstance(event, dict) else event}")
        records = event.get("Records") if isinstance(event, dict) else None
        if records:
            queue_summary = process_sqs_event_records(records, force_deletions=True)
            failed_message_ids = queue_summary.get("failed_message_ids") or []
            if failed_message_ids:
                logger.warning(f"Queued webhook processing reported failures: {queue_summary}")
            else:
                logger.info(f"Processed queued webhook events: {queue_summary}")
            # SQS event source mappings expect per-record failures so only the
            # unsuccessful messages are retried.
            return {
                "batchItemFailures": [
                {"itemIdentifier": message_id}
                for message_id in failed_message_ids
            ]
        }

        if _is_http_event(event):
            logger.info("Received direct webhook invocation; dispatching through webhook app")
            return _http_response_from_event(event)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No webhook queue event payload",
            }),
        }
    except SystemExit as e:
        logger.exception(f"Handler attempted to exit process: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(f"SystemExit: {e}")
        }
    except BaseException as e:
        logger.exception(f"Handler fatal error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(str(e))
        }

if __name__ == "__main__":
    lambda_handler(None, None)
