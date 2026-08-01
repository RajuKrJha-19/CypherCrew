"""Provider-agnostic AI assist layer.

Mirrors app/social/: a provider interface (base.py), a registry that resolves
the configured backend (registry.py), and a service façade the routes call
(service.py). Backends live under providers/. Everything is inert until
AI_ENABLED is on; in AI_SIMULATION_MODE the SimulationProvider returns scripted
output so localhost + tests never touch a real provider or the network.
"""
