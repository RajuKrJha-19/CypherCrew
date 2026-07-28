# Meta App Review — what to configure for CypherCrew

Everything here is derived from what the code actually calls. If a
permission is listed, there is a Graph call in this repo that needs it; if
one is missing, that feature fails silently, which is exactly how the
first comment stayed broken for so long.

Current app state: **Manage messaging and content on Instagram** and
**Manage everything on your Facebook Page** are enabled.

---

## 1. What the app actually does

| Feature (Social Studio) | Graph call | Permission |
|---|---|---|
| List the Pages a person manages, on connect | `GET /me/accounts` | `pages_show_list`, `business_management` |
| Discover the Instagram account linked to a Page | `GET /{page-id}?fields=instagram_business_account` | `instagram_basic` |
| Identify who granted consent (for data deletion) | `GET /me?fields=id` | — (any user token) |
| Publish to a Facebook Page — text, photo, video, reel, carousel | `POST /{page-id}/feed`, `/photos`, `/videos`, `/video_reels` | `pages_manage_posts` |
| Delete a published Page post | `DELETE /{post-id}` | `pages_manage_posts` |
| Publish to Instagram — image, carousel, reel, story | `POST /{ig-id}/media`, `/{ig-id}/media_publish` | `instagram_content_publish`, `instagram_basic` |
| Auto first comment + Engage replies (Facebook) | `POST /{post-id}/comments` | `pages_manage_engagement` |
| Auto first comment + Engage replies (Instagram) | `POST /{media-id}/comments`, `/{comment-id}/replies` | `instagram_manage_comments` |
| Read comments into the Engage inbox (Facebook) | `GET /{post-id}/comments` | `pages_read_engagement` |
| Analytics — Facebook post insights | `GET /{post-id}/insights` | `read_insights` |
| Analytics — Instagram media insights | `GET /{media-id}/insights` | `instagram_manage_insights` |

The app does **not** read or send direct messages, does not access friends
lists, does not run ads, and does not use platform data for advertising or
model training.

**You can deploy before approval comes through.** Connect only hard-fails
when a *publishing* scope is missing — `_required_scopes()` on each
provider is deliberately just `pages_manage_posts` + `pages_show_list`
(Facebook) and `instagram_content_publish` + `instagram_basic`
(Instagram). The comment and insights scopes are requested but not
required, so channels keep connecting while review is pending; those
features start working the moment the permissions are granted and the
channel is reconnected.

---

## 2. Use cases to configure

### Keep — "Manage everything on your Facebook Page"

Add these permissions under it. The first three are almost certainly
already on; the last two are the ones that were missing:

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts`
- `pages_manage_engagement` ← needed for the first comment and Engage replies
- `read_insights` ← needed for Facebook analytics

### Keep — "Manage messaging and content on Instagram"

**Keep this use case. Do not remove it.** Despite the name, this is the
use case for **Instagram API with Facebook Login**, which is the flow this
app uses (an Instagram Business account linked to a Facebook Page,
published with that Page's token). Its permission bundle is:

`instagram_basic`, `instagram_content_publish`,
`instagram_manage_comments`, `instagram_manage_insights`,
`instagram_manage_messages`, `pages_show_list`, `pages_read_engagement`

Removing it takes `instagram_basic` with it, and Instagram then cannot be
connected at all. The name is simply broader than what we need — the same
bundle serves apps that do Instagram messaging.

Customise the use case and request only the four permissions the app
actually uses:

- `instagram_basic`
- `instagram_content_publish`
- `instagram_manage_comments`
- `instagram_manage_insights` ← add this; it was missing and Analytics is empty without it

**Leave `instagram_manage_messages` unrequested.** There is no messaging
code in this app, and asking for a permission you cannot demonstrate is a
common rejection. Say so explicitly in the submission notes: *"This app
does not use Instagram messaging; `instagram_manage_messages` is not
requested."*

> The newer `instagram_business_*` permission names belong to **Instagram
> API with Instagram Login**, a different flow where Instagram is
> authenticated directly rather than through a linked Facebook Page. Do not
> switch to it — this app publishes with the Page token, and tokens from
> that flow will not work here.

### Add — Business asset access

`business_management` is already requested by the code (it is needed to
enumerate assets owned through a Business portfolio). Make sure it is
declared, or connecting will fail for clients whose Pages sit inside a
Business Manager.

### Products to add

Per Meta's Instagram Platform overview, the Facebook Login flow needs these
products on the app:

- **Facebook Login for Business**
- **Instagram** → *Instagram API setup with Facebook login*
- **Messenger** (including Instagram settings) — required as a product by
  this flow even though we request no messaging permission

### Do NOT request

- `instagram_manage_messages` — no messaging code exists.
- Any `pages_messaging` permission — same reason.
- `pages_manage_metadata`, `pages_manage_ads`, `ads_management` — unused.
- `public_profile` beyond the default — not needed.

---

## 3. The three URLs

All three are live in this app, public, and need no login:

| Dashboard field | URL |
|---|---|
| Privacy Policy URL | `https://<your-domain>/legal/privacy` |
| Terms of Service URL | `https://<your-domain>/legal/terms` |
| User Data Deletion | see below |

For **User Data Deletion**, Meta accepts either a callback or an
instructions URL. This app implements **both**, and the callback is the
stronger option — use it:

- **Data Deletion Callback URL**: `https://<your-domain>/legal/data-deletion/callback`
- (or) **Data Deletion Instructions URL**: `https://<your-domain>/legal/data-deletion`

The callback verifies Meta's `signed_request` with `META_APP_SECRET`
(HMAC-SHA256, constant-time compare), deletes the requesting user's
channels and everything derived from them, and replies with the
`{url, confirmation_code}` pair Meta expects. **It cannot work without
`META_APP_SECRET` set** — it fails closed and returns 400 rather than
deleting on an unverifiable request.

Also set the **Deauthorize Callback URL** if the dashboard asks for one; if
you have no separate endpoint, point it at the data deletion instructions
page.

---

## 4. Other compliance to complete

- **Business verification** — required before Advanced Access is granted.
  Meta verifies the legal entity behind the app.
- **Verify the domain** in Business settings — required for the app to use
  it in API integrations.
- **Advanced Access, not Standard** — the app publishes on behalf of Pages
  and Instagram accounts that CypherCrew does not own (clients'), which
  requires Advanced Access for every publishing permission.
- **Data Use Checkup** — an annual re-confirmation that each permission is
  still used for the declared purpose. Diarise it; access is cut off if it
  lapses.
- **App icon, category, and a working app URL** — a reviewer that cannot
  reach the app rejects the submission.
- **Test users** — while the app is in development mode, only accounts
  listed as testers can connect. Add the reviewer flow accounts.
- **HTTPS everywhere** — the privacy policy in particular must be served
  over a certificate from a trusted CA.

---

## 5. Justification text you can adapt per permission

Reviews are rejected for vague use cases. Each of these says what the app
does, for whom, and why the permission is the minimum needed.

**`pages_manage_posts`**
> CypherCrew is a work-management tool used by a marketing agency to
> publish content for its clients. Agency staff create and approve a post
> inside CypherCrew, and on approval the app publishes it to the client's
> Facebook Page at the scheduled time. This permission is used only to
> create and delete posts the client's team has authored and approved in
> the app. Nothing is posted automatically without human approval.

**`pages_manage_engagement`**
> Used for two features. First, an optional "first comment" that the author
> writes alongside the post and the app publishes as a comment immediately
> after the post goes live — the standard practice of keeping hashtags or a
> link out of the caption. Second, the Engage inbox, where the client's team
> reads audience comments on their own posts and replies to them from
> CypherCrew. We do not comment on Pages our users do not manage.

**`read_insights`**
> Used to show the client how their posts performed. The app reads
> impressions, reach and engagement counts for posts it published to the
> client's Page and displays them on an analytics screen and in reports.
> The data is not used for advertising or profiling.

**`instagram_basic`**
> Used to identify the Instagram Business account linked to a Facebook Page
> the user manages, and to display its username in the app so a person can
> confirm they are publishing to the right account.

**`instagram_content_publish`**
> Agency staff create and approve Instagram content in CypherCrew — images,
> carousels, reels and stories — and on approval the app publishes it to the
> client's Instagram Business account at the scheduled time. Every post is
> authored and approved by a person inside the app before it is published.

**`instagram_manage_comments`**
> Same two features as on Facebook: an optional first comment published
> immediately after the post goes live, and the Engage inbox where the
> client's team reads and replies to comments on their own posts.

**`instagram_manage_insights`**
> Used to report performance back to the client — impressions, reach, likes,
> comments and saves for posts the app published to their account. Not used
> for advertising or profiling.

---

## 6. What the reviewer should be shown

Meta's dashboard now lists the approval criteria per permission, and the
screen-recording upload requirement was dropped in 2026 — but a clear
walkthrough still helps. Cover the whole journey:

1. Sign in to CypherCrew.
2. Channels → **Connect a channel** → the Facebook consent screen → the
   Page and its linked Instagram account appearing as connected.
3. Create post → write a caption, attach media, add a first comment, pick
   the client's channels → **Save & submit**.
4. Approvals → approve → the post publishes → the post page shows the live
   permalink and the **First comment: Posted** confirmation.
5. Engage → a comment on that post, and a reply sent from the app.
6. Analytics → the insights for that post.
7. Legal → the privacy policy, terms, and the data-deletion page, and the
   confirmation code a deletion request returns.

---

## 7. Configuration checklist for the deployment

```bash
META_APP_ID=...
META_APP_SECRET=...          # required, or the deletion callback fails closed
SOCIAL_TOKEN_KEY=...         # Fernet key; tokens are encrypted at rest
LEGAL_COMPANY_NAME="..."     # the legal entity named on the policy pages
LEGAL_CONTACT_EMAIL="..."    # a monitored inbox
LEGAL_LAST_UPDATED="..."     # date shown on the legal pages
```

After deploying, confirm all three URLs return **200 while signed out** —
a redirect to `/login` is an automatic rejection:

```bash
curl -sI https://<your-domain>/legal/privacy       | head -1
curl -sI https://<your-domain>/legal/terms         | head -1
curl -sI https://<your-domain>/legal/data-deletion | head -1
```

> **Reconnect existing channels after deploying.** The new comment and
> insights permissions are only granted at consent time. Tokens already
> stored were issued without them, so the first comment and analytics stay
> broken on those channels until each one is disconnected and connected
> again.
