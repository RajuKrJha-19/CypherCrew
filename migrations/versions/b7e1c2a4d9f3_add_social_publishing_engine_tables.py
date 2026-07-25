"""add social publishing engine tables

Revision ID: b7e1c2a4d9f3
Revises: f4afabbda8a4
Create Date: 2026-07-26 10:00:00.000000

Phase 0 of the Social Publishing Engine. Purely additive: eleven new
tables, no changes to any existing table. Everything stays dormant until
SOCIAL_ENGINE_ENABLED is turned on.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'b7e1c2a4d9f3'
down_revision = 'f4afabbda8a4'
branch_labels = None
depends_on = None


def upgrade():
    # -- social_accounts ---------------------------------------------------
    op.create_table(
        'social_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=30), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('account_type', sa.String(length=30), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('scopes', sa.Text(), nullable=True),
        sa.Column('token_ciphertext', sa.Text(), nullable=True),
        sa.Column('token_key_version', sa.Integer(), nullable=False),
        sa.Column('refresh_ciphertext', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('refresh_expires_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('connected_by_id', sa.Integer(), nullable=True),
        sa.Column('meta', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['connected_by_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'platform', 'external_id',
            name='uq_social_account_platform_external',
        ),
    )
    with op.batch_alter_table('social_accounts', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_accounts_platform'),
            ['platform'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_social_accounts_client_id'),
            ['client_id'], unique=False,
        )

    # -- social_oauth_states ----------------------------------------------
    op.create_table(
        'social_oauth_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=128), nullable=False),
        sa.Column('platform', sa.String(length=30), nullable=False),
        sa.Column('code_verifier', sa.String(length=255), nullable=True),
        sa.Column('redirect_uri', sa.String(length=500), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_oauth_states', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_oauth_states_state'),
            ['state'], unique=True,
        )

    # -- social_posts ------------------------------------------------------
    op.create_table(
        'social_posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('base_caption', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('approved_by_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_posts', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_posts_task_id'), ['task_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_social_posts_client_id'), ['client_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_social_posts_status'), ['status'], unique=False)

    # -- social_post_targets ----------------------------------------------
    op.create_table(
        'social_post_targets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('social_post_id', sa.Integer(), nullable=False),
        sa.Column('social_account_id', sa.Integer(), nullable=True),
        sa.Column('platform', sa.String(length=30), nullable=False),
        sa.Column('post_type', sa.String(length=30), nullable=False),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('hashtags', sa.Text(), nullable=True),
        sa.Column('scheduled_for', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('external_post_id', sa.String(length=255), nullable=True),
        sa.Column('permalink', sa.String(length=500), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('ai_generated', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['social_post_id'], ['social_posts.id'], ),
        sa.ForeignKeyConstraint(['social_account_id'], ['social_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_post_targets', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_post_targets_social_post_id'),
            ['social_post_id'], unique=False)
        batch_op.create_index(
            'ix_social_post_targets_sched_status',
            ['scheduled_for', 'status'], unique=False)

    # -- social_media_assets ----------------------------------------------
    op.create_table(
        'social_media_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('social_post_id', sa.Integer(), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('task_file_id', sa.Integer(), nullable=True),
        sa.Column('client_asset_id', sa.Integer(), nullable=True),
        sa.Column('object_key', sa.String(length=1000), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('alt_text', sa.Text(), nullable=True),
        sa.Column('mime_type', sa.String(length=150), nullable=True),
        sa.Column('meta', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['social_post_id'], ['social_posts.id'], ),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.ForeignKeyConstraint(['task_file_id'], ['task_files.id'], ),
        sa.ForeignKeyConstraint(['client_asset_id'], ['client_assets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_media_assets', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_media_assets_social_post_id'),
            ['social_post_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_social_media_assets_target_id'),
            ['target_id'], unique=False)

    # -- publish_jobs ------------------------------------------------------
    op.create_table(
        'publish_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(), nullable=False),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=64), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=True),
        sa.Column('provider_state', postgresql.JSONB(), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'idempotency_key', name='uq_publish_jobs_idempotency_key'),
    )
    with op.batch_alter_table('publish_jobs', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_publish_jobs_target_id'),
            ['target_id'], unique=False)
        batch_op.create_index(
            'ix_publish_jobs_state_next_run',
            ['state', 'next_run_at'], unique=False)

    # -- publish_results ---------------------------------------------------
    op.create_table(
        'publish_results',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('external_post_id', sa.String(length=255), nullable=True),
        sa.Column('permalink', sa.String(length=500), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('raw_response', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('publish_results', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_publish_results_target_id'),
            ['target_id'], unique=False)

    # -- social_analytics --------------------------------------------------
    op.create_table(
        'social_analytics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('external_post_id', sa.String(length=255), nullable=True),
        sa.Column('metrics', postgresql.JSONB(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_analytics', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_analytics_target_id'),
            ['target_id'], unique=False)
        batch_op.create_index(
            'ix_social_analytics_target_fetched',
            ['target_id', 'fetched_at'], unique=False)

    # -- social_audit_logs -------------------------------------------------
    op.create_table(
        'social_audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('post_id', sa.Integer(), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('detail', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['account_id'], ['social_accounts.id'], ),
        sa.ForeignKeyConstraint(['post_id'], ['social_posts.id'], ),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('social_audit_logs', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_social_audit_logs_created_at'),
            ['created_at'], unique=False)

    # -- content_versions --------------------------------------------------
    op.create_table(
        'content_versions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('social_post_id', sa.Integer(), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('snapshot', postgresql.JSONB(), nullable=True),
        sa.Column('edited_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['social_post_id'], ['social_posts.id'], ),
        sa.ForeignKeyConstraint(['target_id'], ['social_post_targets.id'], ),
        sa.ForeignKeyConstraint(['edited_by_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('content_versions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_content_versions_social_post_id'),
            ['social_post_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_content_versions_target_id'),
            ['target_id'], unique=False)

    # -- platform_rate_budgets --------------------------------------------
    op.create_table(
        'platform_rate_budgets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('social_account_id', sa.Integer(), nullable=False),
        sa.Column('rate_window', sa.String(length=20), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('used_count', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['social_account_id'], ['social_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'social_account_id', 'rate_window',
            name='uq_rate_budget_account_window'),
    )
    with op.batch_alter_table('platform_rate_budgets', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_platform_rate_budgets_social_account_id'),
            ['social_account_id'], unique=False)


def downgrade():
    op.drop_table('platform_rate_budgets')
    op.drop_table('content_versions')
    op.drop_table('social_audit_logs')
    op.drop_table('social_analytics')
    op.drop_table('publish_results')
    op.drop_table('publish_jobs')
    op.drop_table('social_media_assets')
    op.drop_table('social_post_targets')
    op.drop_table('social_posts')
    op.drop_table('social_oauth_states')
    op.drop_table('social_accounts')
