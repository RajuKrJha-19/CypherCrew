"""R2 storage maintenance - opt-in, run manually. Never runs at app boot.

Addresses two audit items that are infrastructure, not request-path code:

  * abandoned multipart uploads leave billable parts in R2 if the browser
    dies after `initiate` (no server-side reaper) - `apply-lifecycle` installs
    a bucket rule that aborts incomplete multipart uploads after N days.
  * a post-commit R2 delete that fails leaves an object with no DB row -
    `find-orphans` reports (dry-run) R2 keys under our prefixes that no DB
    row references, so they can be reviewed and reclaimed.

Usage (from the repo root, with the app's .env in place):

    python scripts/r2_maintenance.py apply-lifecycle [--days 7]
    python scripts/r2_maintenance.py find-orphans [--prefix clients/] [--delete]

`find-orphans` is a DRY RUN by default and only deletes with an explicit
--delete flag. Review the report first.
"""
import argparse
import sys

from app import create_app
from app.extensions import db
from app.storage.r2_provider import R2Provider


PREFIXES = ["clients/", "thumbnails/", "social_uploads/", "client_assets/"]


def _client_and_bucket():
    p = R2Provider()
    return p.client, p.bucket_name


def apply_lifecycle(days):
    client, bucket = _client_and_bucket()
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [{
                "ID": "abort-incomplete-multipart-uploads",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": days},
            }],
        },
    )
    print(f"Applied lifecycle rule: abort incomplete multipart uploads after "
          f"{days} day(s) on bucket '{bucket}'.")


def _referenced_keys():
    """Every object key any DB row points at."""
    from app.models import TaskFile
    keys = set()
    for object_key, thumbnail_key in db.session.query(
            TaskFile.object_key, TaskFile.thumbnail_key).all():
        if object_key:
            keys.add(object_key)
        if thumbnail_key:
            keys.add(thumbnail_key)
    try:
        from app.models import ClientAsset
        for (k,) in db.session.query(ClientAsset.object_key).all():
            if k:
                keys.add(k)
    except Exception:  # noqa: BLE001 - optional model
        pass
    try:
        from app.models import SocialMediaAsset
        for (k,) in db.session.query(SocialMediaAsset.object_key).all():
            if k:
                keys.add(k)
    except Exception:  # noqa: BLE001 - engine may be off
        pass
    return keys


def find_orphans(prefix, do_delete):
    client, bucket = _client_and_bucket()
    referenced = _referenced_keys()
    prefixes = [prefix] if prefix else PREFIXES
    orphans, total = [], 0
    paginator = client.get_paginator("list_objects_v2")
    for pfx in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=pfx):
            for obj in page.get("Contents", []) or []:
                total += 1
                if obj["Key"] not in referenced:
                    orphans.append(obj["Key"])
    print(f"Scanned {total} object(s) under {prefixes}. "
          f"{len(orphans)} not referenced by any DB row.")
    for k in orphans:
        print(("DELETE " if do_delete else "orphan ") + k)
    if do_delete and orphans:
        for k in orphans:
            client.delete_object(Bucket=bucket, Key=k)
        print(f"Deleted {len(orphans)} orphaned object(s).")
    elif orphans:
        print("Dry run - re-run with --delete to remove these.")


def main():
    ap = argparse.ArgumentParser(description="R2 storage maintenance")
    sub = ap.add_subparsers(dest="command", required=True)
    p_life = sub.add_parser("apply-lifecycle")
    p_life.add_argument("--days", type=int, default=7)
    p_orph = sub.add_parser("find-orphans")
    p_orph.add_argument("--prefix", default="")
    p_orph.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        if args.command == "apply-lifecycle":
            apply_lifecycle(args.days)
        elif args.command == "find-orphans":
            find_orphans(args.prefix, args.delete)
        else:
            ap.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
