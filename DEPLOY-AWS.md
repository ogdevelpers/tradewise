# Run Tradewise on AWS in Mumbai

This setup gives you a server in Mumbai with a **fixed IP address**, which Zerodha requires for real trading.

When you're done, you'll have:
- your private Tradewise website, `https://<numbers>.sslip.io`, password-protected and open on phone and laptop
- prices that update by themselves every weekday after 4 pm, and practice accounts that trade by themselves
- the **Real trading** page: each evening you log in to Zerodha, review the plan, and press one button

About 30 minutes, mostly waiting. Commands aren't needed.

> **Cost:**
> - **First 6 months:** new AWS accounts on the **Free plan** get $100 of credit (up to $200 by trying a few
>   services), valid for 6 months. This setup uses a small part of that each month. On the Free plan, AWS
>   doesn't charge you.
> - **After that:** to keep the server, upgrade to the paid plan, roughly ₹1,000–1,500/month for this setup
>   (an estimate; check the AWS price calculator).
> - **If you don't upgrade:** AWS closes a Free plan account **90 days after it ends** and deletes its servers.
> - **Zerodha:** the Kite Connect API costs **₹500/month**.

---

## 1. Create the AWS account

1. Go to **aws.amazon.com** and click **Create an AWS Account**.
2. When asked to choose a plan, pick **Free plan**.
   - If your account is set up by **AWS India (Amazon Internet Services Pvt. Ltd.)** and no Free plan
     appears, the free credit may not apply to your account. In that case you'd pay from day one.
     Set the budget in step 1.4 before creating anything.
3. Verify with a **Visa or Mastercard credit card** (international transactions turned on). India accounts
   are usually charged a tiny refundable amount to check the card.
4. After signing in, search for **Budgets** → **Create budget** → **Use a template** → **Zero spend budget** →
   enter your email → **Create**. AWS will email you if anything ever starts costing money.

## 2. Create the server

1. In the region menu at the top right, choose **Asia Pacific (Mumbai) ap-south-1**.
2. Search for **EC2** → **Launch instance**.
3. **Name:** `tradewise`
4. **Application and OS Images:** choose **Ubuntu**, then **Ubuntu Server 24.04 LTS**. Under **Architecture**,
   pick **64-bit (Arm)**.
5. **Instance type:** `t4g.small` (2 GB memory). It shows **Free tier eligible**. Tradewise needs only about
   250 MB, so `t4g.micro` also works and uses less credit.
6. **Key pair:** choose **Create new key pair**, name it `tradewise`, and download it. Keep it safe; you won't
   need it day to day.
7. **Network settings:** keep **Allow SSH traffic** on, and tick both **Allow HTTPS traffic from the internet**
   and **Allow HTTP traffic from the internet**.
8. **Configure storage:** 20 GiB **gp3** is plenty.
9. Open **Advanced details** and scroll to **User data** at the bottom.
10. Open **`deploy/setup-server.sh`** from the Tradewise folder in a text editor and change the three lines at the
    top:
    - `APP_PASSWORD`: the password for your website.
    - `REPO_URL`: leave as is.
    - `GITHUB_TOKEN`: only if your GitHub repository is private. Create one at github.com → Settings →
      Developer settings → Fine-grained tokens, with read-only *Contents* access to the tradewise repository.

    Then copy the whole file into **User data**.
11. Click **Launch instance**.

## 3. Give it a permanent IP address (Elastic IP)

Do this right away, before registering anything with Zerodha.

1. In EC2, open **Elastic IPs** (left menu, under *Network & Security*) → **Allocate Elastic IP address** →
   **Allocate**.
2. Select the new address → **Actions → Associate Elastic IP address** → choose the **tradewise** instance →
   **Associate**.
3. Note this **Elastic IP**. It stays the same for as long as you keep it.
4. Go to **Instances**, select **tradewise** → **Instance state → Reboot**, so Tradewise picks up the new address.

> An Elastic IP is billed while you hold it (a few dollars a month); on the Free plan, the credit covers it.
> If you ever stop using Tradewise, release the Elastic IP so it doesn't keep costing.

## 4. Open your Tradewise

1. Wait about 10 minutes after the reboot.
2. Turn your Elastic IP into the address by replacing dots with dashes. For example, IP `13.234.10.20` becomes
   **https://13-234-10-20.sslip.io**.
3. Open it and log in with your `APP_PASSWORD`. The first price download takes a few minutes, and the page
   updates by itself.
4. Bookmark it. On your phone, use **Share → Add to Home Screen**.

## 5. Connect Zerodha (for real trading)

1. Go to **developers.kite.trade**, sign up, and create an app (Kite Connect, ₹500/month).
2. **Redirect URL:** `https://<your-address>.sslip.io/api/broker/callback`
3. **IP Whitelist:** add your Elastic IP.
4. In Tradewise, open **Real trading**, paste the app's **API key** and **API secret**, and save.
5. Click **Log in to Zerodha**.

Every evening after 4 pm: open **Real trading** → **Log in to Zerodha** → read **Tonight's plan** → tick the box →
**Place orders**. DEPLOY-INDIA.md, section 7, explains what happens and the safety rails.

---

## If something isn't working

- **Website doesn't open after 15 minutes:**
  - In EC2, select the instance → **Actions → Monitor and troubleshoot → Get system log**, and look for
    lines starting with `=== Tradewise setup`.
  - Or connect with **EC2 Instance Connect** (the **Connect** button) and run
    `sudo tail -50 /var/log/tradewise-setup.log`.
- **Address changed:** this only happens if the Elastic IP isn't attached. Re-associate it and reboot.
- **Updating Tradewise:** after new code is on GitHub, click **Connect** on the instance (EC2 Instance Connect)
  and run `sudo tradewise-update`.
- **Before the 6 months end:** AWS emails you. Upgrade to the paid plan to keep the server, or move Tradewise to
  another Ubuntu server with the same `deploy/setup-server.sh`.
