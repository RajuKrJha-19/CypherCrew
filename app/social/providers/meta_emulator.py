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

_GRANTED = [
    "pages_show_list", "pages_read_engagement", "pages_manage_posts",
    "business_management", "instagram_basic", "instagram_content_publish",
]


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
    if edge == "media":
        return jsonify(id=f"CONTAINER_{n}")
    if edge == "media_publish":
        return jsonify(id=f"IGMEDIA_{n}")
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


@meta_emulator_bp.route("/<ver>/<node>", methods=["GET"])
def node(ver, node):
    fields = set((request.args.get("fields", "") or "").split(","))
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
