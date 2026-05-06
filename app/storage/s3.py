"""Hetzner Object Storage client (S3-compatible) via aioboto3."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from botocore.config import Config

from app.config import get_settings


def _client_config(addressing_style: str) -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
        retries={"max_attempts": 5, "mode": "standard"},
    )


@asynccontextmanager
async def s3_client(addressing_style: str = "path") -> AsyncIterator[Any]:
    settings = get_settings()
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=_client_config(addressing_style),
    ) as client:
        yield client


def _join_key(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)


def receipt_key(user_id: int, trip_id: int, receipt_id: int) -> str:
    settings = get_settings()
    return _join_key(settings.s3_prefix_receipts, str(user_id), str(trip_id), f"{receipt_id}.jpg")


def report_key(user_id: int, trip_id: int, filename: str) -> str:
    settings = get_settings()
    return _join_key(settings.s3_prefix_reports, str(user_id), str(trip_id), filename)


async def upload_bytes(
    bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
) -> None:
    async with s3_client("path") as client:
        await client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )


async def upload_file(
    bucket: str, key: str, path: str, content_type: str = "application/octet-stream"
) -> None:
    async with s3_client("path") as client:
        with open(path, "rb") as fh:
            await client.put_object(
                Bucket=bucket,
                Key=key,
                Body=fh.read(),
                ContentType=content_type,
            )


async def download_bytes(bucket: str, key: str) -> bytes:
    async with s3_client("path") as client:
        resp = await client.get_object(Bucket=bucket, Key=key)
        async with resp["Body"] as stream:
            return await stream.read()


async def delete_object(bucket: str, key: str) -> None:
    async with s3_client("path") as client:
        await client.delete_object(Bucket=bucket, Key=key)


async def presign_url(bucket: str, key: str, expires_in: int) -> str:
    async with s3_client("virtual") as client:
        return await client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
