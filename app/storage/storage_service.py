import re
import unicodedata
from datetime import datetime, timedelta
from uuid import uuid4

from flask import current_app
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import TaskFile, ClientAsset
from app.storage.r2_provider import R2StorageProvider
from app.utils import client_assets as client_asset_catalog


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
        "submission",
        "final",
    }

    # Content-types R2 is allowed to store as-is. Anything else -
    # including text/html, image/svg+xml, application/xhtml+xml -
    # gets remapped to a generic download type before it ever reaches
    # R2. Preview URLs are served with ResponseContentDisposition=
    # inline using whatever content-type is stored, and content_type
    # for the multipart flow comes straight from the client's JSON
    # body with no other validation, so an unrecognised or
    # browser-executable type would otherwise let an uploaded file
    # render as a live HTML/SVG document (stored XSS) the moment
    # anyone previews it.
    _SAFE_CONTENT_TYPE_PREFIXES = (
        "image/",
        "video/",
        "audio/",
    )

    _SAFE_CONTENT_TYPES = {
        "application/pdf",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
        "application/x-zip-compressed",
        "application/x-rar-compressed",
        "text/plain",
        "text/csv",
        # Brand fonts (client asset uploads) - inert as a browser download,
        # never executed, so there is no reason to fall back to
        # application/octet-stream and lose the real type.
        "font/ttf",
        "font/otf",
        "font/woff",
        "font/woff2",
        "application/font-woff",
        "application/x-font-ttf",
        "application/vnd.ms-fontobject",
    }

    _UNSAFE_IMAGE_CONTENT_TYPES = {
        "image/svg+xml",
    }

    _FALLBACK_CONTENT_TYPE = "application/octet-stream"

    @classmethod
    def _sanitize_content_type(cls, content_type):
        value = str(content_type or "").strip().lower()

        if not value:
            return cls._FALLBACK_CONTENT_TYPE

        if value in cls._UNSAFE_IMAGE_CONTENT_TYPES:
            return cls._FALLBACK_CONTENT_TYPE

        if value in cls._SAFE_CONTENT_TYPES:
            return value

        if value.startswith(cls._SAFE_CONTENT_TYPE_PREFIXES):
            return value

        return cls._FALLBACK_CONTENT_TYPE

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

    def _build_display_filename(
        self,
        *,
        task,
        original_filename,
    ):
        """
        Build the user-facing filename shown in the gallery, task detail
        and download prompt: TASK-<id>-<client>-<task-name>-<dd-mm-yy>.

        This replaces whatever name the uploader's file had - browsers
        default to "final v2 (3).psd" and every file in a task otherwise
        looks the same in a flat download folder. Naming it after the
        task and the upload date makes files self-describing outside
        the app.

        Uses IST (UTC+5:30) for the date, matching the rest of the
        app's date display (see gallery._scope_to_viewer's date
        grouping).
        """

        client = getattr(task, "client", None)
        client_name = getattr(client, "client_name", None) if client else None

        extension = ""

        if "." in original_filename:
            extension = "." + original_filename.rsplit(".", 1)[-1].lower()

        upload_date = datetime.utcnow() + timedelta(hours=5, minutes=30)

        return (
            "-".join(
                [
                    f"TASK-{task.task_code}",
                    self._slugify_segment(client_name),
                    self._slugify_segment(getattr(task, "title", None)),
                    upload_date.strftime("%d-%m-%y"),
                ]
            )
            + extension
        )

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
                content_type=self._sanitize_content_type(content_type),
            )

        except Exception as error:
            raise StorageServiceError(
                f"Unable to upload object: {object_key}"
            ) from error

    def read_bytes(self, object_key):
        """
        Read an object's raw bytes. Used by thumbnail generation.
        """

        try:
            return self.provider.get_bytes(
                object_key=object_key,
            )

        except Exception as error:
            raise StorageServiceError(
                f"Unable to read object: {object_key}"
            ) from error

    def put_bytes(self, *, data, object_key, content_type=None):
        """
        Store raw bytes at `object_key`.

        For derived assets the app generates itself (thumbnails), as
        opposed to upload(), which takes a file from a client.
        """

        import io

        try:
            return self.provider.upload_file(
                file_obj=io.BytesIO(data),
                object_key=object_key,
                content_type=self._sanitize_content_type(content_type),
            )

        except Exception as error:
            raise StorageServiceError(
                f"Unable to write object: {object_key}"
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
                original_filename=self._build_display_filename(
                    task=task,
                    original_filename=original_filename,
                ),
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

    def initiate_task_file_multipart_upload(
        self,
        *,
        task,
        filename,
        folder_type="reference",
        uploaded_by_id,
        content_type=None,
    ):
        """
        Start a multipart upload session for a task file.

        Builds the object key using the same convention as
        upload_task_file, then opens a multipart upload on R2.

        Returns the identifiers the caller needs to request part
        upload URLs and later complete the upload.
        """

        if task is None or task.id is None:
            raise StorageServiceError(
                "Task must be saved before uploading files."
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
            filename or ""
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
            upload_result = self.provider.create_multipart_upload(
                object_key=object_key,
                content_type=self._sanitize_content_type(content_type),
            )

            return {
                "upload_id": upload_result["upload_id"],
                "object_key": upload_result["object_key"],
                "stored_filename": stored_filename,
                "original_filename": self._build_display_filename(
                    task=task,
                    original_filename=original_filename,
                ),
            }

        except StorageServiceError:
            raise

        except Exception as error:
            raise StorageServiceError(
                f"Unable to initiate multipart upload: {original_filename}"
            ) from error

    def get_multipart_part_url(
        self,
        *,
        object_key,
        upload_id,
        part_number,
        expires_in=600,
    ):
        """
        Generate a presigned URL for uploading a single multipart part.
        """

        if not object_key or not str(object_key).strip():
            raise StorageServiceError(
                "object_key is required."
            )

        if not upload_id or not str(upload_id).strip():
            raise StorageServiceError(
                "upload_id is required."
            )

        if not part_number:
            raise StorageServiceError(
                "part_number is required."
            )

        try:
            return self.provider.generate_part_upload_url(
                object_key=object_key,
                upload_id=upload_id,
                part_number=part_number,
                expires_in=expires_in,
            )

        except StorageServiceError:
            raise

        except Exception as error:
            raise StorageServiceError(
                f"Unable to generate part upload URL for: {object_key}"
            ) from error

    def complete_task_file_multipart_upload(
        self,
        *,
        object_key,
        upload_id,
        parts,
        task,
        uploaded_by_id,
        folder_type="reference",
        original_filename,
        stored_filename,
        is_final=False,
    ):
        """
        Complete a multipart upload for a task file.

        The parts are combined into the final object on R2, then
        a TaskFile metadata record is added to the current
        SQLAlchemy session.

        Database commit remains the caller's responsibility.
        """

        if task is None or task.id is None:
            raise StorageServiceError(
                "Task must be saved before uploading files."
            )

        if not uploaded_by_id:
            raise StorageServiceError(
                "Uploader ID is required."
            )

        if folder_type not in self.ALLOWED_FOLDER_TYPES:
            raise StorageServiceError(
                "Invalid task storage folder type."
            )

        if not object_key or not str(object_key).strip():
            raise StorageServiceError(
                "object_key is required."
            )

        if not upload_id or not str(upload_id).strip():
            raise StorageServiceError(
                "upload_id is required."
            )

        # Harden: the completed key must live under THIS task's own prefix and
        # folder (clients/.../TASK-<code>/<folder>/...), so a tampered client
        # can't register a TaskFile row pointing at an arbitrary object it
        # happens to hold a valid upload_id for.
        expected_segment = f"/TASK-{getattr(task, 'task_code', '')}/{folder_type}/"
        if not (str(object_key).startswith("clients/")
                and expected_segment in str(object_key)):
            raise StorageServiceError(
                "object_key does not match this task's storage location."
            )

        if not parts:
            raise StorageServiceError(
                "parts is required."
            )

        original_filename = str(
            original_filename or ""
        ).strip()

        if not original_filename:
            raise StorageServiceError(
                "Uploaded file must have a filename."
            )

        if not stored_filename or not str(stored_filename).strip():
            raise StorageServiceError(
                "stored_filename is required."
            )

        try:
            upload_result = self.provider.complete_multipart_upload(
                object_key=object_key,
                upload_id=upload_id,
                parts=parts,
            )

            task_file = TaskFile(
                task_id=task.id,
                bucket_name=upload_result["bucket_name"],
                storage_provider="r2",
                object_key=upload_result["object_key"],
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=upload_result.get("content_type"),
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

    def abort_task_file_multipart_upload(
        self,
        *,
        object_key,
        upload_id,
    ):
        """
        Abort an in-progress multipart upload for a task file.
        """

        if not object_key or not str(object_key).strip():
            raise StorageServiceError(
                "object_key is required."
            )

        if not upload_id or not str(upload_id).strip():
            raise StorageServiceError(
                "upload_id is required."
            )

        try:
            return self.provider.abort_multipart_upload(
                object_key=object_key,
                upload_id=upload_id,
            )

        except StorageServiceError:
            raise

        except Exception as error:
            raise StorageServiceError(
                f"Unable to abort multipart upload for: {object_key}"
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

    # ===========================
    # Client brand assets
    #
    # Permanent, client-level files (logo, brand imagery, video,
    # fonts, brand guidelines) - not scoped to a task or a date, so
    # the object key is deliberately flatter than
    # _build_task_object_key: clients/<slug>/brand-assets/<category>/
    # ===========================

    def _build_client_asset_object_key(
        self,
        *,
        client,
        category,
        stored_filename,
    ):
        if not client_asset_catalog.is_valid(category):
            raise StorageServiceError(
                "Invalid client asset category."
            )

        client_name = getattr(client, "client_name", None)

        if not client_name:
            raise StorageServiceError(
                "Client name could not be resolved."
            )

        return "/".join(
            [
                "clients",
                self._slugify_segment(client_name),
                "brand-assets",
                category,
                stored_filename,
            ]
        )

    def upload_client_asset(
        self,
        *,
        client,
        file_storage,
        uploaded_by_id,
        category,
    ):
        """
        Upload one Flask FileStorage object as a permanent client
        brand asset.

        A ClientAsset metadata record is added to the current
        SQLAlchemy session. Database commit remains the caller's
        responsibility.
        """

        if client is None or client.id is None:
            raise StorageServiceError(
                "Client must be saved before uploading assets."
            )

        if file_storage is None:
            raise StorageServiceError(
                "Uploaded file is required."
            )

        if not uploaded_by_id:
            raise StorageServiceError(
                "Uploader ID is required."
            )

        if not client_asset_catalog.is_valid(category):
            raise StorageServiceError(
                "Invalid client asset category."
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

        object_key = self._build_client_asset_object_key(
            client=client,
            category=category,
            stored_filename=stored_filename,
        )

        try:
            upload_result = self.upload(
                file_obj=file_storage.stream,
                object_key=object_key,
                content_type=file_storage.mimetype,
            )

            asset = ClientAsset(
                client_id=client.id,
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
                category=category,
                uploaded_by_id=uploaded_by_id,
            )

            db.session.add(
                asset
            )

            return {
                "asset": asset,
                "provider_metadata": upload_result,
            }

        except StorageServiceError:
            raise

        except Exception as error:
            raise StorageServiceError(
                f"Unable to save client asset: {original_filename}"
            ) from error