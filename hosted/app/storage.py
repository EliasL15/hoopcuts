from __future__ import annotations

from .config import settings


def client():
    import boto3
    from botocore.config import Config
    if not settings.configured_storage:
        raise RuntimeError("R2 storage is not configured")
    return boto3.client("s3", endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto", config=Config(signature_version="s3v4"))


def key(user_id: str, run_id: str, name: str) -> str:
    # Callers supply only controlled names; never use a client-provided path.
    return f"users/{user_id}/runs/{run_id}/{name}"


def signed_upload(user_id: str, run_id: str, extension: str, content_type: str, content_length: int) -> dict:
    object_key = key(user_id, run_id, f"source{extension}")
    url = client().generate_presigned_url("put_object", Params={
        "Bucket": settings.r2_bucket, "Key": object_key, "ContentType": content_type,
        "ContentLength": content_length,
    }, ExpiresIn=3600)
    return {"key": object_key, "upload_url": url, "expires_in": 3600}


def signed_download(object_key: str, expires: int = 600, filename: str | None = None) -> str:
    params = {"Bucket": settings.r2_bucket, "Key": object_key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def delete_prefix(prefix: str) -> None:
    s3 = client()
    pages = s3.get_paginator("list_objects_v2")
    for page in pages.paginate(Bucket=settings.r2_bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            result = s3.delete_objects(Bucket=settings.r2_bucket, Delete={"Objects": objects})
            if result.get("Errors"):
                raise RuntimeError("Some media objects could not be deleted")


def delete_object(object_key: str) -> None:
    client().delete_object(Bucket=settings.r2_bucket, Key=object_key)


def exists(object_key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=settings.r2_bucket, Key=object_key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
