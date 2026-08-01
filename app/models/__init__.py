from app.models.user import User

from app.models.permission import Permission

from app.models.user_permission import UserPermission

from app.models.client import Client

from app.models.service import Service

from app.models.client_target import (
    ClientMonthlyTarget,
    ClientDeliverable
)

from app.models.task import Task, task_visibility

from app.models.daily_report import (
    DailyReport,
    ActivityLog
)
from app.models.task_feedback import TaskFeedback
from app.models.ai_check import AICheck
from app.models.ai_settings import AISettings
from app.models.notification import Notification

from app.models.task_activity import TaskActivity

from .holiday import Holiday
from .meeting import Meeting
from .leave import Leave

from .task_sequence import TaskSequence

from app.models.task_comment import (
    TaskComment,
    TaskCommentReaction
)

from app.models.note import (
    Note,
    NoteLabel,
    NoteAttachment
)
from app.models.task_file import TaskFile
from app.models.client_asset import ClientAsset
from app.models.task_transfer import TaskTransferRequest

# --- Social Publishing Engine ---
from app.models.social_account import SocialAccount
from app.models.social_oauth import SocialOAuthState
from app.models.social_post import (
    SocialPost,
    SocialPostTarget,
    SocialMediaAsset,
    SocialHashtagSet,
)
from app.models.publish_job import PublishJob, PublishResult
from app.models.social_comment import SocialComment
from app.models.social_posting_slot import SocialPostingSlot
from app.models.social_analytics import SocialAnalyticsSnapshot
from app.models.social_audit import SocialAuditLog, ContentVersion
from app.models.platform_rate_budget import PlatformRateBudget
from app.models.data_deletion import DataDeletionRequest

# --- Attendance (Zoho People bridge + idle-task alerts) ---
from app.models.zoho_connection import ZohoConnection
from app.models.attendance_session import AttendanceSession
from app.models.attendance_settings import AttendanceSettings

# --- Cypher-Teams ---
from app.models.team_channel import TeamChannel, TeamChannelMember
from app.models.team_message import (
    TeamMessage,
    TeamAttachment,
    TeamReaction,
)
from app.models.team_presence import TeamPresence, TeamTyping
from app.models.team_saved import TeamSavedMessage