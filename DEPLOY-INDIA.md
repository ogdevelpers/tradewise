# Run Tradewise on a free server in India (Oracle Cloud, Mumbai)

This is the setup for **real trading**. Zerodha only accepts API orders from a **fixed (static) IP
address that you register with them**, and an Oracle Cloud server in Mumbai provides one, free.

When you're done, you'll have:
- your private Tradewise website, `https://<numbers>.sslip.io`, password-protected and open on phone and laptop
- prices that update by themselves every weekday after 4 pm, and practice accounts that trade by themselves
- the **Real trading** page: each evening you log in to Zerodha, review the plan, and press one button

About 30–45 minutes, mostly waiting. Commands aren't needed.

> **Cost:** the server is in Oracle's *Always Free* tier. Zerodha's Kite Connect API costs **₹500/month**
> (Zerodha's fee, not Oracle's). Oracle asks for a card to verify you; it doesn't charge for Always Free
> resources.

---

## 1. Create the Oracle Cloud account

1. Go to **oracle.com/cloud/free** and choose **Start for free**.
2. For **Home Region**, choose **India West (Mumbai)**, or **India South (Hyderabad)**. You can't change
   this later, and the free server must be in the home region.
3. Finish the sign-up (a card is needed for verification).
4. **Recommended:** under **Billing → Upgrade and Manage Payment**, upgrade to **Pay As You Go**.
   Always Free resources stay free. It stops Oracle from reclaiming the server when it looks idle (Tradewise
   uses very little CPU, which can look idle). Then set a budget alert of ₹100 under **Billing → Budgets**,
   so you'd hear about any charge at once.

## 2. Create the server

1. Menu **☰ → Compute → Instances → Create instance**.
2. **Name:** `tradewise`.
3. **Image:** click **Change image** and choose **Canonical Ubuntu 24.04** (22.04 also works).
4. **Shape:** click **Change shape**. Pick **Ampere → VM.Standard.A1.Flex** with **2 OCPUs and 12 GB memory**.
   If it says *out of capacity*, pick **Specialty and previous generation → VM.Standard.E2.1.Micro** instead.
   Tradewise needs only about 250 MB of memory, so either works.
5. **Networking:** keep the defaults, and make sure **Assign a public IPv4 address** is on.
6. **SSH keys:** choose **Save private key** and keep the file somewhere safe. You won't need it day to day.
7. Scroll down, click **Show advanced options → Management**, and choose **Paste cloud-init script**.
8. Open **`deploy/oracle/cloud-init.sh`** from the Tradewise folder in a text editor and change the three lines
   at the top:
   - `APP_PASSWORD`: the password for your website.
   - `REPO_URL`: leave as is.
   - `GITHUB_TOKEN`: only if your GitHub repository is private. Create one at github.com → Settings →
     Developer settings → Fine-grained tokens, with read-only *Contents* access to the tradewise repository.

   Then copy the whole file into the box.
9. Click **Create**.

## 3. Make the IP address permanent (important for Zerodha)

Do this right away, before registering anything with Zerodha.

1. Open the instance → **Attached VNICs** → click the VNIC → **IPv4 Addresses**.
2. On the address row, click **⋯ → Edit**. Choose **No public IP** and save. Then edit again, choose
   **Reserved public IP → Create a new reserved IP**, name it `tradewise`, and save.
3. Note the new **public IP address**. It won't change, even if the server restarts.

   Oracle's free-tier page doesn't say whether a reserved IP is free on every account. If Oracle shows a cost
   for it, skip this step: the address the server already has normally stays the same unless you delete the
   server.
4. Click **Reboot** on the instance page, so Tradewise picks up the new address.

## 4. Open the web ports

1. On the instance page, click the **Subnet** link → **Security Lists** → **Default Security List**.
2. Click **Add Ingress Rules** twice and add these rules:
   - **Source CIDR** `0.0.0.0/0`, **Destination Port** `80`
   - **Source CIDR** `0.0.0.0/0`, **Destination Port** `443`

## 5. Open your Tradewise

1. Wait about 10 minutes after the reboot.
2. Turn your IP into the address by replacing dots with dashes. For example, IP `140.238.10.20` becomes
   **https://140-238-10-20.sslip.io**.
3. Open it and log in with your `APP_PASSWORD`. The first price download takes a few minutes, and the page
   updates by itself.
4. Bookmark it. On your phone, use **Share → Add to Home Screen**.

## 6. Connect Zerodha (for real trading)

1. Go to **developers.kite.trade**, sign up, and create an app (Kite Connect, ₹500/month).
2. **Redirect URL:** `https://<your-address>.sslip.io/api/broker/callback`
3. **IP Whitelist:** add your server's IP from step 3.
4. In Tradewise, open **Real trading**, paste the app's **API key** and **API secret**, and save.
5. Click **Log in to Zerodha**. After you log in, you come back to Tradewise, which checks your account and
   prepares tonight's plan.

## 7. How real trading works, every evening

1. **After 4 pm**, open Tradewise → **Real trading** → **Log in to Zerodha**. The exchange requires you to do
   this yourself once a day.
2. Tradewise reads your Zerodha account and makes sure every share it bought has its **safety exit** (and
   **profit step**) set at Zerodha as a GTT order. GTT orders work even if Tradewise is offline.
3. Read **Tonight's plan**, tick the box, and press **Place orders**. They go to Zerodha as after-market orders
   and execute at the next morning's open:
   - **Buys** are limit orders a little above today's close, so you never chase a big jump.
   - **Sells** are market orders with Zerodha's automatic price protection.

**Safety rails:**
- Real trading is **off** until you type the confirmation phrase.
- A **money limit** caps what's invested, and there's a **daily cap on new buys**.
- An order is never sent twice, and the run stops at the first Zerodha error.
- **Emergency stop** turns trading off and cancels Tradewise's pending orders. Safety exits stay in place to
  protect shares you already own.

**Start small:**
- Begin with a limit like ₹25,000, and keep the practice account running alongside.
- Compare the two for a few weeks before raising the limit.

---

## Good to know

- **Updating Tradewise:** after new code is pushed to GitHub, open the instance in Oracle → **Cloud Shell**,
  or connect over SSH, and run `sudo tradewise-update`.
- **Setup log:** if something didn't work, it's at `/var/log/tradewise-setup.log` on the server.
- **First trading day:** the first day after a buy, the safety exit is set in the evening, when you log in.
  Until then, during that first day, the share has no automatic stop at Zerodha. GTT orders can only protect
  shares already in your account.
- **What Tradewise doesn't do:** intraday, futures and options, or anything above Zerodha's 10-orders-per-second
  limit, so no exchange strategy registration is needed. If you ever share the signals with other people, SEBI
  research-analyst rules apply.
