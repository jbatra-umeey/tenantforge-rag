import os
import uuid
from pathlib import Path

from .auth import ID


class Storage:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.storage_dir).resolve()
        self.s3 = None
        if settings.storage_backend == "s3":
            import boto3
            from botocore.config import Config
            self.s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint or None,
                config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3}))
        else:
            self.root.mkdir(parents=True, exist_ok=True)

    def validate(self, tenant, key):
        if not ID.fullmatch(tenant) or not key.startswith(tenant + "/"):
            raise ValueError("Object tenant mismatch")
        if ".." in key or len(key.split("/")) != 2 or "\\" in key:
            raise ValueError("Invalid object key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid object path")
        return path

    def put(self, tenant, data):
        key = f"{tenant}/{uuid.uuid4()}"
        path = self.validate(tenant, key)
        if self.s3:
            self.s3.put_object(Bucket=self.settings.s3_bucket, Key=key, Body=data, ServerSideEncryption="AES256")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            with temp.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            temp.replace(path)
        return key

    def get(self, tenant, key):
        path = self.validate(tenant, key)
        if self.s3:
            body = self.s3.get_object(Bucket=self.settings.s3_bucket, Key=key)["Body"]
            try:
                return body.read(self.settings.max_upload_bytes + 1)
            finally:
                body.close()
        return path.read_bytes()

    def delete(self, tenant, key):
        path = self.validate(tenant, key)
        if self.s3:
            self.s3.delete_object(Bucket=self.settings.s3_bucket, Key=key)
        else:
            path.unlink(missing_ok=True)
