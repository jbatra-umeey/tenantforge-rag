"""Reproducible vector overview; no external graphics dependencies."""
from html import escape
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="1050" viewBox="0 0 1400 1050">',
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#68857c"/></marker></defs>',
    '<rect width="1400" height="1050" rx="24" fill="#f3f6f0"/>']


def text(x,y,value,size=16,color="#173d37",weight=400):
    parts.append(f'<text x="{x}" y="{y}" font-family="Arial,sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(value)}</text>')


def box(x,y,w,h,title,lines,fill="#ffffff"):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" stroke="#ccdacb"/>')
    text(x+22,y+34,title,19,weight=700)
    for i,line in enumerate(lines):
        text(x+22,y+62+i*23,line,15,"#61776c")


def arrow(d):
    parts.append(f'<path d="{d}" fill="none" stroke="#68857c" stroke-width="2.2" marker-end="url(#arrow)"/>')


text(52,55,"TENANTFORGE / SYSTEM ARCHITECTURE",13,"#66826c",700)
text(52,103,"One tenant boundary. Two independent paths.",34,weight=700)
text(52,136,"Runnable local pipeline + PostgreSQL deployment adapter. Capacity must be measured.",17,"#61776c")
box(440,178,520,100,"Browser and API clients",["Bearer token · file upload · question · source inspection"],"#dfedce")
arrow("M700 278V311")
box(390,312,620,110,"API / authorization boundary",["Verify tenant + subject + role from signed identity", "Bound input · admit work · reserve tenant budgets"],"#e5efe0")
arrow("M440 422V452H250V478")
arrow("M960 422V452H1120V478")
text(62,469,"ASYNCHRONOUS INGESTION",12,"#66826c",700)
text(912,469,"SYNCHRONOUS QUERIES",12,"#66826c",700)
box(60,480,380,112,"Source storage + SQL job",["Opaque tenant-prefixed raw objects", "Metadata and queued work commit together"])
box(910,480,430,112,"Private memory + answer cache",["Tenant + user + history + corpus version", "No cross-user conversation reuse"])
arrow("M250 592V621")
arrow("M1120 592V621")
box(60,623,380,135,"Independent ingestion workers",["Lease + fence · retries · dead letters", "Bounded PDF / DOCX / HTML / CSV parse", "Sentence chunks + batched embeddings"])
box(910,623,430,135,"Hybrid retrieval + reranking",["Tenant and metadata filters", "Dense vectors + lexical candidates + RRF", "Bounded evidence context"])
box(475,589,400,165,"Shared SQL persistence",["Tenant metadata · chunks · vectors", "Jobs · budgets · private conversations", "SQLite exact search for local mode", "PostgreSQL RLS + pgvector for deployment"],"#e5efe0")
arrow("M440 690H473")
arrow("M909 690H878")
arrow("M1120 758V791")
box(910,793,430,116,"Grounded answer + citations",["Extractive or configured model generation", "Validate citation IDs and corpus revision"])
box(60,810,815,99,"Cross-cutting controls",["Scoped storage and caches · role checks · audit events · metrics and OTLP spans", "Production gates: real IdP/S3/PG tests, model quality, load, restore and security review"],"#dfedce")
text(60,967,"Local default: hash embeddings + extractive answers. Neural embeddings and models are configurable adapters.",17,"#61776c")
text(60,1001,"100K concurrent users is a design target, not a benchmark claim.",18,weight=700)
parts.append('</svg>')
(root/"docs/architecture.svg").write_text('\n'.join(parts))
print("Wrote docs/architecture.svg")
