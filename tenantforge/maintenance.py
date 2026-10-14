import time

from sqlalchemy import delete, select

from .db import cache, documents, embedding_cache, usage


def purge(db, storage, tenant, user):
    """Retry raw object cleanup; expire caches. Run per tenant/user or with a trusted maintenance role."""
    removed = 0
    with db.tx(tenant, user) as c:
        rows = c.execute(select(documents).where(documents.c.tenant_id == tenant,
            documents.c.status == "deleted")).mappings().all()
    for row in rows:
        storage.delete(tenant, row["object_key"])
        removed += 1
    with db.tx(tenant, user) as c:
        c.execute(delete(cache).where(cache.c.tenant_id == tenant, cache.c.user_id == user,
                                     cache.c.expires_at < time.time()))
        c.execute(delete(embedding_cache).where(embedding_cache.c.tenant_id == tenant,
                                               embedding_cache.c.created_at < time.time()-7*86400))
        # Keep current rate and daily token buckets only.
        c.execute(delete(usage).where(usage.c.tenant_id == tenant,
            usage.c.bucket.not_in([f"queries:{int(time.time()//60)}", f"tokens:{int(time.time()//86400)}"])))
    return {"objects_cleanup_attempted": removed}
