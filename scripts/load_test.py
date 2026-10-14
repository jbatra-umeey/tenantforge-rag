"""Opt-in HTTP load probe. Run against your authorized deployment, with a dedicated test tenant."""
import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter

import httpx


async def run(args):
    limiter = asyncio.Semaphore(args.concurrency)
    latencies, statuses = [], Counter()
    token = os.environ["TF_TEST_TOKEN"]
    async with httpx.AsyncClient(base_url=args.base_url,timeout=60,trust_env=False,
        limits=httpx.Limits(max_connections=args.concurrency),headers={"Authorization":f"Bearer {token}"}) as client:
        async def query(i):
            async with limiter:
                start = time.perf_counter()
                try:
                    question = "What is the annual leave policy?" + (f" Request {i}." if args.cold else "")
                    response = await client.post("/v1/query",json={"question":question})
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError:
                    statuses["transport_error"] += 1
                latencies.append((time.perf_counter()-start)*1000)
        start = time.perf_counter()
        await asyncio.gather(*(query(i) for i in range(args.requests)))
        duration = time.perf_counter()-start
    latencies.sort()
    def percentile(p):
        return round(latencies[max(0,math.ceil(p*len(latencies))-1)],2)
    print(json.dumps({"requests":args.requests,"concurrency":args.concurrency,"cold":args.cold,
        "duration_seconds":round(duration,2),"throughput_rps":round(args.requests/duration,2),
        "latency_ms_includes_errors":{"p50":percentile(.5),"p95":percentile(.95),"p99":percentile(.99)},
        "statuses":dict(statuses),"note":"Not a 100K-user capacity certification; inspect throttles/errors."},indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url",default="http://127.0.0.1:8000")
    parser.add_argument("--requests",type=int,default=20)
    parser.add_argument("--concurrency",type=int,default=4)
    parser.add_argument("--cold",action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 1000 or not 1 <= args.requests <= 100000:
        parser.error("concurrency 1..1000 and requests 1..100000 required")
    asyncio.run(run(args))
