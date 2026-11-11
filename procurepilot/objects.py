"""原始附件对象存储：内容寻址、原子写入和读取完整性校验。

文件名不参与对象路径。工作区授权由附件记录提供，调用者不能凭哈希直接
下载对象。默认保留原件，数据库事务失败产生的无引用对象可由维护工具清理。
"""

import hashlib
import os
from pathlib import Path
import re
from contextlib import closing
from urllib.parse import urlsplit
from uuid import uuid4
from .service import fail


MAX_OBJECT_BYTES = 2_000_000


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        fail("附件对象标识无效", 503)
    return value


def _metadata(content, backend):
    if len(content) > MAX_OBJECT_BYTES:
        fail("原始附件超过2MB", 413)
    key = hashlib.sha256(content).hexdigest()
    return {"key": key, "size": len(content), "sha256": key, "backend": backend}


def _verify(content, metadata):
    if (
        len(content) > MAX_OBJECT_BYTES
        or len(content) != metadata.get("size")
        or hashlib.sha256(content).hexdigest() != metadata.get("sha256")
    ):
        fail("原始附件完整性校验失败，已停止下载", 503)
    return content


class LocalObjects:
    def __init__(self):
        self.root = Path(
            os.getenv("OBJECT_ROOT")
            or Path(__file__).resolve().parents[1] / ".local/objects"
        ).resolve()

    def path(self, key):
        key = _key(key)
        target = (self.root / key[:2] / key[2:]).resolve()
        if not target.is_relative_to(self.root):
            fail("附件对象路径无效", 503)
        return target

    def put(self, content):
        """相同内容复用同一对象；只在完整写入后原子发布。"""
        metadata = _metadata(content, "local")
        target = self.path(metadata["key"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / ("upload-" + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata

    def get(self, metadata):
        target = self.path(metadata["key"])
        try:
            with target.open("rb") as stream:
                content = stream.read(MAX_OBJECT_BYTES + 1)
        except OSError:
            fail("原始附件对象不可用，请检查存储或重新上传", 503)
        return _verify(content, metadata)


class S3Objects:
    """S3 原件存储。SQL 引用仅包含摘要、字节数与存储命名空间。"""

    def __init__(self):
        try:
            from botocore.config import Config
            from botocore.session import Session
        except ImportError:
            fail("请安装 S3 对象存储依赖", 503)
        self.session_factory = Session
        self.bucket = os.getenv("OBJECT_S3_BUCKET", "")
        self.prefix = os.getenv("OBJECT_S3_PREFIX", "procurepilot")
        self.endpoint = os.getenv("OBJECT_S3_ENDPOINT") or None
        self.region = os.getenv("OBJECT_S3_REGION", "us-east-1")
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket
        ) or not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", self.prefix):
            fail("S3 桶名或对象前缀配置无效", 503)
        if self.endpoint:
            try:
                url = urlsplit(self.endpoint)
                port = url.port
            except ValueError:
                fail("S3 自定义端点地址无效", 503)
            loopback = url.hostname in ("127.0.0.1", "::1", "localhost")
            if (
                not url.hostname
                or port == 0
                or url.username
                or url.password
                or url.query
                or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and loopback))
            ):
                fail("S3 自定义端点需使用 HTTPS 或本机回环 HTTP", 503)
        self.namespace = hashlib.sha256(
            f"{self.bucket}/{self.prefix}".encode()
        ).hexdigest()
        self.config = Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 2, "mode": "standard"},
            proxies={},
            ignore_configured_endpoint_urls=True,
            s3={"addressing_style": "path", "payload_signing_enabled": True},
            request_checksum_calculation="when_required",
        )

    def _client(self):
        return self.session_factory().create_client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            config=self.config,
        )

    def _request(self, metadata):
        key = _key(metadata.get("key"))
        size = metadata.get("size")
        if (
            metadata.get("namespace") != self.namespace
            or metadata.get("sha256") != key
            or type(size) is not int
            or not 0 <= size <= MAX_OBJECT_BYTES
        ):
            fail("附件对象记录与当前存储不一致", 503)
        return {"Bucket": self.bucket, "Key": f"{self.prefix}/{key[:2]}/{key[2:]}"}

    def put(self, content):
        """条件写入保持内容去重，竞争时有限重试并验证已有对象的元数据。"""
        from botocore.exceptions import BotoCoreError, ClientError

        metadata = {**_metadata(content, "s3"), "namespace": self.namespace}
        request = self._request(metadata)
        try:
            with closing(self._client()) as client:
                for attempt in range(2):
                    try:
                        client.put_object(
                            **request,
                            Body=content,
                            IfNoneMatch="*",
                            ContentType="application/octet-stream",
                            Metadata={"sha256": metadata["sha256"]},
                        )
                    except ClientError as error:
                        status = error.response["ResponseMetadata"]["HTTPStatusCode"]
                        if status == 409 and attempt == 0:
                            continue
                        if status != 412:
                            raise
                        existing = client.head_object(**request)
                        if (
                            existing.get("ContentLength") != metadata["size"]
                            or existing.get("Metadata", {}).get("sha256")
                            != metadata["sha256"]
                        ):
                            fail("已存在的 S3 对象元数据与附件不一致", 503)
                    return metadata
        except (BotoCoreError, ClientError, OSError):
            fail("原始附件对象存储写入失败", 503)

    def get(self, metadata):
        from botocore.exceptions import BotoCoreError, ClientError

        request = self._request(metadata)
        try:
            with closing(self._client()) as client:
                result = client.get_object(**request)
                if result.get("Body") is None:
                    fail("原始附件对象不可用，请检查存储或重新上传", 503)
                with closing(result["Body"]) as stream:
                    if result.get("ContentLength") != metadata["size"]:
                        fail("原始附件完整性校验失败，已停止下载", 503)
                    content = stream.read(MAX_OBJECT_BYTES + 1)
        except (BotoCoreError, ClientError, OSError):
            fail("原始附件对象不可用，请检查存储或重新上传", 503)
        return _verify(content, metadata)


def object_store(backend=None):
    """上传使用当前配置，读取按已持久化的后端路由。"""
    backend = os.getenv("OBJECT_BACKEND", "local") if backend is None else backend
    if backend == "local":
        return LocalObjects()
    if backend == "s3":
        return S3Objects()
    fail("未知附件对象存储后端", 503)
