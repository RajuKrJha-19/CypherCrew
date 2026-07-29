"""Meeting backends, behind one interface.

Same shape as app/social/providers: business logic depends on
MeetingProvider and never imports a concrete adapter, so adding LiveKit or
Daily is a new file plus one line in `load_meeting_providers`.

Only Jitsi ships. It is here rather than hard-coded because the thing most
likely to change about a meeting is where the call actually happens - and
because the public meet.jit.si has a real limitation (no server-side
authorisation) that a self-hosted deployment or a different provider
fixes. When that day comes it should be a config change, not a rewrite.
"""
