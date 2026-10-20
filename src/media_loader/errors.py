from __future__ import annotations


class MediaLoaderError(Exception):
    def __init__(self, message: str, http_status: int = 502, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


class InvalidRequestError(MediaLoaderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=422, code="invalid_request")


class UnsupportedMediaError(MediaLoaderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=422, code="unsupported_media")


class DiskFullError(MediaLoaderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=507, code="disk_full")


class DownloadFailedError(MediaLoaderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=502, code="download_failed")


class LeaseLostError(MediaLoaderError):
    def __init__(self, message: str = "source lease lost") -> None:
        super().__init__(message, http_status=503, code="lease_lost")
