"""Post a signed progress observation from an explicitly configured executor."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--event-id", default=None, help="Keep this unchanged when retrying a report")
    parser.add_argument("--producer", default="custom-executor")
    args = parser.parse_args()
    secret = os.environ.get("EXECUTOR_WEBHOOK_SECRET")
    url = os.environ.get("EXECUTOR_REPORT_URL")
    if not secret or not url:
        raise SystemExit("Configure EXECUTOR_WEBHOOK_SECRET and EXECUTOR_REPORT_URL")
    event = {"version": 1, "event_id": args.event_id or uuid.uuid4().hex, "producer": args.producer, "repo": args.repo, "execution_id": args.execution_id, "step": args.step, "status": args.status, "sequence": args.sequence, "occurred_at": datetime.now(timezone.utc).isoformat(), "message": args.message, "result": {}}
    body = json.dumps(event, ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Executor-Signature-256": signature}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.read().decode())


if __name__ == "__main__":
    main()
