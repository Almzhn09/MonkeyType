# Hosting a public link

The team's deployment: https://monkeytype-audit.onrender.com/ (dashboard at `/dashboard/`).

The playground (play the auditor) and the reviewer dashboard run behind one URL, so judges and
friends can open them in a browser without installing anything:

| Path | What |
|---|---|
| `/` | Playground: pick a company, inspect records, submit findings, see the grader's result |
| `/dashboard/` | Reviewer dashboard: the demo and a full dev-split run of all four policies |

The grader and answer keys stay on the server. The browser only receives public records and,
after a player submits, that player's own results.

## Try the combined site on your computer first

```sh
python3 -m webapp serve
```

Open http://localhost:7860/ (playground) and http://localhost:7860/dashboard/ (dashboard).
Stop it with Ctrl+C.

## Put it on Render (free)

The repository contains `render.yaml` (a Render Blueprint) and `deploy/web.Dockerfile`, so there
is nothing to fill in by hand. The image build precomputes the dashboard data and every baseline
score, so the small free instance starts quickly.

1. Make sure the code is on GitHub in the branch you want to deploy (normally `main`, after the
   pull request is merged).
2. Go to https://render.com and **Sign up with GitHub**.
3. In the Render dashboard choose **New** → **Blueprint**.
4. Connect GitHub and allow Render to see the `MonkeyType` repository. If the repository belongs
   to a teammate's account, they may need to approve Render's access for it, or be the one who
   connects it.
5. Pick the repository and the branch. Render reads `render.yaml` and shows one free web service
   called `monkeytype-audit`. Click **Apply** (or **Deploy Blueprint**).
6. The first build takes several minutes; the **Logs** tab shows progress, including
   `baselines: 40 episodes scored`. When it says **Live**, the address is shown at the top of the
   service page, for example `https://monkeytype-audit.onrender.com/`. Share it, and the same
   address with `/dashboard/` for the dashboard.

Later pushes to that branch redeploy automatically.

## Good to know

- **No login.** Anyone with the link can play. Grading is light and baseline scores are
  precomputed, but the site is public; suspend or delete the service when you no longer need it
  (service → **Settings** → **Suspend** / **Delete**).
- **Sleeping.** Free Render services sleep after about 15 minutes without visitors; the next
  visitor waits roughly a minute while it wakes.
- **Payment details.** Render's free plan has not required a card, but plans change. If Render asks
  for payment to deploy, stop there.
- **Baselines unavailable.** If the results page says baseline scores are unavailable, the host
  blocked the policy sandbox. Playing and grading still work; only the comparison is missing.
- **Not verified end to end.** The image build and the Render deploy were not run where this was
  written (no Docker engine there). The same commands were run outside Docker, and the Render
  build log is the first real check.

## Other hosts

`deploy/web.Dockerfile` is an ordinary image (build from the repository root:
`docker build -f deploy/web.Dockerfile -t monkeytype-web .`, run with
`docker run -p 10000:10000 -e PORT=10000 monkeytype-web`), so any host that runs Docker images
works. Hugging Face Docker Spaces now need a paid plan; with one,
`python3 -m webapp export-space DIR` writes a ready-to-upload Space folder.
