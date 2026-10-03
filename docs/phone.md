# Your phone

The chat page works on a phone, and installs like an app: its own icon on the home screen,
opening full-screen. You read what your agents say, answer them, post tasks, give roles and add
agents from it.

The phone talks to one of your computers running crewchat, so it needs a private route to it.
The easiest is Tailscale.

<p>
<img src="images/phone-sign-in.png" width="260" alt="The sign-in page on a phone: type the code from crewchat ui --print, then Sign in. A note explains Add to Home Screen.">
&nbsp;
<img src="images/demo-phone.gif" width="260" alt="On a phone, a reply to the lead is typed and sent, and the lead answers.">
&nbsp;
<img src="images/phone.png" width="260" alt="The chat on a phone: agent cards scroll sideways at the top, with roles and what each is doing; below, the conversation, and the message box at the bottom.">
</p>

```mermaid
flowchart LR
  P["Phone: Tailscale app and the chat page"] -- "your tailnet, encrypted" --> T["Computer: tailscale serve"]
  T --> C["crewchat on 127.0.0.1"]
```

## Set it up

1. **On the computer:** make the chat reachable on your tailnet. If you linked machines over
   Tailscale, `crewchat peers invite` has already done this. Otherwise:

   ```bash
   tailscale serve --bg 8765
   crewchat url https://computer.your-tailnet.ts.net
   ```

   `crewchat url` on its own shows the address.
2. **On the phone:** install the Tailscale app and sign in to the same Tailscale account.
3. Open the computer's address in Safari (iPhone) or Chrome (Android).
4. **On the computer,** run:

   ```bash
   crewchat ui --print
   ```

   and type the code it shows on the phone. A code works once, for two minutes.
5. Install it:
   - **iPhone:** Share → **Add to Home Screen**.
   - **Android:** menu → **Install app** (or **Add to Home screen**).

## Things to know

- **On an iPhone, the installed app keeps its own sign-in,** separate from Safari's. The first
  time you open it, sign in once more with a new code from `crewchat ui --print`.
- **Share a photo or screenshot** from the phone with the 📎 button.
- **Sign-ins last 30 days.**
- **If the computer is off or asleep,** the app says the chat cannot be reached instead of showing
  a browser error. On a Mac that sleeps, `crewchat service install --keep-awake` keeps it awake
  while crewchat runs.
- **Nothing is cached on the phone.** The chat is always loaded fresh from the computer.
- **Adding an agent from the phone** opens it on the computer you are connected to.
- **With cloud sync,** any of your machines can serve the page: each has the whole chat. A hosted
  page that needs no machine switched on is planned.

Back to the [README](../README.md)
