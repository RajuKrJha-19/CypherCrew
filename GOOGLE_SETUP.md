# YouTube + Google Business Profile — setup

The adapters are built. What remains is Google-side configuration, which
is where the real waiting is: **YouTube needs an OAuth verification, and
Business Profile needs a separate API access application.** Start both
early — the code is ready long before Google is.

Until `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set, both
platforms stay on the demo adapter, exactly as today. Nothing breaks by
deploying this before Google is ready.

---

## 1. Google Cloud project

1. [console.cloud.google.com](https://console.cloud.google.com) → create a
   project (e.g. *CypherCrew Social*).
2. **APIs & Services → Library**, enable:
   - **YouTube Data API v3**
   - **My Business Account Management API**
   - **My Business Business Information API**
   - **Google My Business API** ← the v4 host that serves local posts. It
     usually does not appear in the Library until access is granted (see
     step 4).

---

## 2. OAuth consent screen

**APIs & Services → OAuth consent screen**

- User type: **External**
- App name, support email, logo
- **App domain**: `crew.cypherms.com`
- **Privacy policy**: `https://crew.cypherms.com/legal/privacy`
- **Terms of service**: `https://crew.cypherms.com/legal/terms`
- **Authorised domain**: `cypherms.com`

Add these scopes:

```
https://www.googleapis.com/auth/youtube.upload
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/youtube.force-ssl
https://www.googleapis.com/auth/business.manage
```

`youtube.upload` and `youtube.force-ssl` are **sensitive/restricted**, so
Google runs its own verification — a demo video and a written
justification, much like Meta's app review. Submit it as soon as the
consent screen is filled in.

While verification is pending, add yourself under **Test users**; the flow
works for those accounts immediately.

---

## 3. OAuth client

**APIs & Services → Credentials → Create Credentials → OAuth client ID**

- Type: **Web application**
- Authorised redirect URIs — add both, exactly:

```
https://crew.cypherms.com/oauth/youtube/callback
https://crew.cypherms.com/oauth/google_business/callback
```

These are the app's real callback paths — the route is
`/oauth/<platform>/callback`, and the base comes from
`SOCIAL_PUBLIC_BASE_URL` when set, otherwise the incoming request host.

> **Set `SOCIAL_PUBLIC_BASE_URL=https://crew.cypherms.com`** in the
> environment. Behind CyberPanel's proxy the app can see an internal host
> instead of the public one, and it would then build a redirect URI Google
> has never heard of — which fails at consent time with a mismatch error
> that points nowhere useful.

Copy the **Client ID** and **Client secret**.

---

## 4. Business Profile API access (the long pole)

The v4 `localPosts` endpoint is not open by default. Apply here:

**[Google Business Profile APIs — request access](https://developers.google.com/my-business/content/prereqs)**

You submit the Cloud project number and a description of the use case.
Approval takes days to weeks. Until it lands, calls return 403, which the
adapter classifies as an auth error and surfaces as *"reconnect this
channel"* — so if Business Profile connects but every post fails with a
permission error, this application is what is missing, not the code.

---

## 5. Environment variables

On the server (`.env`), then restart:

```bash
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# Must match the domain in the redirect URIs above.
SOCIAL_PUBLIC_BASE_URL=https://crew.cypherms.com

# Optional - both default to true. Turn one off to keep it on the demo
# adapter while its approval is still pending.
YOUTUBE_ENABLED=true
GOOGLE_BUSINESS_ENABLED=true

# Applied to every upload.
YOUTUBE_CATEGORY_ID=22          # 22 = People & Blogs
YOUTUBE_PRIVACY_STATUS=public   # public | unlisted | private
YOUTUBE_MADE_FOR_KIDS=false     # Google requires this declaration
```

---

## 6. Connect and test

Studio → **Channels** → Connect. The Demo badge disappears once the real
adapter is registered.

Publish one video and one Business post before trusting a schedule. Watch
for these in particular:

- The first upload is the one that proves the resumable path works. A
  large file is a better test than a small one.
- Business Profile posts can come back `PROCESSING`; the queue keeps
  polling and only reports Published when Google says `LIVE`.

---

## Things worth knowing before you rely on it

**YouTube's upload quota is the real constraint.** A video upload costs
1600 units against a default project quota of 10,000/day — about **six
uploads a day**. The composer is configured with that limit. Ask Google
for a quota increase if the agency needs more; it is a form, and it takes
time. Exhausting the quota is treated as a rate-limit, so posts retry the
next day rather than failing.

**Google tokens expire in one hour.** Meta's Page tokens effectively never
do, so the engine used to treat a stored token as good forever. It now
refreshes just before use (`AccountManager.access_token`), which is what
makes a post scheduled for tomorrow morning work at all. This depends on a
refresh token, and Google only returns one when the consent screen is
actually shown — the adapter therefore forces `access_type=offline` and
`prompt=consent`, and refuses a connect that comes back without one rather
than storing a connection that dies in an hour.

**A YouTube caption is split.** The composer has one caption box; YouTube
wants a title and a description. The first line becomes the title, the
rest the description, hashtags appended.

**Business Profile posts to a *location*, not an account.** Each location
appears as its own channel. A business with three branches gets three
channels, and posts are per-branch.
