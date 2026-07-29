"""Jitsi Meet.

Chosen because it needs no account, no API key and no per-minute billing -
meetings work the day the module ships. The cost of that is the thing
worth being explicit about:

    On the public meet.jit.si the ROOM NAME IS THE AUTHORISATION.

There is no server-side check that the person joining is invited; anybody
holding the room name can walk in. Two things follow, and both are
deliberate:

  * The room name is derived from Meeting.room_key - 24 bytes of
    `secrets.token_urlsafe`, never from the meeting id, title or date. A
    guessable room is an open room.
  * The room name never appears in a URL path. It goes into the embed's
    JavaScript config, so it does not land in browser history, in a
    Referer header, or in a screenshot of the address bar.

The lobby (`TEAMS_JITSI_LOBBY`, on by default) is the mitigation for what
remains: someone who learns the name still has to be admitted.

For a real access check, point TEAMS_JITSI_DOMAIN at a self-hosted Jitsi
and set the JWT pair - the token path below activates on its own.
"""

import secrets
import time

from flask import current_app

from app.teams.providers.base import MeetingProvider

#: 24 bytes -> a 32-character url-safe string. Not a round number chosen
#: for looks: it is the point past which guessing a live room is not worth
#: anyone's time.
ROOM_KEY_BYTES = 24

#: Jitsi room names are case-insensitive on some deployments and appear in
#: the UI, so the key gets a readable prefix rather than standing alone.
ROOM_PREFIX = "cyphercrew-"


def new_room_key():
    return secrets.token_urlsafe(ROOM_KEY_BYTES)


class JitsiProvider(MeetingProvider):
    key = "jitsi"
    display_name = "Jitsi Meet"
    supports_embed = True
    #: The public instance cannot record. A self-hosted one with Jibri can,
    #: but that is a deployment fact this adapter cannot detect.
    supports_recording = False

    # -- config ---------------------------------------------------------

    @property
    def domain(self):
        return current_app.config.get("TEAMS_JITSI_DOMAIN", "meet.jit.si")

    @property
    def lobby_enabled(self):
        return bool(current_app.config.get("TEAMS_JITSI_LOBBY", True))

    @property
    def _jwt_configured(self):
        return bool(
            current_app.config.get("TEAMS_JITSI_APP_ID")
            and current_app.config.get("TEAMS_JITSI_JWT_SECRET")
        )

    # -- interface ------------------------------------------------------

    def room_name(self, meeting):
        return f"{ROOM_PREFIX}{meeting.room_key}"

    def join_url(self, meeting, user):
        """Full-page fallback. Carries the display name in the fragment,
        which never reaches the server."""
        name = (getattr(user, "name", "") or "").strip()
        url = f"https://{self.domain}/{self.room_name(meeting)}"
        if name:
            from urllib.parse import quote
            url += f"#userInfo.displayName=%22{quote(name)}%22"
        return url

    def embed_config(self, meeting, user, moderator=False):
        """Options handed to JitsiMeetExternalAPI in the browser."""
        config = {
            "domain": self.domain,
            "roomName": self.room_name(meeting),
            "userInfo": {
                "displayName": getattr(user, "name", "") or "Guest",
                "email": getattr(user, "email", "") or "",
            },
            "configOverwrite": {
                # Everyone lands muted. Joining a standup and blasting your
                # keyboard into it is the default this avoids.
                "startWithAudioMuted": True,
                "startWithVideoMuted": True,
                # Kept ON. It is the only place someone can pick the right
                # microphone and see themselves before walking into a
                # client call - and we have no screen of our own that does
                # that. (meet.jit.si shows it regardless, so turning it off
                # bought a config line and no behaviour.)
                "prejoinPageEnabled": True,
                # Without this, mobile browsers try to bounce the user into
                # the Jitsi app and the embed never loads.
                "disableDeepLinking": True,
                "enableLobbyChat": False,
                "lobby": {"enabled": self.lobby_enabled},
            },
            "interfaceConfigOverwrite": {
                "SHOW_JITSI_WATERMARK": False,
                "SHOW_BRAND_WATERMARK": False,
                "MOBILE_APP_PROMO": False,
                "TOOLBAR_BUTTONS": [
                    "microphone", "camera", "desktop", "chat",
                    "raisehand", "participants-pane", "tileview",
                    "toggle-camera", "fullscreen", "settings", "hangup",
                ],
            },
        }

        token = self._jwt(meeting, user, moderator)
        if token:
            config["jwt"] = token

        return config

    # -- internals ------------------------------------------------------

    def _jwt(self, meeting, user, moderator):
        """A signed token, when a self-hosted Jitsi is configured.

        Returns None on the public instance, which accepts no token - and
        None on any signing failure, because a meeting that opens without
        moderator rights is far better than a meeting that will not open.
        """
        if not self._jwt_configured:
            return None

        try:
            import jwt as pyjwt
        except ImportError:
            current_app.logger.warning(
                "TEAMS_JITSI_JWT_SECRET is set but PyJWT is not installed - "
                "joining without a token.")
            return None

        app_id = current_app.config["TEAMS_JITSI_APP_ID"]
        now = int(time.time())

        try:
            return pyjwt.encode(
                {
                    "aud": app_id,
                    "iss": app_id,
                    "sub": self.domain,
                    "room": self.room_name(meeting),
                    "exp": now + 4 * 60 * 60,
                    "nbf": now - 30,
                    "context": {
                        "user": {
                            "name": getattr(user, "name", "") or "Guest",
                            "email": getattr(user, "email", "") or "",
                            "moderator": bool(moderator),
                        },
                    },
                },
                current_app.config["TEAMS_JITSI_JWT_SECRET"],
                algorithm="HS256",
            )
        except Exception:                                    # noqa: BLE001
            current_app.logger.exception("[teams-meeting] JWT signing failed")
            return None
