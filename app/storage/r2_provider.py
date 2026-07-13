import boto3
from botocore.config import Config
from flask import current_app
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
)
from app.storage.base import BaseStorageProvider


class R2StorageProvider(BaseStorageProvider):
    """
    Cloudflare R2 storage provider.

    Uses the S3-compatible API via boto3.
    """

    def __init__(self):
        self.account_id = current_app.config["R2_ACCOUNT_ID"]
        self.bucket_name = current_app.config["R2_BUCKET_NAME"]
        self.endpoint_url = current_app.config["R2_ENDPOINT_URL"]
        self.access_key = current_app.config["R2_ACCESS_KEY_ID"]
        self.secret_key = current_app.config["R2_SECRET_ACCESS_KEY"]

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4"
            ),
        )

    def upload_file(
        self,
        *,
        file_obj,
        object_key,
        content_type=None,
    ):
        """
        Upload a binary file-like object to Cloudflare R2.

        Returns basic provider metadata.
        """

        if file_obj is None:
            raise ValueError(
                "file_obj is required."
            )

        if not object_key or not str(object_key).strip():
            raise ValueError(
                "object_key is required."
            )

        clean_object_key = str(
            object_key
        ).strip().lstrip("/")

        try:
            file_obj.seek(0)
        except (AttributeError, OSError):
            pass

        extra_args = {}

        if content_type:
            extra_args["ContentType"] = content_type

        try:
            upload_kwargs = {
                "Fileobj": file_obj,
                "Bucket": self.bucket_name,
                "Key": clean_object_key,
            }

            if extra_args:
                upload_kwargs["ExtraArgs"] = extra_args

            self.client.upload_fileobj(
                **upload_kwargs
            )

            metadata = self.client.head_object(
                Bucket=self.bucket_name,
                Key=clean_object_key,
            )

            return {
                "bucket_name": self.bucket_name,
                "object_key": clean_object_key,
                "content_type": metadata.get(
                    "ContentType"
                ),
                "content_length": metadata.get(
                    "ContentLength"
                ),
                "etag": (
                    metadata.get("ETag", "")
                    .strip('"')
                ),
                "last_modified": metadata.get(
                    "LastModified"
                ),
            }

        except (ClientError, BotoCoreError) as error:
            raise RuntimeError(
                f"Unable to upload object: {clean_object_key}"
            ) from error

    def delete_file(
        self,
        *,
        object_key,
    ):
        """
        Delete an object from Cloudflare R2.

        Returns True when the delete request
        completes successfully.
        """

        if not object_key or not str(object_key).strip():
            raise ValueError(
                "object_key is required."
            )

        clean_object_key = str(
            object_key
        ).strip().lstrip("/")

        try:
            self.client.delete_object(
                Bucket=self.bucket_name,
                Key=clean_object_key,
            )

            return True

        except (ClientError, BotoCoreError) as error:
            raise RuntimeError(
                f"Unable to delete object: {clean_object_key}"
            ) from error

    def file_exists(
        self,
        *,
        object_key,
    ):
        """
        Return True when an object exists in the configured bucket.

        Return False only when the object does not exist.
        Other provider errors are raised.
        """

        if not object_key or not str(object_key).strip():
            raise ValueError(
                "object_key is required."
            )

        clean_object_key = str(
            object_key
        ).strip().lstrip("/")

        try:
            self.client.head_object(
                Bucket=self.bucket_name,
                Key=clean_object_key,
            )

            return True

        except ClientError as error:
            error_code = str(
                error.response.get(
                    "Error",
                    {},
                ).get(
                    "Code",
                    "",
                )
            )

            http_status = error.response.get(
                "ResponseMetadata",
                {},
            ).get(
                "HTTPStatusCode"
            )

            if (
                error_code in {
                    "404",
                    "NoSuchKey",
                    "NotFound",
                }
                or http_status == 404
            ):
                return False

            raise RuntimeError(
                "Unable to check whether R2 object exists: "
                f"{clean_object_key}"
            ) from error

        except BotoCoreError as error:
            raise RuntimeError(
                "Unable to check whether R2 object exists: "
                f"{clean_object_key}"
            ) from error

    def generate_download_url(
        self,
        *,
        object_key,
        expires_in=600,
        download_filename=None,
    ):
        """
        Generate a temporary signed URL that forces file download.

        Default validity: 10 minutes.
        """

        if not object_key or not str(object_key).strip():
            raise ValueError(
                "object_key is required."
            )

        clean_object_key = str(
            object_key
        ).strip().lstrip("/")

        params = {
            "Bucket": self.bucket_name,
            "Key": clean_object_key,
        }

        if download_filename:
            safe_download_filename = str(
                download_filename
            ).replace(
                '"',
                "",
            ).strip()

            params["ResponseContentDisposition"] = (
                f'attachment; filename="{safe_download_filename}"'
            )

        else:
            params["ResponseContentDisposition"] = "attachment"

        try:
            return self.client.generate_presigned_url(
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=int(expires_in),
            )

        except (ClientError, BotoCoreError) as error:
            raise RuntimeError(
                "Unable to generate download URL for: "
                f"{clean_object_key}"
            ) from error

    def generate_preview_url(
        self,
        *,
        object_key,
        expires_in=600,
    ):
        """
        Generate a temporary signed URL for browser preview.

        Default validity: 10 minutes.
        """

        if not object_key or not str(object_key).strip():
            raise ValueError(
                "object_key is required."
            )

        clean_object_key = str(
            object_key
        ).strip().lstrip("/")

        try:
            return self.client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": clean_object_key,
                    "ResponseContentDisposition": "inline",
                },
                ExpiresIn=int(expires_in),
            )

        except (ClientError, BotoCoreError) as error:
            raise RuntimeError(
                "Unable to generate preview URL for: "
                f"{clean_object_key}"
            ) from error
    def verify_connection(self):
        """
        Verify Cloudflare R2 connection by checking
        whether the configured bucket is accessible.
        """

        try:
            self.client.head_bucket(
                Bucket=self.bucket_name
            )

            return True

        except ClientError as error:
            raise RuntimeError(
                f"Unable to connect to R2 bucket: {self.bucket_name}"
            ) from error