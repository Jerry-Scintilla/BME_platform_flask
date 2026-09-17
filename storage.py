"""对象存储封装（可切换后端：MinIO / 任意 S3 兼容服务 / 本地磁盘）。

单例 `storage`，app.py 启动时 `storage.init_app(app)` 按 STORAGE_BACKEND 选择实现：
- minio（默认，向后兼容）：薄封装 minio 客户端；bucket 懒建（首次上传时 ensure，幂等），
  存储服务未起时 app 仍可启动，仅文件接口报错，不影响其他流程。
  换 S3 兼容服务（MinIO -> 阿里云 OSS S3 模式 / 腾讯 COS / AWS S3 等）只改 .env 的
  MINIO_* 配置，本模块与业务代码不动。
- local：本地磁盘后端，object_key 原样映射为 {DATA_ROOT}/storage/{key} 目录树，
  无需任何外部存储服务（开发环境 / 轻量部署用）。

对外接口签名（put_object/stat_object/get_object/remove_object/ensure_bucket）与
调用约定在各后端间完全一致，业务代码对后端无感。
"""
import os
import shutil
import types

from minio import Minio


class _LocalObject:
    """本地文件读流，兼容 minio urllib3 响应的使用方式（read/close/release_conn）。"""

    def __init__(self, fh):
        self._fh = fh

    def read(self, *args):
        return self._fh.read(*args)

    def __iter__(self):
        return iter(self._fh)

    def close(self):
        self._fh.close()

    def release_conn(self):
        pass    # no-op：minio 调用方有 close+release_conn 双调先例，保持同构


class _BoundedReader:
    """限定最多读 length 字节的文件包装（HTTP Range 分段读取用）。
    兼容 _LocalObject 的使用方式（read/close），__iter__ 按块耗尽后自然停止。"""

    def __init__(self, fh, length):
        self._fh = fh
        self._remaining = max(int(length), 0)

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self._remaining
        chunk = self._fh.read(min(size, self._remaining))
        self._remaining -= len(chunk)
        return chunk

    def __iter__(self):
        while True:
            chunk = self.read(1024 * 1024)
            if not chunk:
                break
            yield chunk

    def close(self):
        self._fh.close()


class MinioStorage:
    """MinIO / S3 兼容后端（原 Storage 逻辑平移）。"""

    def __init__(self, endpoint, access_key, secret_key, bucket, secure):
        self.endpoint = endpoint
        self.bucket = bucket
        self.secure = secure
        self._client = Minio(endpoint, access_key=access_key,
                             secret_key=secret_key, secure=secure)
        self._bucket_ready = False

    def ensure_bucket(self):
        """幂等建 bucket。存储服务未起时抛异常由调用方处理。"""
        if self._bucket_ready:
            return
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)
        self._bucket_ready = True

    # ── 对象操作 ──
    def put_object(self, key, stream, length=None, content_type="application/octet-stream"):
        """上传对象。stream 是可读二进制流；length 未知时走分片上传（-1）。"""
        self.ensure_bucket()
        if length is None:
            return self._client.put_object(
                self.bucket, key, stream, -1,
                part_size=16 * 1024 * 1024, content_type=content_type
            )
        return self._client.put_object(
            self.bucket, key, stream, length, content_type=content_type
        )

    def stat_object(self, key):
        return self._client.stat_object(self.bucket, key)

    def get_object(self, key, offset=None, length=None):
        """返回可读响应流（调用方负责关闭并 release_conn）。
        offset/length 用于 HTTP Range 分段读取（视频拖动进度条依赖 206）。"""
        return self._client.get_object(self.bucket, key, offset=offset, length=length)

    def remove_object(self, key):
        """删除对象；对象已不存在时静默成功（幂等），其他错误抛出。"""
        from minio.error import S3Error
        try:
            self._client.remove_object(self.bucket, key)
        except S3Error as e:
            if getattr(e, "code", None) in ("NoSuchKey", "NoSuchObject"):
                return
            raise


class LocalStorage:
    """本地磁盘后端：无 bucket 概念，object_key 原样映射为 {root}/{key} 目录树
    （如 'camp/3/chapter/12/abc.pdf' -> {DATA_ROOT}/storage/camp/3/chapter/12/abc.pdf）。"""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)   # 本地无「服务未起」态，init 即建根

    def _path(self, key):
        p = os.path.abspath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):   # 防目录穿越（key 虽多来自 DB，仍守一道）
            raise ValueError(f"非法 object_key: {key}")
        return p

    def ensure_bucket(self):
        pass    # 幂等语义天然成立（init 已建根）

    def put_object(self, key, stream, length=None, content_type="application/octet-stream"):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            if length is None:
                # 对齐 minio 分片语义：未知长度按块拷贝整个流
                shutil.copyfileobj(stream, fh, 1024 * 1024)
            else:
                # 只写 length 字节（copyfileobj 的第三参是缓冲大小非总量，须显式限量）
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)

    def stat_object(self, key):
        st = os.stat(self._path(key))    # 不存在抛 FileNotFoundError，对齐 minio 抛错语义
        return types.SimpleNamespace(size=st.st_size)

    def get_object(self, key, offset=None, length=None):
        fh = open(self._path(key), "rb")
        if offset is not None:
            fh.seek(offset)
        if length is None:
            return _LocalObject(fh)
        return _LocalObject(_BoundedReader(fh, length))

    def remove_object(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            return                        # 幂等，对齐 minio NoSuchKey 静默


class Storage:
    """门面单例：init_app 按 STORAGE_BACKEND 选实现后委托，业务方只认本接口。"""

    def __init__(self):
        self._impl = None
        self.backend = None

    def init_app(self, app):
        """从 app.config 读 STORAGE_BACKEND / MINIO_* / DATA_ROOT，装配后端实现。"""
        backend = (app.config.get("STORAGE_BACKEND") or "minio").lower()
        if backend == "minio":
            self._impl = MinioStorage(
                app.config["MINIO_ENDPOINT"],
                app.config["MINIO_ACCESS_KEY"],
                app.config["MINIO_SECRET_KEY"],
                app.config["MINIO_BUCKET"],
                app.config["MINIO_SECURE"],
            )
        elif backend == "local":
            self._impl = LocalStorage(os.path.join(app.config["DATA_ROOT"], "storage"))
        else:
            raise ValueError(f"未知 STORAGE_BACKEND: {backend}（仅支持 minio/local）")
        self.backend = backend

    def _impl_or_fail(self):
        if self._impl is None:
            raise RuntimeError("Storage 未初始化（未调 init_app）")
        return self._impl

    def ensure_bucket(self):
        return self._impl_or_fail().ensure_bucket()

    # ── 对象操作（签名与各后端一致）──
    def put_object(self, key, stream, length=None, content_type="application/octet-stream"):
        return self._impl_or_fail().put_object(key, stream, length, content_type)

    def stat_object(self, key):
        return self._impl_or_fail().stat_object(key)

    def get_object(self, key, offset=None, length=None):
        """返回可读响应流（调用方负责关闭并 release_conn——local 后端两者皆可用）。
        offset/length 为 HTTP Range 分段读取参数，缺省整读（既有调用方不受影响）。"""
        return self._impl_or_fail().get_object(key, offset=offset, length=length)

    def remove_object(self, key):
        """删除对象；对象已不存在时静默成功（幂等），其他错误抛出。"""
        return self._impl_or_fail().remove_object(key)


storage = Storage()
