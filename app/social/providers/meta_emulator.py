"""A faithful, local emulator of the Meta Graph API - just enough of the
endpoints the Facebook + Instagram adapters call, so the ENTIRE real
provider code path (OAuth login, token exchange, Page/IG discovery, the
container publish flow, insights) can be exercised in the browser and in
tests with no real Meta app.

Mounted at /mock/graph only when config.META_EMULATOR is on. Never in
production. The Meta provider's base URLs point here; it cannot tell the
difference between this and graph.facebook.com.
"""

from itertools import count

from flask import Blueprint, jsonify, redirect, request

from app.social.providers.meta_common import META_UNIFIED_SCOPES


meta_emulator_bp = Blueprint("meta_emulator", __name__, url_prefix="/mock/graph")

_seq = count(1)

# A fixed, believable graph: one Page with a linked IG business account.
_PAGE = {
    "id": "100000000000001",
    "name": "Demo Brand Page",
    "access_token": "EMU-PAGE-TOKEN",
    "tasks": ["ANALYZE", "ADVERTISE", "MESSAGING", "MODERATE", "CREATE_CONTENT", "MANAGE"],
    "instagram_business_account": {"id": "17800000000000001", "username": "demo_brand"},
}

#: The emulator stands in for a FULLY approved app, so it grants exactly
#: what the consent screen asks for. Derived from the one list rather than
#: copied: the hand-written copy went stale the moment the comment and
#: insights scopes were added, leaving dev mode unable to reproduce the
#: features that depend on them.
_GRANTED = list(META_UNIFIED_SCOPES)


@meta_emulator_bp.route("/<ver>/dialog/oauth")
def dialog_oauth(ver):
    """The consent screen - auto-approves and redirects back with a code."""
    redirect_uri = request.args.get("redirect_uri", "")
    state = request.args.get("state", "")
    sep = "&" if "?" in redirect_uri else "?"
    return redirect(f"{redirect_uri}{sep}code=EMU-AUTH-CODE&state={state}")


@meta_emulator_bp.route("/<ver>/oauth/access_token")
def access_token(ver):
    if request.args.get("grant_type") == "fb_exchange_token":
        return jsonify(access_token="EMU-LONGLIVED-USER-TOKEN",
                       token_type="bearer", expires_in=5184000)  # ~60d
    return jsonify(access_token="EMU-SHORTLIVED-USER-TOKEN",
                   token_type="bearer", expires_in=3600)


@meta_emulator_bp.route("/<ver>/me/permissions")
def me_permissions(ver):
    return jsonify(data=[{"permission": p, "status": "granted"} for p in _GRANTED])


@meta_emulator_bp.route("/<ver>/me/accounts")
def me_accounts(ver):
    return jsonify(data=[_PAGE], paging={})


@meta_emulator_bp.route("/<ver>/<node>/<edge>", methods=["GET", "POST"])
def node_edge(ver, node, edge):
    n = next(_seq)
    if edge == "feed":
        return jsonify(id=f"{node}_{n}")
    if edge == "photos":
        published = (request.form.get("published", "true") != "false")
        out = {"id": f"PHOTO_{n}"}
        if published:
            out["post_id"] = f"{node}_{n}"
        return jsonify(out)
    if edge == "videos":
        return jsonify(id=f"VIDEO_{n}")
    if edge == "video_reels":
        if request.form.get("upload_phase") == "start":
            vid = f"REEL_{n}"
            base = request.host_url.rstrip("/")
            return jsonify(video_id=vid, upload_url=f"{base}/mock/graph/rupload/{vid}")
        return jsonify(success=True)  # finish
    if edge == "video_stories":                    # FB video story (3-phase)
        if request.form.get("upload_phase") == "start":
            vid = f"STORY_{n}"
            base = request.host_url.rstrip("/")
            return jsonify(video_id=vid, upload_url=f"{base}/mock/graph/rupload/{vid}")
        return jsonify(success=True, post_id=f"{node}_{n}")  # finish
    if edge == "photo_stories":                    # FB photo story
        return jsonify(post_id=f"{node}_{n}", id=f"{node}_{n}")
    if edge == "media":
        return jsonify(id=f"CONTAINER_{n}")
    if edge == "media_publish":
        return jsonify(id=f"IGMEDIA_{n}")
    if edge == "comments":
        if request.method == "GET":                # Engage: read comments
            base = request.host_url.rstrip("/")
            return jsonify(data=[
                {"id": f"{node}_c1", "message": "Love this! 🔥 Where can I get it?",
                 "from": {"id": "user_aditya", "name": "Aditya Rao",
                          "picture": {"data": {
                              "url": f"{base}/mock/avatar/aditya"}}},
                 "created_time": "2026-07-26T10:05:00+0000"},
                {"id": f"{node}_c2", "message": "Great work team 👏",
                 "from": {"id": "user_neha", "name": "Neha Sharma"},
                 "created_time": "2026-07-26T11:32:00+0000"},
            ], paging={})
        return jsonify(id=f"COMMENT_{n}")          # POST: first comment / reply
    if edge == "replies":                          # IG reply to a comment
        return jsonify(id=f"REPLY_{n}")
    if edge == "insights":
        metrics = (request.args.get("metric") or "").split(",")
        return jsonify(data=[
            {"name": m, "period": "lifetime", "values": [{"value": 100 + i * 37}]}
            for i, m in enumerate(m for m in metrics if m)
        ])
    return jsonify(id=f"{node}_{edge}_{n}")


@meta_emulator_bp.route("/rupload/<video_id>", methods=["POST"])
def rupload(video_id):
    """Reels phase-2 hosted upload target. Accepts the file_url header."""
    return jsonify(success=True, video_id=video_id)


@meta_emulator_bp.route("/<ver>/<node>", methods=["GET", "DELETE"])
def node(ver, node):
    if request.method == "DELETE":                 # delete a published post
        return jsonify(success=True)
    raw_fields = request.args.get("fields", "") or ""
    fields = set(raw_fields.split(","))
    if "instagram_business_account" in raw_fields:  # Page -> linked IG (refresh)
        return jsonify(id=node, name=_PAGE["name"],
                       instagram_business_account=_PAGE["instagram_business_account"])
    if "status_code" in fields:                    # IG container
        return jsonify(id=node, status_code="FINISHED")
    if "status" in fields:                         # FB video/reel processing
        return jsonify(id=node, status={
            "video_status": "ready",
            "uploading_phase": {"status": "complete"},
            "processing_phase": {"status": "complete"},
            "publishing_phase": {"status": "complete", "publish_status": "published"},
        })
    if "permalink_url" in fields:                  # FB permalink
        return jsonify(id=node, permalink_url=f"https://www.facebook.com/{node}")
    if "permalink" in fields:                      # IG permalink
        return jsonify(id=node, permalink=f"https://www.instagram.com/p/{node}/")
    return jsonify(id=node)
