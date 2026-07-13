import re
import unicodedata
from datetime import datetime
from uuid import uuid4

from flask import current_app
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import TaskFile
from app.storage.r2_provider import R2StorageProvider


class StorageServiceError(Exception):
    """
    Raised when an application-level storage operation fails.
    """


class StorageService:
    """
    Main storage service for CypherCrew.

    Routes and application modules should communicate only with
    this service instead of accessing R2StorageProvider directly.
    """

    ALLOWED_FOLDER_TYPES = {
        "reference",
        "working",
        "final",
    }

    def __init__(self):
        self.provider = R2StorageProvider()

    @staticmethod
    def _slugify_segment(value, fallback="unknown"):
        """
        Convert a business name into a safe object-key segment.

        Example:
            Hope Plus IVF -> hope-plus-ivf
            Social Media  -> social-media
        """

        value = str(value or "").strip()

        if not value:
            return fallback

        normalized_value = unicodedata.normalize(
            "NFKD",
            value,
        )

        ascii_value = normalized_value.encode(
            "ascii",
            "ignore",
        ).decode(
            "ascii",
        )

        slug = re.sub(
            r"[^a-zA-Z0-9]+",
            "-",
            ascii_value,
        ).strip(
            "-"
        ).lower()

        return slug or fallback

    @staticmethod
    def _get_service_name(task):
        """
        Resolve the task service or deliverable name.
        """

        deliverable = getattr(
            task,
            "deliverable",
            None,
        )

        if deliverable is None:
            raise StorageServiceError(
                "Task deliverable relationship is required."
            )

        service_name = (
            getattr(deliverable, "service_name", None)
            or getattr(deliverable, "deliverable_name", None)
        )

        if not service_name:
            raise StorageServiceError(
                "Task service name could not be resolved."
            )

        return service_name

    def _build_task_object_key(
        self,
        *,
        task,
        folder_type,
        stored_filename,
    ):
        """
        Build a provider-independent object key.

        Structure:
            clients/
                client/
                    service/
                        year/
                            month/
                                day/
                                    TASK-XXXX/
                                        reference|working|final/
                                            stored-file
        """

        if folder_type not in self.ALLOWED_FOLDER_TYPES:
            raise StorageServiceError(
                "Invalid task storage folder type."
            )

        client = getattr(
            task,
            "client",
            None,
        )

        if client is None:
            raise StorageServiceError(
                "Task client relationship is required."
            )

        client_name = getattr(
            client,
            "client_name",
            None,
        )

        if not client_name:
            raise StorageServiceError(
                "Task client name could not be resolved."
            )

        service_name = self._get_service_name(
            task
        )

        task_date = (
            getattr(task, "created_at", None)
            or datetime.utcnow()
        )

        task_code = getattr(
            task,
            "task_code",
            None,
        )

        if task_code is None:
            raise StorageServiceError(
                "Task code is required for file storage."
            )

        return "/".join(
            [
                "clients",
                self._slugify_segment(client_name),
                self._slugify_segment(service_name),
                str(task_date.year),
                task_date.strftime("%B").lower(),
                str(task_date.day).zfill(2),
                f"TASK-{task_code}",
                folder_type,
                stored_filename,
            ]
        )

    def upload(
        self,
        *,
        file_obj,
        object_key,
        content_type=None,
    ):
        """
        Upload a raw file object through the configured provider.
        """

        try:
            return self.provider.upload_file(
                file_obj=file_obj,
                object_key=object_key,
                content_type=content_type,
            )

        except Exception as error:
            raise StorageServiceError(
                f"Unable to upload object: {object_key}"
            ) from error

    def upload_task_file(
        self,
        *,
        task,
        file_storage,
        uploaded_by_id,
        folder_type="reference",
        is_final=False,
    ):
        """
        Upload one Flask FileStorage object for a task.

        The actual file is uploaded to Cloudflare R2.
        A TaskFile metadata record is added to the current
        SQLAlchemy session.

        Database commit remains the caller's responsibility.
        """

        if task is None or task.id is None:
            raise StorageServiceError(
                "Task must be saved before uploading files."
            )

        if file_storage is None:
            raise StorageServiceError(
                "Uploaded file is required."
            )

        if not uploaded_by_id:
            raise StorageServiceError(
                "Uploader ID is required."
            )

        if folder_type not in self.ALLOWED_FOLDER_TYPES:
            raise StorageServiceError(
                "Invalid task storage folder type."
            )

        original_filename = str(
            file_storage.filename or ""
        ).strip()

        if not original_filename:
            raise StorageServiceError(
                "Uploaded file must have a filename."
            )

        safe_filename = secure_filename(
            original_filename
        )

        if not safe_filename:
            raise StorageServiceError(
                "Uploaded filename is invalid."
            )

        unique_prefix = uuid4().hex[:12]

        stored_filename = (
            f"{unique_prefix}_{safe_filename}"
        )

        object_key = self._build_task_object_key(
            task=task,
            folder_type=folder_type,
            stored_filename=stored_filename,
        )

        try:
            upload_result = self.upload(
                file_obj=file_storage.stream,
                object_key=object_key,
                content_type=file_storage.mimetype,
            )

            task_file = TaskFile(
                task_id=task.id,
                bucket_name=upload_result["bucket_name"],
                storage_provider="r2",
                object_key=upload_result["object_key"],
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=(
                    upload_result.get("content_type")
                    or file_storage.mimetype
                ),
                file_size=(
                    upload_result.get("content_length")
                    or 0
                ),
                folder_type=folder_type,
                version=1,
                is_final=is_final,
                uploaded_by_id=uploaded_by_id,
            )

            db.session.add(
                task_file
            )

            return {
                "task_file": task_file,
                "provider_metadata": upload_result,
            }

        except StorageServiceError:
            raise

        except Exception as error:
            try:
                if self.provider.file_exists(
                    object_key=object_key
                ):
                    self.provider.delete_file(
                        object_key=object_key
                    )
            except Exception:
                current_app.logger.exception(
                    "Unable to remove orphan R2 object: %s",
                    object_key,
                )

            raise StorageServiceError(
                f"Unable to save task file: {original_filename}"
            ) from error

    def delete(
        self,
        *,
        object_key,
    ):
        return self.provider.delete_file(
            object_key=object_key,
        )

    def exists(
        self,
        *,
        object_key,
    ):
        return self.provider.file_exists(
            object_key=object_key,
        )

    def preview_url(
        self,
        *,
        object_key,
        expires_in=3600,
    ):
        return self.provider.generate_preview_url(
            object_key=object_key,
            expires_in=expires_in,
        )

    def download_url(
        self,
        *,
        object_key,
        expires_in=600,
        download_filename=None,
    ):
        return self.provider.generate_download_url(
            object_key=object_key,
            expires_in=expires_in,
            download_filename=download_filename,
        )