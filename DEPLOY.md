# Put Tradewise online with Vercel (free)

About 15 minutes, all clicks, no commands. You need free accounts on **GitHub** and **Vercel**.

When you're done, you'll have:
- a private website, protected by your password
- prices that update by themselves every weekday evening (around 5, 7 and 9 pm IST, plus 8 am as a catch-up)
- practice accounts that trade by themselves after each update

---

## 1. Put the code on GitHub

1. Go to **github.com** and sign in (or sign up).
2. Click **+** (top right) → **New repository**.
   - Name: `tradewise`
   - Choose **Private**.
   - Click **Create repository**.
3. On the next page, click the link **"uploading an existing file"**.
4. Open your **Documents → tradewise** folder. Select **everything inside it** (not the folder itself) and drag it onto
   the GitHub page. Skip `.venv` if you see it.
5. Wait for the upload to finish, then click **Commit changes**.

## 2. Create the website on Vercel

1. Go to **vercel.com** and choose **Sign Up → Continue with GitHub**. Pick the free **Hobby** plan.
2. Click **Add New… → Project**, find **tradewise**, and click **Import**.
3. For **Framework Preset**, choose **Other**.
4. Open **Environment Variables** and add two:

   | Name | Value |
   |---|---|
   | `APP_PASSWORD` | The password you'll use to open your site. Make it long. |
   | `CRON_SECRET` | Any long random text, at least 30 letters and numbers. You never type it again. |

5. Click **Deploy** and wait for it to finish (a few minutes).

## 3. Add the database (keeps your prices and practice accounts)

1. In your new Vercel project, open the **Storage** tab.
2. Click **Create Database**, choose **Neon** (Serverless Postgres), and click **Continue**.
3. For the region, pick **Washington, D.C., USA (East)**, the same place Vercel runs the app. Pick the
   **Free** plan, then click **Create**.
4. When asked, **connect it to the tradewise project** (all environments).
5. Open **Deployments**, click **⋯** on the top one, and choose **Redeploy**.

## 4. Open it

1. Click **Visit**, or open the address shown on the project page (like `tradewise-yourname.vercel.app`).
2. Log in with your `APP_PASSWORD`.
3. The first visit downloads about 12 years of prices. That takes 1–3 minutes, and the page updates by itself.
4. Bookmark it, or on your phone use **Share → Add to Home Screen**.

In the Vercel project, **Settings → Cron Jobs** should list four scheduled updates.

---

## If something looks wrong

- **"One more step" page:** the database isn't connected yet. Do step 3, then redeploy.
- **"Couldn't reach the price server" or DEMO prices on the hosted site:** Yahoo sometimes blocks cloud servers.
  Tradewise keeps trying at every scheduled update. If it lasts more than a day, tell me: the fix is
  switching to a paid data source.
- **Wrong password:** change `APP_PASSWORD` in Vercel → Settings → Environment Variables, then redeploy.
- **Changing the code later:** upload the changed files to the same GitHub repository. Vercel redeploys by itself.

## Limits of the free plan

- **Update timing:** the evening updates can start up to an hour late (a Vercel Hobby limit). Ideas are for the
  next morning, so this doesn't matter.
- **No real orders:** placing real orders through a broker needs a fixed internet address (static IP), which the
  free plan can't provide. That's a separate step for later.
