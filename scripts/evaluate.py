"""Deterministic regression evaluation; creates an isolated, temporary corpus."""
import json
import tempfile
import time
from pathlib import Path

from tenantforge.auth import Principal
from tenantforge.config import Settings
from tenantforge.db import Database
from tenantforge.providers import Providers
from tenantforge.service import Service
from tenantforge.storage import Storage
from tenantforge.worker import Worker


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as folder:
        s = Settings(database_url=f"sqlite:///{folder}/eval.db",storage_dir=f"{folder}/objects")
        db = Database(s)
        db.initialize()
        db.provision("evaluation")
        providers = Providers(s)
        svc = Service(db,Storage(s),providers)
        worker = Worker(db,svc.storage,providers)
        p = Principal("evaluation","evaluator","editor")
        for name,department in [("handbook.md","hr"),("runbook.html","engineering"),("products.csv","sales"),
                                ("travel-policy.pdf","hr"),("security-policy.docx","security")]:
            svc.ingest(p,name,(root/"examples"/name).read_bytes(),{"department":department})
        while worker.once():
            pass
        cases = json.loads((root/"evals/cases.json").read_text())
        rows = []
        for case in cases:
            start = time.perf_counter()
            result = svc.query(p,case["question"],filters=case["filters"])
            if case.get("abstain"):
                correct = result["abstained"] and not result["citations"]
                source_hit = True
            else:
                correct = case["contains"].lower() in result["answer"].lower()
                source_hit = any(x["name"] == case["source"] for x in result["citations"])
            rows.append({"question":case["question"],"answer_match":correct,"source_hit":source_hit,
                         "ms":round((time.perf_counter()-start)*1000,2)})
        report = {"profile":"SQLite + hash embeddings + extractive answers", "cases":len(rows),
            "answer_match":sum(x["answer_match"] for x in rows)/len(rows),
            "source_hit":sum(x["source_hit"] for x in rows)/len(rows), "results":rows,
            "scope":"Small synthetic regression set, not a semantic quality or production capacity benchmark"}
        print(json.dumps(report,indent=2))
        providers.close()
        db.engine.dispose()
        if not all(x["answer_match"] and x["source_hit"] for x in rows):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
