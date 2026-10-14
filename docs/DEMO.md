# Five walkthroughs

Run `python -m tenantforge demo`, connect using the printed token, and keep the worker running. Source files are in `examples/`; the PDF and DOCX are included and can be regenerated with `scripts/generate_examples.py`.

## 1. Trace an answer back to a real document

Upload `handbook.md` with `{"department":"hr"}`. Wait for ready. Ask **“How many annual leave days do employees receive?”** Expect 25 days with a citation. Click the source and inspect the excerpt/locator. Ask about an equipment allowance; expect 500 credits.

What this demonstrates: actual extraction, asynchronous publication, retrieval, generated source references and authorized source access. Default answering is extractive, so wording comes from the source.

## 2. Compare document formats and metadata filters

Upload `runbook.html` as engineering, `products.csv` as sales, `travel-policy.pdf` as HR and `security-policy.docx` as security. Ask **“When must a P1 incident be acknowledged?”** with engineering filter; expect 10 minutes. Ask **“What is the hotel reimbursement cap?”**; expect 180 credits. Ask the same question with a nonexistent department; expect abstention.

What this demonstrates: five different parsers, locators, index availability and filtering before candidate selection. HTML script tags are not run.

## 3. Prove organization isolation with conflicting facts

Initialize Beta in a second terminal pointing to the same local database:

```bash
python -m tenantforge init --tenant beta
python -m tenantforge token --tenant beta --user bob --role editor
```

Connect another browser tab with Bob’s token and upload `beta-policy.txt`. Ask about annual leave. Acme should answer 25; Beta should answer 12. Beta’s source list must not show Acme files. Copy an Acme source URL and request it with Beta’s token; expect `404`.

What this demonstrates: tenant identity is taken from the token and carried through files, SQL, vectors, caches and source endpoints. Do not use the default signing key outside a local demonstration.

## 4. Conversation memory and user privacy

As Alice, ask about annual leave and then **“When does it begin?”** in the same conversation. Inspect `GET /v1/conversations/{id}` using Alice’s token. Mint a second Acme user token:

```bash
python -m tenantforge token --tenant acme --user charlie --role reader
```

Charlie can read organization documents but cannot read, continue or delete Alice’s conversation: those calls return `404`. A reader also cannot upload or delete source documents.

What this demonstrates: organization-visible knowledge and user-private memory are separate policies. Follow-up expansion is intentionally simple; evaluate it before relying on complex dialogue resolution.

## 5. Cache invalidation and deletion

Start a new conversation and ask **“What is the annual leave policy?”** twice in separate new conversations as the same user. The second standalone question should show a cached answer. Delete the HR handbook. Ask the question again with the HR filter after removing other HR sources if needed; it should not return the deleted handbook. Open the previous citation: it should return `404`.

For queue recovery, stop the worker, upload a file, then restart the worker. The queued document should become ready. Upload a corrupt PDF to see a failed job; retry after correcting the source/environment.

What this demonstrates: durable jobs, answer-version invalidation, immediate retrieval revocation and honest failure handling. Old conversation transcripts can still contain previously returned text until the owner deletes them; see retention notes.

## Suggested 5–7 minute presentation

Spend one minute on the architecture, two on upload/retrieval/citations, one on metadata, one on tenant/user isolation and one on deletion/retries. End with the measured local validation and the explicit production gaps, especially external model quality and the 100K-user capacity qualification.
