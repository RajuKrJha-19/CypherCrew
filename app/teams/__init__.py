"""Cypher-Teams: chat for the people who run the agency.

Chat only. Meetings were removed along with the Jitsi adapter and the
provider registry that fronted it - `/meetings` is the old module's own
again, and nothing here schedules or joins a call.

Layout mirrors the social engine, for the same reason it works there:

    services/   business logic. Routes call these; they never touch a
                template.

The one architectural rule: services may depend on models, never the
other way round, and never on a route.

There is no realtime transport here. Chat is polled - one endpoint, one
cursor, a cadence that slows down as attention drifts (see
routes/teams.py:sync and static/js/teams-chat.js). The payload is shaped so
that putting a push transport in front of it later changes the delivery,
not the contract.
"""
