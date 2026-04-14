#!/usr/bin/env python3
"""
Fake CloudWatch log event generator.
Writes real CloudWatch Logs entries via boto3 so downstream consumers
(like a music program querying CloudWatch) see live traffic.

Requires: pip install boto3
AWS credentials must be configured (env vars, ~/.aws/credentials, or IAM role).

Usage:
    python cloudwatch_event_generator.py [region]

Examples:
    python cloudwatch_event_generator.py              # uses default region
    python cloudwatch_event_generator.py us-west-2    # explicit region

Stop with Ctrl+C.
"""

import time
import random
import uuid
import math
import sys
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Services and their log patterns
# ---------------------------------------------------------------------------

SERVICES = {
    "lambda": {
        "log_group": "/aws/lambda/{function}",
        "functions": ["music-converter", "audio-mixer", "beat-detector", "waveform-gen", "playlist-builder"],
        "messages": [
            "START RequestId: {req_id} Version: $LATEST",
            "END RequestId: {req_id}",
            "REPORT RequestId: {req_id} Duration: {duration:.2f} ms Billed Duration: {billed} ms Memory Size: {mem_size} MB Max Memory Used: {mem_used} MB",
            "INFO Processing event from {source}",
            "WARN Cold start detected, initialization took {duration:.0f}ms",
            "ERROR Task timed out after {timeout} seconds",
            "INFO Successfully processed {count} records",
        ],
    },
    "ecs": {
        "log_group": "/ecs/{cluster}/{service}",
        "clusters": ["prod-cluster", "staging-cluster"],
        "services": ["api-gateway", "worker-service", "scheduler"],
        "messages": [
            "INFO  [main] Starting container health check",
            "INFO  [http] GET /health 200 {duration:.0f}ms",
            "INFO  [http] POST /api/v1/tracks 201 {duration:.0f}ms",
            "WARN  [pool] Connection pool near capacity: {count}/100",
            "ERROR [http] POST /api/v1/upload 500 Internal Server Error",
            "INFO  [worker] Dequeued message {req_id} from SQS",
            "DEBUG [cache] Cache hit ratio: {ratio:.1%}",
        ],
    },
    "rds": {
        "log_group": "/aws/rds/instance/{instance}/postgresql",
        "instances": ["music-db-primary", "music-db-replica", "analytics-db"],
        "messages": [
            "LOG:  checkpoint starting: time",
            "LOG:  checkpoint complete: wrote {count} buffers ({ratio:.1%})",
            "LOG:  duration: {duration:.3f} ms  statement: SELECT * FROM tracks WHERE artist_id = {count}",
            "WARNING:  worker process (PID {pid}) was terminated by signal 9",
            "LOG:  autovacuum: processing database \"musicdb\"",
            "LOG:  connection received: host={ip} port={port}",
            "LOG:  disconnection: session time: {duration:.3f} s",
        ],
    },
    "apigateway": {
        "log_group": "/aws/apigateway/music-api",
        "messages": [
            "({req_id}) Method request path: /v1/tracks",
            "({req_id}) Method request path: /v1/playlists/{count}",
            "({req_id}) Method response status: 200",
            "({req_id}) Method response status: 429 - Rate limit exceeded",
            "({req_id}) Method response status: 504 - Gateway timeout",
            "({req_id}) Integration latency: {duration:.0f} ms",
            "({req_id}) API Key authorized: key=ak-****{port}",
        ],
    },
    "s3": {
        "log_group": "/aws/s3/access-logs",
        "buckets": ["music-uploads", "processed-audio", "album-art", "backup-archive"],
        "messages": [
            "REST.PUT.OBJECT tracks/{req_id}.mp3 200 {duration:.0f}ms {size} bytes",
            "REST.GET.OBJECT tracks/{req_id}.wav 200 {duration:.0f}ms",
            "REST.HEAD.OBJECT metadata/{req_id}.json 200 {duration:.0f}ms",
            "REST.DELETE.OBJECT temp/{req_id}.tmp 204 {duration:.0f}ms",
            "REST.GET.OBJECT covers/{req_id}.jpg 304 NotModified",
            "REST.PUT.OBJECT exports/{req_id}.zip 200 {duration:.0f}ms {size} bytes",
        ],
    },
    "cloudfront": {
        "log_group": "/aws/cloudfront/music-cdn",
        "messages": [
            "Hit {ip} GET /audio/{req_id}.mp3 200 {size}",
            "Miss {ip} GET /audio/{req_id}.mp3 200 {size}",
            "RefreshHit {ip} GET /images/{req_id}.webp 200 {size}",
            "Error {ip} GET /stream/{req_id} 503 ServiceUnavailable",
            "Hit {ip} GET /api/metadata/{count} 200 {size}",
            "LimitExceeded {ip} GET /download/{req_id}.flac 429 TooManyRequests",
        ],
    },
    "sqs": {
        "log_group": "/aws/sqs/music-queue",
        "queues": ["audio-processing", "notification", "transcode-jobs", "analytics-events"],
        "messages": [
            "SendMessage queue={queue} MessageId={req_id} size={size}",
            "ReceiveMessage queue={queue} count={count} wait=20s",
            "DeleteMessage queue={queue} MessageId={req_id}",
            "ApproximateNumberOfMessages queue={queue} count={count}",
            "SendMessageBatch queue={queue} successful={count} failed=0",
            "MessageMovedToDLQ queue={queue}-dlq MessageId={req_id} reason=MaxReceiveCount",
        ],
    },
    "dynamodb": {
        "log_group": "/aws/dynamodb/music-tables",
        "tables": ["UserSessions", "TrackMetadata", "PlayHistory", "Recommendations"],
        "messages": [
            "GetItem table={table} consumed_rcu={count} latency={duration:.1f}ms",
            "PutItem table={table} consumed_wcu={count} latency={duration:.1f}ms",
            "Query table={table} items_returned={count} consumed_rcu={count} latency={duration:.1f}ms",
            "BatchGetItem table={table} unprocessed_keys=0 consumed_rcu={count}",
            "ThrottlingException table={table} ProvisionedThroughputExceededException",
            "UpdateItem table={table} consumed_wcu={count} latency={duration:.1f}ms",
        ],
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_ip():
    return f"{random.randint(10,192)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"


def resolve_log_group(cfg):
    """Return a concrete log group name from a service config."""
    tpl = cfg["log_group"]
    if "{function}" in tpl:
        return tpl.format(function=random.choice(cfg["functions"]))
    if "{cluster}" in tpl:
        return tpl.format(
            cluster=random.choice(cfg.get("clusters", ["cluster"])),
            service=random.choice(cfg.get("services", ["service"])),
        )
    if "{instance}" in tpl:
        return tpl.format(instance=random.choice(cfg["instances"]))
    return tpl


def build_message(cfg):
    """Build a single log message string."""
    template = random.choice(cfg["messages"])
    return template.format(
        req_id=str(uuid.uuid4()),
        duration=random.uniform(0.5, 3500),
        billed=random.choice([100, 200, 300, 500, 1000, 3000]),
        mem_size=random.choice([128, 256, 512, 1024, 2048]),
        mem_used=random.randint(40, 1800),
        timeout=random.choice([3, 5, 15, 30, 60, 900]),
        count=random.randint(1, 500),
        source=random.choice(["s3", "sqs", "api-gateway", "eventbridge", "kinesis"]),
        ratio=random.random(),
        pid=random.randint(1000, 65000),
        ip=random_ip(),
        port=random.randint(1024, 65535),
        size=random.randint(128, 52_428_800),
        queue=random.choice(cfg.get("queues", ["default"])),
        table=random.choice(cfg.get("tables", ["default"])),
        function=random.choice(cfg.get("functions", ["default"])),
    )


def get_current_rate(t):
    """
    Smoothly varying event rate between 1 and 10 events/sec.
    Layered sine waves for an organic feel.
    """
    rate = 1.5
    rate += 3.0 * math.sin(t / 11.0)        # slow swell   (~69s period)
    rate += 1.5 * math.sin(t / 4.7 + 1.0)   # medium wave  (~30s period)
    rate += 0.8 * math.sin(t / 1.3 + 2.5)   # fast ripple  (~8s period)
    return max(1.0, min(10.0, rate))


# ---------------------------------------------------------------------------
# CloudWatch Logs helpers
# ---------------------------------------------------------------------------

def enumerate_all_log_groups():
    """Return every concrete log group name the generator can produce."""
    groups = set()
    for cfg in SERVICES.values():
        tpl = cfg["log_group"]
        if "{function}" in tpl:
            for fn in cfg["functions"]:
                groups.add(tpl.format(function=fn))
        elif "{cluster}" in tpl:
            for cl in cfg.get("clusters", []):
                for sv in cfg.get("services", []):
                    groups.add(tpl.format(cluster=cl, service=sv))
        elif "{instance}" in tpl:
            for inst in cfg["instances"]:
                groups.add(tpl.format(instance=inst))
        else:
            groups.add(tpl)
    return sorted(groups)


def ensure_log_groups(client, groups):
    """Create log groups if they don't already exist."""
    for group in groups:
        try:
            client.create_log_group(logGroupName=group)
            print(f"  + Created  {group}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
                print(f"    Exists   {group}")
            else:
                raise


_known_streams: set = set()


def ensure_log_stream(client, log_group, stream_name):
    key = (log_group, stream_name)
    if key in _known_streams:
        return
    try:
        client.create_log_stream(
            logGroupName=log_group, logStreamName=stream_name
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    _known_streams.add(key)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    region = sys.argv[1] if len(sys.argv) > 1 else None
    client = (
        boto3.client("logs", region_name=region) if region
        else boto3.client("logs")
    )
    actual_region = client.meta.region_name

    print(f"CloudWatch Log Generator  (region: {actual_region})")
    print("=" * 56)

    # --- Create all log groups up front ---
    all_groups = enumerate_all_log_groups()
    print(f"\nEnsuring {len(all_groups)} log groups exist …")
    ensure_log_groups(client, all_groups)

    service_names = list(SERVICES.keys())
    weights = [3, 2, 1, 3, 2, 2, 2, 2]

    # We batch events by log group and flush every ~250 ms to reduce API calls
    BATCH_INTERVAL = 0.25
    batch: dict[str, list[tuple[str, int, str]]] = {}
    total_events = 0
    total_puts = 0

    print(f"\nStreaming events (1–10/sec).  Ctrl+C to stop.\n")
    t0 = time.monotonic()
    last_flush = t0
    last_status = t0

    def flush_batch():
        nonlocal total_puts
        for grp, events in batch.items():
            by_stream: dict[str, list[dict]] = {}
            for stream, ts, msg in events:
                by_stream.setdefault(stream, []).append(
                    {"timestamp": ts, "message": msg}
                )
            for stream, log_events in by_stream.items():
                ensure_log_stream(client, grp, stream)
                log_events.sort(key=lambda e: e["timestamp"])
                try:
                    client.put_log_events(
                        logGroupName=grp,
                        logStreamName=stream,
                        logEvents=log_events,
                    )
                    total_puts += 1
                except ClientError as e:
                    print(f"  WARN put_log_events: {e}", file=sys.stderr)
        batch.clear()

    try:
        while True:
            t = time.monotonic() - t0
            rate = get_current_rate(t)

            # --- Generate one event ---
            svc = random.choices(service_names, weights=weights, k=1)[0]
            cfg = SERVICES[svc]
            log_group = resolve_log_group(cfg)
            message = build_message(cfg)
            now = datetime.now(timezone.utc)
            stream = f"{now.strftime('%Y/%m/%d')}/gen-{uuid.uuid4().hex[:8]}"
            ts_ms = int(now.timestamp() * 1000)

            batch.setdefault(log_group, []).append((stream, ts_ms, message))
            total_events += 1

            # --- Flush if interval elapsed ---
            wall = time.monotonic()
            if wall - last_flush >= BATCH_INTERVAL:
                flush_batch()
                last_flush = wall

            # --- Status line every 5s ---
            if wall - last_status >= 5.0:
                print(
                    f"  [{t:7.1f}s]  events={total_events:>6}  "
                    f"puts={total_puts:>5}  rate={rate:.1f}/s",
                    flush=True,
                )
                last_status = wall

            # --- Sleep ---
            interval = 1.0 / rate
            jitter = random.uniform(-0.02, 0.02)
            time.sleep(max(0.02, interval + jitter))

    except KeyboardInterrupt:
        flush_batch()
        elapsed = time.monotonic() - t0
        print(
            f"\n{'=' * 56}\n"
            f"Stopped after {elapsed:.1f}s\n"
            f"  Total events written : {total_events}\n"
            f"  Total API put calls  : {total_puts}\n"
            f"  Log groups used      : {len(all_groups)}\n"
        )


if __name__ == "__main__":
    main()
