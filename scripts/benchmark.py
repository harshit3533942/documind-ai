"""Measure API response times (latency percentiles) against a running server.

Usage:
    python -m scripts.benchmark --email you@example.com --password ... [--base http://127.0.0.1:8000]

Reports, for /api/v1/chat/ask:
  * cold  - distinct questions (no answer-cache hits)
  * warm  - the same questions again (answer cache)
  * concurrent - N parallel clients (thread pool), to see queueing under load
and, for comparison, a cheap authenticated GET.

Why percentiles and not the average: the average hides the slow tail. p95 =
"95 % of requests were faster than this" - what most users actually feel.
Note: use 127.0.0.1, not localhost, on Windows (localhost tries IPv6 first
and adds ~200 ms of client-side connect time that is not the server's fault).
"""

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

QUESTIONS = [
    "How many vacation days do I get?", "What is the sick leave policy?", "How long is parental leave?",
    "Can I work from home?", "What are the core collaboration hours?", "What is the daily meal allowance?",
    "Which flight class is allowed?", "How fast must receipts be uploaded?", "How long must passwords be?",
    "What should I do if I lose my laptop?", "Do visitors need a badge?", "How often do passwords rotate?",
]


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def report(label: str, samples: list[float], server: list[float] | None = None, statuses: list[int] | None = None) -> None:
    line = (f"{label:<26} n={len(samples):<3} p50={pct(samples, 50):7.1f}ms  p95={pct(samples, 95):7.1f}ms  "
            f"p99={pct(samples, 99):7.1f}ms  max={max(samples):7.1f}ms  mean={statistics.mean(samples):7.1f}ms")
    if server:
        line += f"  | server p50={pct(server, 50):6.1f}ms"
    if statuses and any(s != 200 for s in statuses):
        line += f"  | non-200: {sorted({s for s in statuses if s != 200})}"
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=60) as client:
        token = client.post("/api/v1/auth/login", data={"username": args.email, "password": args.password}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        run_id = str(time.time_ns())[-6:]  # unique suffix -> genuinely uncached questions

        def ask(question: str) -> tuple[float, float, int]:
            started = time.perf_counter()
            r = client.post("/api/v1/chat/ask", headers=headers, json={"question": question})
            total = (time.perf_counter() - started) * 1000
            return total, float(r.headers.get("X-Response-Time-ms", 0)), r.status_code

        client.get("/api/v1/documents", headers=headers)  # warm the connection

        gets = []
        for _ in range(20):
            started = time.perf_counter()
            client.get("/api/v1/documents", headers=headers, params={"limit": 5})
            gets.append((time.perf_counter() - started) * 1000)
        report("GET /documents", gets)

        cold_q = [f"{q} (run {run_id})" for q in QUESTIONS]
        cold = [ask(q) for q in cold_q]
        report("ask: cold (uncached)", [c[0] for c in cold], [c[1] for c in cold], [c[2] for c in cold])
        warm = [ask(q) for q in cold_q]
        report("ask: warm (answer cache)", [w[0] for w in warm], [w[1] for w in warm], [w[2] for w in warm])

        # One HTTP client per thread: like separate users (and httpx clients are
        # not meant to be shared across threads).
        def ask_own_client(question: str) -> tuple[float, float, int]:
            with httpx.Client(base_url=args.base, timeout=60) as own:
                started = time.perf_counter()
                r = own.post("/api/v1/chat/ask", headers=headers, json={"question": question})
                return (time.perf_counter() - started) * 1000, float(r.headers.get("X-Response-Time-ms", 0)), r.status_code

        concurrent_q = [f"{q} (parallel {run_id})" for q in QUESTIONS]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(ask_own_client, concurrent_q))
        wall = time.perf_counter() - started
        report(f"ask: {args.concurrency} parallel clients", [r[0] for r in results], [r[1] for r in results], [r[2] for r in results])
        print(f"{'':<26} throughput ~ {len(results) / wall:.1f} requests/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
