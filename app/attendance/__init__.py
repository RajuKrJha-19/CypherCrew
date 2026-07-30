"""Attendance module: the Zoho People bridge + idle-task alerts.

Self-contained like app/social: nothing here is imported unless
ATTENDANCE_ENABLED is on (see app/__init__.py). Business logic (tasks,
approvals) is untouched - the only cross-read is "does this user have a
task in progress", which the idle-alert service asks read-only.
"""
