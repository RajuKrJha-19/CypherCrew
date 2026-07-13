from abc import ABC, abstractmethod


class BaseStorageProvider(ABC):
    """
    Abstract base class for all storage providers.

    Every storage backend (Cloudflare R2, AWS S3,
    Local Storage, etc.) must implement this interface.
    """

    @abstractmethod
    def upload_file(
        self,
        *,
        file_obj,
        object_key,
        content_type=None,
    ):
        """
        Upload a file object.

        Returns provider metadata.
        """
        raise NotImplementedError

    @abstractmethod
    def delete_file(
        self,
        *,
        object_key,
    ):
        """
        Delete a stored object.
        """
        raise NotImplementedError

    @abstractmethod
    def file_exists(
        self,
        *,
        object_key,
    ):
        """
        Check whether an object exists.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_download_url(
        self,
        *,
        object_key,
        expires_in=3600,
    ):
        """
        Generate a temporary download URL.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_preview_url(
        self,
        *,
        object_key,
        expires_in=3600,
    ):
        """
        Generate a temporary preview URL.
        """
        raise NotImplementedError