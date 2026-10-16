# Authentication

How the frontend should actually implement sign-up, login, Google sign-in,
staying logged in, and logout. `docs/api-endpoints.md` has the exact
request/response shapes for every call mentioned here — this doc is the flow
and the reasoning, that one is the reference. Between the two, this is
everything the frontend needs to implement.

## Token model

Two JWTs, returned together by register/login/google/refresh:

- **Access token** — lives 30 minutes. Send it as `Authorization: Bearer <access>`
  on every authenticated request.
- **Refresh token** — lives 14 days. Used only to get a new access token from
  `POST /api/auth/token/refresh/`.

**Refresh tokens rotate.** Every call to `/token/refresh/` blacklists the
refresh token you sent and returns a brand new one. You must overwrite your
stored refresh token with the new one every time — reusing an old one (even
if it hasn't expired yet) fails with `401`. This is a deliberate security
property (a stolen refresh token stops working the moment the real user's
client refreshes), not a bug to work around.

There is no server-side access-token revocation. Logout blacklists the
refresh token, but an access token issued before logout keeps working until
it naturally expires (up to 30 minutes later). This is a standard JWT
tradeoff, not something to fix — just don't expect logout to be instant
everywhere.

## Sign-up and login (email + password)

`POST /api/auth/register/` and `POST /api/auth/login/` — see
`api-endpoints.md` for bodies. Both return `{access, refresh, user}` on
success. No email verification step exists yet — a registered account is
immediately usable; don't build UI that assumes verification is required.

## Sign-up allowlist

This app is invite-only, not open signup. `POST /api/auth/register/` and
`POST /api/auth/google/` (first-time only, see below) both check the email
against `AllowedEmail` (`accounts/models.py`) and reject with `400`/`401`
respectively if there's no matching row. There's no self-serve "request an
invite" flow — an operator adds a row at `/admin/accounts/allowedemail/`
(email + an optional note, e.g. "friend from work"), and that person can
then register immediately, no deploy needed.

The frontend's job is just to surface the rejection cleanly — see "Error
handling summary" below — not to implement any allowlist logic itself.

**The check only ever applies to creating a brand-new account.** It never
re-checks an email that already has one: logging in, refreshing tokens, and
linking Google sign-in to an existing email/password account (or vice
versa) all keep working even if that email's `AllowedEmail` row is later
removed. Removing a row only prevents *future* signups with that email — it
is not a way to deactivate an existing account (there's no such mechanism
in this API yet; that would go through Django admin, flipping `is_active`).

## Google sign-in

The frontend owns the Google redirect/prompt UX and only hands the backend a
token to verify — the backend never talks to Google's OAuth *authorization*
endpoints directly, only its token-verification one.

**1. Load Google Identity Services and get a client ID.**

```html
<script src="https://accounts.google.com/gsi/client" async defer></script>
```

Get an OAuth 2.0 Client ID from the Google Cloud Console (APIs & Services →
Credentials → OAuth client ID → Web application), with the frontend's origin
(e.g. `http://localhost:3000`) registered as an Authorized JavaScript origin.
This same Client ID must be set as `GOOGLE_OAUTH_CLIENT_ID` in the backend's
`.env` — the backend checks that any token it's handed was issued *for this
specific client ID*, so a mismatch here fails every Google login with
`401 Invalid Google token`.

**2. Get an ID token in the browser.**

```js
google.accounts.id.initialize({
  client_id: GOOGLE_CLIENT_ID,
  callback: async (response) => {
    // response.credential is the ID token (a JWT) -- send it as-is.
    const res = await fetch('/api/auth/google/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_token: response.credential }),
    });
    const { access, refresh, user } = await res.json();
    // store tokens, hydrate user state -- same as after a normal login
  },
});
google.accounts.id.renderButton(document.getElementById('google-btn'), { theme: 'outline', size: 'large' });
// or, for One Tap instead of a button:
// google.accounts.id.prompt();
```

**3. POST that token to `/api/auth/google/`.** The backend verifies it with
Google, extracts the email/name/subject ID, and finds-or-creates the user.
The response is the same `{access, refresh, user}` shape as email/password
login — from here on, the frontend doesn't need to know or care which method
the user signed in with.

**Account linking:** if someone signs up with `alex@example.com` / a password,
then later hits "Sign in with Google" using the same Google account (same
email), the backend links the Google identity to that *same* existing user
rather than creating a second account. Both sign-in methods work for that
user going forward. There's no "this email is already registered, please
sign in differently" error to handle — it just works.

## Storing tokens

The backend returns both tokens in the JSON response body (no cookies are
set). Given that, the pragmatic recommendation:

- **Access token: keep in memory** (a JS variable / app state), not
  persisted. It's short-lived, and not persisting it means it can't leak via
  storage inspection or a stale tab.
- **Refresh token: `localStorage`.** This is what makes staying logged in
  across page reloads / browser restarts possible without a backend change.
  The realistic downside is XSS — any injected script can read `localStorage`
  and mint new access tokens with the stolen refresh token, so this is a
  tradeoff, not a free lunch. Standard mitigation is the usual XSS hygiene
  (don't render untrusted HTML, CSP headers) rather than trying to avoid
  `localStorage` entirely — a fully cookie-based, XSS-hardened setup would
  need the backend to set `httpOnly` cookies instead of returning tokens in
  the body, which it currently doesn't do. Fine for now; revisit if/when this
  handles anything more sensitive than research notes.

## Making authenticated requests, with auto-refresh

On every request: send `Authorization: Bearer <access_token>`. On `401`, try
one silent refresh and retry the request once before giving up. Rough shape:

```js
let accessToken = null; // in memory

async function apiFetch(path, options = {}) {
  const doFetch = () => fetch(path, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
  });

  let res = await doFetch();
  if (res.status === 401 && accessToken !== null) {
    const refreshed = await tryRefresh();
    if (refreshed) res = await doFetch();
  }
  return res;
}

async function tryRefresh() {
  const refresh = localStorage.getItem('refresh');
  if (!refresh) return false;
  const res = await fetch('/api/auth/token/refresh/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh }),
  });
  if (!res.ok) {
    localStorage.removeItem('refresh');
    accessToken = null;
    return false; // caller should redirect to login
  }
  const data = await res.json();
  accessToken = data.access;
  localStorage.setItem('refresh', data.refresh); // rotated -- must overwrite
  return true;
}
```

**On app load:** if `localStorage` has a refresh token, call `tryRefresh()`
to get a fresh access token silently, then `GET /api/auth/me/` to hydrate
user state, before showing the logged-in UI. If there's no refresh token, or
`tryRefresh()` fails, show the logged-out state.

## Logout

`POST /api/auth/logout/` with the refresh token in the body (and a valid
access token in the `Authorization` header). On success: clear the in-memory
access token and remove the refresh token from `localStorage`, then show the
logged-out UI. See the token model note above — the access token isn't
server-side revoked, so don't rely on logout as a hard security boundary
within its remaining lifetime.

## Error handling summary

- `400` from `register/` (missing field, or email not on the sign-up
  allowlist) — show the field-level message from the response next to the
  relevant input; the allowlist rejection lands on the `email` field like
  any other validation error, so no special-casing needed.
- `401` from `login/` — generic "invalid email or password," don't reveal
  which one was wrong.
- `401` from `google/` because of the allowlist (`"This email isn't
  authorized to sign up."`) — worth its own message ("this account isn't
  invited yet") rather than folding into the generic Google-failure message
  below, since it's not a technical failure the user can retry past.
- `401` from `google/` for any other reason — Google sign-in failed or is
  misconfigured; show a generic "Google sign-in isn't working right now"
  and let the user fall back to email/password.
- `401` from `token/refresh/` — the session is over. Clear stored tokens and
  redirect to login. Don't retry.
- `401` from any other authenticated endpoint after the auto-refresh retry
  in the pattern above already failed once — same as above, session's over.
