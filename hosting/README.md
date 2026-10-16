# crewchat's website (Firebase Hosting)

The public site, https://crewchat-chat.web.app: the Hosting site `crewchat-chat` in the Firebase
project `crewchat-ae908` (`.firebaserc`, `firebase.json`). The name `crewchat` was taken on Firebase.

## Pages

`hosting/public/` is **generated**. Edit `site/` (templates, `site/assets`, `site/config.json`),
then run `python3 site/build.py`, which rewrites `hosting/public/`. Never edit it by hand.
`tests/test_site.py` checks the build (links, nothing inline under the security policy, sizes) and that
the committed copy matches it.

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
