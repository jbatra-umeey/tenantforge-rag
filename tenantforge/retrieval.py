import json
import math
from collections import Counter

from sqlalchemy import select, text

from .db import chunks, documents
from .errors import Problem
from .providers import terms


def bm25(question, rows):
    query = set(terms(question))
    tokens = [terms(row["content"]) for row in rows]
    avg = sum(map(len, tokens)) / max(1, len(tokens))
    df = Counter(t for ts in tokens for t in set(ts))
    scores = {}
    for row, ts in zip(rows, tokens):
        counts = Counter(ts)
        score = 0.0
        for term in query:
            frequency = counts[term]
            idf = math.log(1 + (len(rows) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * len(ts) / max(avg, 1)))
        scores[row["id"]] = score
    return scores


def hybrid(db, principal, question, vector, filters, candidate_limit=30):
    tenant = principal.tenant
    if db.pg:
        params = {"tenant": tenant, "query": question, "vector": json.dumps(vector),
                  "filters": json.dumps(filters), "limit": candidate_limit}
        base = ("SELECT c.id, c.document_id, c.content, c.locator, d.name, d.metadata "
                "FROM chunks c JOIN documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id "
                "WHERE c.tenant_id=:tenant AND d.status='ready' AND d.metadata @> CAST(:filters AS jsonb) ")
        with db.tx(tenant, principal.user) as c:
            c.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
            c.execute(text("SET LOCAL hnsw.ef_search = 100"))
            dense = [dict(x) for x in c.execute(text(base +
                "ORDER BY c.vector <=> CAST(:vector AS vector) LIMIT :limit"), params).mappings()]
            lexical = [dict(x) for x in c.execute(text(base +
                "AND c.search_vector @@ plainto_tsquery('english', :query) "
                "ORDER BY ts_rank_cd(c.search_vector, plainto_tsquery('english', :query)) DESC LIMIT :limit"), params).mappings()]
    else:
        with db.tx(tenant, principal.user) as c:
            stmt = select(chunks, documents.c.name, documents.c.metadata).join(documents,
                (documents.c.id == chunks.c.document_id) & (documents.c.tenant_id == chunks.c.tenant_id))
            rows = [dict(x) for x in c.execute(stmt.where(chunks.c.tenant_id == tenant,
                documents.c.status == "ready").limit(db.settings.sqlite_max_chunks+1)).mappings()]
        if len(rows) > db.settings.sqlite_max_chunks:
            raise Problem(503, "Local corpus limit reached; use PostgreSQL")
        rows = [x for x in rows if all(x["metadata"].get(k) == v for k, v in filters.items())]
        lexical_scores = bm25(question, rows)
        for row in rows:
            row["dense_score"] = sum(a*b for a, b in zip(vector, row["embedding"], strict=True))
        dense = sorted(rows, key=lambda x: (-x["dense_score"], x["id"]))[:candidate_limit]
        lexical = sorted([x for x in rows if lexical_scores[x["id"]] > 0],
                         key=lambda x: (-lexical_scores[x["id"]], x["id"]))[:candidate_limit]
    fused = {}
    for ranking in (dense, lexical):
        for position, row in enumerate(ranking, 1):
            item = fused.setdefault(row["id"], {**row, "rrf_score": 0.0})
            item["rrf_score"] += 1 / (60 + position)
    return sorted(fused.values(), key=lambda x: (-x["rrf_score"], x["id"]))[:candidate_limit]

