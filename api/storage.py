"""
Custom S3 storage backend tuned for large multipart uploads over slow links.
"""

from boto3.s3.transfer import TransferConfig
from botocore.client import Config as BotoCoreConfig
from storages.backends.s3 import S3Storage


class TimeoutS3Storage(S3Storage):
    """
    S3Boto3Storage-compatible backend with generous socket timeouts, adaptive
    retries, and a TransferConfig tuned for large PDFs on unstable connections.
    """

    def __init__(self, **settings):
        super().__init__(**settings)
        self.config = self.config.merge(
            BotoCoreConfig(
                connect_timeout=15,
                read_timeout=300,
                retries={"max_attempts": 10, "mode": "adaptive"},
                tcp_keepalive=True,
            )
        )
        self.transfer_config = TransferConfig(
            multipart_threshold=16 * 1024 * 1024,
            multipart_chunksize=8 * 1024 * 1024,
            max_concurrency=2,
            num_download_attempts=10,
            use_threads=True,
        )