import json
import os
import sys


def lambda_handler(event, context):
    try:
        from cleanarr.cleanup import MediaCleanup

        if isinstance(event, dict) and event.get("Records"):
            print(
                "SQS event payload received by scheduled runtime; "
                "webhook queue messages are consumed by apps/lambda/main only."
            )

        cleaner = MediaCleanup()
        result = cleaner.run()
        if result.failed:
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {
                        "outcome": result.outcome,
                        "failed": True,
                        "errors": result.errors,
                        "message": result.message
                        or f"Cleanup finished with outcome={result.outcome}",
                    }
                ),
            }
        return {
            "statusCode": 200,
            "body": "Cleanup executed successfully.",
        }
    except Exception as e:
        print(f"Error during cleanup execution: {e}", file=sys.stderr)
        return {
            "statusCode": 500,
            "body": str(e),
        }
