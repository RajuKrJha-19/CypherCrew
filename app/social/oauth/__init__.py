"""OAuth handshake orchestration (state/CSRF + PKCE) shared by all
providers. Provider-specific URL building and code exchange live in each
adapter; this layer only manages the handshake envelope."""
