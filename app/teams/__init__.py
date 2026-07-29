"""Cypher-Teams: chat and meetings for the people who run the agency.

Layout mirrors the social engine, for the same reason it works there:

    services/   business logic. Routes call these; they never import a
                provider or touch a template.
    providers/  meeting backends behind one interface, so swapping Jitsi
                for LiveKit is a new file rather than a rewrite.

The one architectural rule: services may depend on models and on the
MeetingProvider interface, never on a concrete provider.

There is no realtime transport here. Chat is polled - one endpoint, one
cursor, a cadence that slows down as attention drifts (see
routes/teams.py:sync and static/js/teams-chat.js). The payload is shaped so
that putting a push transport in front of it later changes the delivery,
not the contract.
"""
