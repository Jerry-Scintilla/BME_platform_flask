"""对象存储封装（MinIO / 任意 S3 兼容服务）。

薄封装 minio Python 客户端。单例 `storage`，app.py 启动时 `storage.init_app(app)`
注入配置；bucket 懒建（首次上传时 ensure，幂等），存储服务未起时 app 仍可启动，
仅文件接口报错，不影响其他流程。

换存储后端（MinIO -> 阿里云 OSS S3 模式 / 腾讯 COS / AWS S3 等）只改 .env 的
MINIO_* 配置，本模块与业务代码不动。
"""
from minio import Minio


class Storage:
    """MinIO 封装单例。"""

    def __init__(self):
        self._client = None
        self.endpoint = None
        self.bucket = None
        self.secure = False
        self._bucket_ready = False

    def init_app(self, app):
        """从 app.config 读 MINIO_* 配置，建客户端（不连，懒连接）。"""
        self.endpoint = app.config["MINIO_ENDPOINT"]
        self.bucket = app.config["MINIO_BUCKET"]
        self.secure = app.config["MINIO_SECURE"]
        self._client = Minio(
            self.endpoint,
            access_key=app.config["MINIO_ACCESS_KEY"],
            secret_key=app.config["MINIO_SECRET_KEY"],
            secure=self.secure,
        )
        self._bucket_ready = False

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("Storage 未初始化（未调 init_app）")
        return self._client

    def ensure_bucket(self):
        """幂等建 bucket。存储服务未起时抛异常由调用方处理。"""
        if self._bucket_ready:
            return
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        self._bucket_ready = True

    # ── 对象操作 ──
    def put_object(self, key, stream, length=None, content_type="application/octet-stream"):
        """上传对象。stream 是可读二进制流；length 未知时走分片上传（-1）。"""
        self.ensure_bucket()
        if length is None:
            return self.client.put_object(
                self.bucket, key, stream, -1,
                part_size=16 * 1024 * 1024, content_type=content_type
            )
        return self.client.put_object(
            self.bucket, key, stream, length, content_type=content_type
        )

    def stat_object(self, key):
        return self.client.stat_object(self.bucket, key)

    def get_object(self, key):
        """返回可读响应流（调用方负责关闭并 release_conn）。"""
        return self.client.get_object(self.bucket, key)

    def remove_object(self, key):
        """删除对象；对象已不存在时静默成功（幂等），其他错误抛出。"""
        from minio.error import S3Error
        try:
            self.client.remove_object(self.bucket, key)
        except S3Error as e:
            if getattr(e, "code", None) in ("NoSuchKey", "NoSuchObject"):
                return
            raise


storage = Storage()
