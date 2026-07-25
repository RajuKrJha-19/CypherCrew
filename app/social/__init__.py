"""Social Publishing Engine.

A modular, provider-isolated engine that authenticates to social platforms,
schedules and publishes content, retries on failure, and syncs analytics.

Design rules (enforced by structure):
- Business logic (services/) depends ONLY on the SocialProvider interface
  (providers/base.py) and the platform-agnostic DTOs (dto.py). No
  platform-specific code leaks upward.
- Each platform is an independent adapter under providers/, registered in
  registry.py. Adding a platform = one adapter + one registry entry.
- The whole engine is gated by config.SOCIAL_ENGINE_ENABLED; with it off,
  nothing here is wired into request handling.

See the architecture plan for the full design.
"""
