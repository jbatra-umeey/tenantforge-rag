"""Exercise a running local API AND its separate worker. No paid provider calls."""
import argparse
import json
import os
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url",default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ["TF_TEST_TOKEN"]
    root = Path(__file__).resolve().parents[1]
    with httpx.Client(base_url=args.base_url,headers={"Authorization":f"Bearer {token}"},timeout=60,trust_env=False) as c:
        c.get("/readyz").raise_for_status()
        for name in ["handbook.md","runbook.html","products.csv","travel-policy.pdf","security-policy.docx"]:
            r = c.post("/v1/documents",files={"file":(name,(root/"examples"/name).read_bytes())})
            r.raise_for_status()
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            rows = c.get("/v1/documents").json()["items"]
            if any(x["status"] == "failed" for x in rows):
                raise AssertionError("Worker ingestion failed")
            if len(rows) >= 5 and all(x["status"] == "ready" for x in rows):
                break
            time.sleep(.5)
        else:
            raise AssertionError("Ingestion timed out; is the worker running?")
        result = c.post("/v1/query",json={"question":"How many annual leave days do employees receive?"})
        result.raise_for_status()
        answer = result.json()
        assert "25" in answer["answer"] and answer["citations"]
        for citation in answer["citations"]:
            c.get(citation["url"]).raise_for_status()
        memory = c.get(f"/v1/conversations/{answer['conversation_id']}")
        assert len(memory.json()["messages"]) == 2
        print(json.dumps({"status":"passed","ready_documents":len(rows),"answer":answer["answer"],
                          "citation_count":len(answer["citations"]),"latency_ms":answer["latency_ms"]},indent=2))


if __name__ == "__main__":
    main()
