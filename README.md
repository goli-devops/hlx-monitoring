# StayGrid Branch Monitor — GitHub + Vercel edition

Checks all 10 branch front-desk login pages every 5 minutes, emails
`ict.devops@globalofficium.onmicrosoft.com` on down/recovery, and shows
a live status page you can bookmark.

**How it fits together:**
- **GitHub Actions** runs the actual checks every 5 minutes and sends
  emails (free, no server to maintain). It commits the results to
  `state.json` in the repo.
- **Vercel** hosts a static status page (`index.html`) that reads that
  `state.json` straight from GitHub and auto-refreshes. Vercel doesn't
  need to redeploy for updates to show — the page always fetches the
  latest file.

```
staygrid-repo/
  .github/workflows/monitor.yml   <- the schedule + email logic (GitHub Actions)
  scripts/monitor.py              <- the checking + emailing script
  scripts/config.json             <- branch URLs + settings (no secrets)
  scripts/requirements.txt
  state.json                      <- status data, updated automatically
  index.html                      <- the dashboard (what Vercel serves)
```

---

## Part 1 — Set up email sending (Microsoft Graph API)

Microsoft is disabling plain username/password SMTP for Microsoft 365
tenants through 2026, so this uses Microsoft Graph with an app
registration instead — it'll keep working regardless of that change.

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft
   Entra ID → App registrations → New registration**.
   - Name: `staygrid-monitor` (anything you like)
   - Supported account types: single tenant
   - Click **Register**.
2. On the app's **Overview** page, copy and save:
   - **Application (client) ID**
   - **Directory (tenant) ID**
3. Go to **Certificates & secrets → Client secrets → New client
   secret**. Add a description, choose an expiry, click **Add**, then
   **copy the secret's Value immediately** — it's only shown once.
4. Go to **API permissions → Add a permission → Microsoft Graph →
   Application permissions** → search `Mail.Send` → add it.
5. Still on API permissions, click **Grant admin consent for
   [your org]** (needs an account with admin rights — required, the
   app can't send mail without this step).

You should now have three values saved somewhere safe: tenant ID,
client ID, client secret.

---

## Part 2 — Push the code to GitHub

1. Create a new **private** repo on GitHub (e.g. `staygrid-monitor`).
   Private is recommended since this repo will contain your branch
   URLs and monitor logic.
2. Download the files I generated and push them to that repo:

   ```bash
   cd staygrid-repo
   git init
   git add .
   git commit -m "Initial branch monitor setup"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

## Part 3 — Add your email credentials as GitHub secrets

These stay encrypted in GitHub and are never committed to the repo.

1. In your repo: **Settings → Secrets and variables → Actions → New
   repository secret**.
2. Add three secrets, using the values from Part 1:
   - `GRAPH_TENANT_ID`
   - `GRAPH_CLIENT_ID`
   - `GRAPH_CLIENT_SECRET`

## Part 4 — Turn on and test the GitHub Actions workflow

1. Go to the **Actions** tab in your repo. If prompted, click "I
   understand my workflows, go ahead and enable them."
2. You should see **Branch Monitor** listed. Click it, then **Run
   workflow** (the `workflow_dispatch` trigger lets you run it
   on-demand instead of waiting for the schedule).
3. Watch the run — click into it, expand **Run monitor**, confirm each
   of the 10 branches gets checked with no errors.
4. Check your inbox: since every branch starts as "unknown → checked",
   you shouldn't get alert emails on a normal first run (alerts only
   fire on a down/up transition). To confirm email sending itself
   works end-to-end, you can temporarily run it locally with
   `python scripts/monitor.py --test-email` (needs the three
   `GRAPH_*` values as local environment variables), or just trust the
   workflow logs — a failed send shows up clearly there as an error.
5. After this first run, check that `state.json` was updated and
   committed — look at the repo's commit history for "Update monitor
   state."
6. From here it runs automatically every 5 minutes. No further action
   needed.

**One thing to know:** GitHub disables scheduled workflows
automatically if a repository goes 60 days with no other commits or
activity. If that ever happens, just open the Actions tab and click
**Run workflow** once to re-enable it, or make any small commit.

---

## Part 5 — Deploy the dashboard to Vercel

1. Before deploying, edit `index.html` and update this line near the
   bottom with your actual GitHub username, repo name, and branch:

   ```js
   const STATE_URL = "https://raw.githubusercontent.com/USER/REPO/BRANCH/state.json";
   ```

   For example, if your repo is `github.com/globalofficium/staygrid-monitor`
   on the `main` branch:

   ```js
   const STATE_URL = "https://raw.githubusercontent.com/globalofficium/staygrid-monitor/main/state.json";
   ```

   Commit and push that change.

2. Go to [vercel.com](https://vercel.com) → sign in with your GitHub
   account.
3. **Add New… → Project**, then **Import** the `staygrid-monitor`
   repo.
4. On the configuration screen:
   - **Framework Preset:** Other
   - **Root Directory:** leave as the repo root (where `index.html`
     lives)
   - **Build Command:** leave empty
   - **Output Directory:** leave empty (Vercel will serve the root as
     static files)
5. Click **Deploy**. It takes under a minute since there's nothing to
   build.
6. Once deployed, open the URL Vercel gives you (something like
   `staygrid-monitor.vercel.app`). You should see the status board.
   If it shows "No checks recorded yet," wait for the next GitHub
   Actions run (or trigger one manually per Part 4, step 2) and
   refresh.

**Note on repo visibility:** `raw.githubusercontent.com` only serves a
private repo's files with authentication, which the dashboard doesn't
send. If you made the repo private in Part 2, either:
- make just this practical trade-off and keep the repo **public**
  (the branch URLs and status aren't sensitive, and no secrets are
  ever in the repo — they're GitHub Actions secrets, stored
  separately and never committed), or
- keep it private and instead have the dashboard fetch from a small
  Vercel serverless function that pulls `state.json` from GitHub's
  API using a read-only token — more setup; ask me if you'd rather go
  this route.

Most teams find option 1 (public repo, no real secrets in it) simplest.

---

## Day-to-day use

- **View status:** just open your Vercel URL any time. It auto-refreshes
  every 45 seconds.
- **Get alerted:** nothing to do — emails arrive automatically at
  `ict.devops@globalofficium.onmicrosoft.com` on down/still-down/recovered.
- **Add or remove a branch:** edit the `urls` list in
  `scripts/config.json`, commit, push. Takes effect on the next
  scheduled run.
- **Change check frequency:** edit the `cron` line in
  `.github/workflows/monitor.yml` (`*/5 * * * *` = every 5 minutes).
  GitHub Actions doesn't support sub-5-minute schedules.
- **Change how often you're re-notified while something's down:** edit
  `alert_repeat_minutes` in `scripts/config.json` (default 60).
- **Add more recipients:** edit `email.recipients` in
  `scripts/config.json`.
- **Check run history / troubleshoot a missed alert:** GitHub repo →
  **Actions** tab → click any past run to see its full log.
- **Catch a stricter kind of failure** (server returns 200 but a blank
  or broken page): set `expect_text` in `scripts/config.json` to a
  word that should always appear on a working login page (e.g.
  `"password"`).
