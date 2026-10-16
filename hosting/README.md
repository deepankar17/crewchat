# crewchat's website (Firebase Hosting)

The public site, https://crewchat.dev: the Hosting site `crewchat-chat` in the Firebase project
`crewchat-ae908` (`.firebaserc`, `firebase.json`), also at https://crewchat-chat.web.app.

The domain is registered with Cloudflare, whose DNS points it at Firebase as PhonIQ's is, every record
"DNS only" (not proxied, so Firebase can issue its certificate):

| Record | Name | Value |
|--------|------|-------|
| A | `crewchat.dev` | `199.36.158.100` (Firebase Hosting) |
| TXT | `crewchat.dev` | `hosting-site=crewchat-chat` (Firebase's ownership check) |
| CNAME | `www` | `crewchat-chat.web.app` (Firebase redirects `www.crewchat.dev` to `crewchat.dev`) |
| MX, SPF, DKIM | | Cloudflare Email Routing, managed by Cloudflare |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:support@crewchat.dev` |

## Pages

`hosting/public/` is **generated** and not in git: edit `site/` (templates, `site/assets`,
`site/config.json`) and run `python3 site/build.py` to see it; every deploy builds it first.
`tests/test_site.py` checks the build: links, nothing inline under the security policy, sizes.

| Page | Template |
|------|----------|
| `/` | `site/templates/home.html`: what crewchat is, demos, install, which agents work |
| `/privacy.html` | `site/templates/privacy.html`: what crewchat connects to (from `docs/privacy.md`) |
| `/404.html` | `site/templates/404.html` |

The demo videos and screenshots come from `docs/images`: `python3 site/media.py` (needs ffmpeg) turns
the GIFs into small MP4s with posters and records their sizes in `site/media.json`. Run it when those
images change; the results are committed, so building and deploying need no ffmpeg.

## Deploy

From the repository root (the Firebase CLI rebuilds the site first, `predeploy` in `firebase.json`):

```bash
npx firebase-tools deploy --only hosting
```

Try a change on a temporary address first:

```bash
npx firebase-tools hosting:channel:deploy preview --expires 7d
```
