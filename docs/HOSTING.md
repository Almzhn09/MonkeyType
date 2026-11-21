# Hosting a public link

The playground (play the auditor) and the reviewer dashboard can run behind one URL, so judges
and friends can open them in a browser without installing anything. The hosted site serves:

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
It takes a few seconds to start because it runs the demo and the dev split once. Stop it with Ctrl+C.

## Put it on Hugging Face Spaces (free)

1. Create a free account at https://huggingface.co/join.
2. Create an access token: avatar menu → **Settings** → **Access Tokens** → **Create new token**,
   type **Write**. Copy it; you will paste it as the git password.
3. Create the Space: avatar menu → **New Space**. Name it (for example `monkeytype-audit`),
   choose **Docker** as the SDK and **Blank** as the template, keep **Public** and the free
   **CPU basic** hardware, then **Create Space**.
4. In Terminal, clone the empty Space (replace `USERNAME` and `SPACE`):

   ```sh
   git clone https://huggingface.co/spaces/USERNAME/SPACE ~/monkeytype-space
   ```

   When git asks for a password, paste the token from step 2.
5. Export the app into it from the project folder:

   ```sh
   cd ~/MonkeyType
   python3 -m webapp export-space ~/monkeytype-space
   ```

6. Upload:

   ```sh
   cd ~/monkeytype-space
   git add -A
   git commit -m "Deploy MonkeyType"
   git push
   ```

7. Open the Space page. The **Logs** tab shows the build; it takes a few minutes. When the
   status says **Running**, the public address is `https://USERNAME-SPACE.hf.space/`.
   Share that link, and `https://USERNAME-SPACE.hf.space/dashboard/` for the dashboard.

To update the site after code changes, repeat steps 5 and 6.

## Good to know

- **No login.** Anyone with the link can play. Each finished game runs the four baseline policies
  once per company (results are cached), which is light, but the site is public, so pause it when
  you no longer need it: Space → **Settings** → **Pause** (or **Delete**).
- **Sleeping.** Free Spaces sleep after a period without visitors; the next visitor waits about a
  minute while it wakes and prepares the dashboard data again.
- **Baselines unavailable.** If the results page says baseline scores are unavailable, the host
  blocked the policy sandbox. Playing and grading still work; only the comparison table is missing.
- **Docker elsewhere.** The exported folder is a normal Docker build context:
  `docker build -t monkeytype-space ~/monkeytype-space && docker run -p 7860:7860 monkeytype-space`.
  This image build was not tested in the environment where it was written (no Docker engine there);
  the Space build log is the first real check.
