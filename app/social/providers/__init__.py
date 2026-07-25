"""Platform adapters. Each module defines one SocialProvider subclass and
registers it with the shared registry (app.social.registry) on import.

The concrete adapters (meta_instagram, meta_facebook, linkedin, youtube)
are added per phase; this package is intentionally empty of platform code
until then. Business logic never imports these modules directly - it goes
through the registry.
"""
