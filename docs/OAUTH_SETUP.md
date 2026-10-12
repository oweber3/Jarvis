# Registering the Microsoft and Google sign-in apps

**Sign in with Microsoft** and **Sign in with Google** (Settings → Calendar & Email) need an OAuth app registered once by whoever publishes a Jarvis build. The registration holds the app's name and permissions. Users never see it or touch it: they click the button, sign in in their browser and allow read access.

This guide is for the project owner. It covers both registrations and where to paste the IDs. Until the IDs are in, the buttons are hidden, and every other option still works (Gmail app password, IMAP, calendar links, classic Outlook).

What Jarvis asks for, read only:

| Provider | Scopes | Why |
|----------|--------|-----|
| Microsoft | `offline_access`, `User.Read`, `Calendars.Read`, `Mail.Read` | Stay signed in, show which account is connected, read the calendar, read the inbox. The user can untick Calendar or Email, and Jarvis then asks for less |
| Google | `https://www.googleapis.com/auth/calendar.readonly` | Read Google Calendar. **Gmail is deliberately not requested**: Gmail scopes are "restricted" and need a yearly paid security assessment. Gmail users add their mailbox with an app password instead |

Both apps are *public desktop clients*. An installed app cannot keep a secret, so every sign-in is protected by PKCE and a loopback redirect to `127.0.0.1` on a random port. Never create a client secret for the Microsoft app. Google's desktop client secret is documented by Google as not confidential and ships with Jarvis like the ID.

---

## A. Microsoft (Microsoft Entra admin centre)

You need a Microsoft account with access to a Microsoft Entra directory. A free Azure account works, and nothing in this setup costs money. If you sign in with a personal account and have no directory yet, the portal offers to create one.

1. Open **https://entra.microsoft.com** and sign in.
2. In the left menu choose **Entra ID** (or **Identity**) → **App registrations** → **+ New registration**.
3. Fill in the form:
   - **Name**: `Jarvis` (users see this on the consent screen).
   - **Supported account types**: **Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)**. This lets both Outlook.com/Hotmail and work or school accounts sign in.
   - **Redirect URI**: choose platform **Public client/native (mobile & desktop)** and enter `http://localhost`.
   - Click **Register**.
4. On the app's **Overview** page, copy the **Application (client) ID**. This is `microsoft_oauth_client_id`.
5. Open **Authentication**:
   - Under **Mobile and desktop applications**, check that `http://localhost` is listed. Microsoft ignores the port for loopback addresses, so Jarvis's random port is accepted.
   - Under **Advanced settings**, set **Allow public client flows** to **Yes**.
   - Click **Save**.
6. Open **API permissions** → **+ Add a permission** → **Microsoft Graph** → **Delegated permissions**. Tick `offline_access`, `Calendars.Read` and `Mail.Read` (`User.Read` is usually already listed; keep it). Click **Add permissions**.
   - Do **not** click "Grant admin consent". Each user consents for themselves. Some companies block user consent; their IT admin can approve Jarvis for the organisation.
   - Do not add any `Write`, `Send` or `ReadWrite` permission.
7. **Certificates & secrets**: leave it empty. A desktop app must not have a client secret.
8. **Branding & properties** (recommended for a public release): set the logo, **Home page URL**, **Terms of service URL** and **Privacy statement URL**.
9. **Publisher verification** (optional, strongly recommended for public release): under **Branding & properties** → **Publisher domain**, verify a domain you own. Then add your **Microsoft AI Cloud Partner Program (Partner) ID** to mark the app as verified.
   - Without it, the consent screen says "unverified".
   - Many organisations block their users from consenting to unverified multi-tenant apps. Personal accounts are not affected.

Optional: the loopback redirect works through `http://localhost` as registered. Browsers fall back to `127.0.0.1` when `localhost` resolves to IPv6 first, which is how Microsoft's own MSAL library behaves.

## B. Google (Google Cloud console)

1. Open **https://console.cloud.google.com**, then use the project picker (top bar) → **New project**. Name it `Jarvis` → **Create**, and select the new project.
2. **APIs & Services** → **Library**. Search for **Google Calendar API**, open it and click **Enable**.
3. Open **Google Auth Platform** (formerly "OAuth consent screen"). If asked, click **Get started**.
   - **App information**: app name `Jarvis` and a user support email.
   - **Audience**: **External**.
   - **Contact information**: your email.
   - Agree to the Google API Services User Data Policy, then click **Create**.
4. **Branding**: add the app logo (optional) and the **Application home page**, **Application privacy policy link** and **Application terms of service link**. Under **Authorised domains**, add the domain that hosts those pages.
5. **Data access** → **Add or remove scopes**: tick `.../auth/calendar.readonly` ("See and download any calendar you can access using your Google Calendar"), then click **Update** and **Save**. Add nothing else; specifically no Gmail scope.
6. **Audience**: while the app is in **Testing**, add yourself and any testers under **Test users**.
   - Only listed test users (up to 100) can sign in.
   - Refresh tokens expire after 7 days, so testers must sign in again weekly. Publishing removes both limits.
7. **Clients** → **+ Create client**: set **Application type** to **Desktop app** and **Name** to `Jarvis desktop`, then click **Create**.
   - Copy the **Client ID** (`google_oauth_client_id`) and the **Client secret** (`google_oauth_client_secret`).
   - Desktop clients need no redirect URI. Google allows the `127.0.0.1` loopback automatically.

### Going public: Google verification

`calendar.readonly` is a **sensitive** scope (not restricted), so a public release needs Google's **OAuth app verification**. A security assessment is not required. In **Google Auth Platform** → **Audience**, click **Publish app**, then submit for verification under **Verification Center**. Google checks:

- **Domain ownership**: verify the domain of your home page and privacy policy in **Google Search Console** with the same Google account that owns the project.
- **Home page**: a public page on that domain that describes Jarvis and what it does with calendar data.
- **Privacy policy**: on the same domain. It must say which Google data Jarvis accesses (calendar events, read only), how it is used (answering the user's questions on their own PC), that it is not stored on servers or shared, and that use complies with the Google API Services User Data Policy, including the Limited Use requirements.
- **Scope justification**: a short explanation of why `calendar.readonly` is needed ("to answer the user's spoken questions about their own schedule, read only, on their device").
- **Demo video** (unlisted YouTube link): show the full flow in English.
  - Start Jarvis, open Settings → Calendar & Email and click Sign in with Google.
  - Show the consent screen with the app name and the client ID visible in the browser address bar.
  - Then show Jarvis answering "what's on today?" from the calendar.
- **Branding**: the app name and logo must match your home page.

Verification usually takes a few days to a few weeks. Until it is granted, users outside the test list see "Google hasn't verified this app" and cannot sign in.

## C. Where to put the IDs

Paste the values into the one marked constant in **`src/jarvis/accounts/clients.py`**:

```python
BUILT_IN_OAUTH_CLIENTS: Dict[str, str] = {
    "microsoft_oauth_client_id": "<Application (client) ID from step A4>",
    "google_oauth_client_id": "<Client ID from step B7>",
    "google_oauth_client_secret": "<Client secret from step B7>",
}
```

To try an ID before committing it, put the same keys in your own `~/.config/jarvis/config.json`; a value there overrides the built-in one. Power users can point Jarvis at their own registrations in the same way.

Then check it works:

1. Start Jarvis and open **Settings → Calendar & Email**. The sign-in buttons should be enabled.
2. Click **Sign in with Microsoft**, sign in in the browser and allow access. The browser shows "Signed in" and the account appears in the list. Click **Test**.
3. Do the same with **Sign in with Google**.
4. Restart Jarvis when asked and say "what's on today?".

Changing an ID later does not break existing sign-ins: each stored sign-in remembers the client it was issued to.
