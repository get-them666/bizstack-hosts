# LinkedIn Ads — Setup Runbook (BizStack Hosts)

Status: ✅ Business Page exists · ⏳ Campaign Manager + billing · ⏳ Insight Tag · ⏳ Campaigns live

## Your 3 tasks (account-level, only you can do these)

### 1. Create Campaign Manager account
- Go to https://www.linkedin.com/campaignmanager/ and create an account
- Select your Business Page when asked ("Business page / organization")
- Name it "BizStack Hosts"

### 2. Add your payment method (billing)
- Campaign Manager → **Billing** (left sidebar) → **Payment Methods** → Add payment card
- Cards are charged via prepaid balance; add e.g. $50–$100 to start
- LinkedIn won't charge until the ad account is live and the first campaign runs

### 3. Get your Insight Tag ID and send it to me
- Campaign Manager → **Account Assets** → **Insight Tag**
- Click **Set up tag** / copy the `<script>` snippet
- The value I need is the number after `partner-id=` (e.g. `partner-id="12345678"`)
- **Send me that ID** and I will install the tag + conversion tracking on bizstackperks.com immediately

> Optional later: **Domain verification** (Account Assets → Insight Tag → Verify domain) adds a DNS TXT record. Only needed for Marketing API access — not required to launch ads.

## What I do once you send the Insight Tag ID
1. Install Insight Tag (tracking pixel) on every page — `templates/base.html`
2. Add conversion tracking events: `Lead` on the rental inquiry form (`/submit-lead` success), `PageView` sitewide
3. Build the campaign structure per below
4. Help you review/publish in Campaign Manager (or drive it via Marketing API if you set up the dev app)

## Campaign structure (from MARKETING.md)

Three campaign groups, one per audience angle. Single image creatives, **no video needed to start**.

| # | Group / angle | Target | CTA | Destination |
|---|---|---|---|---|
| 1 | "Run it on autopilot" | STR hosts, property managers | Learn More | `https://bizstackperks.com?src=linkedin&camp=autopilot` |
| 2 | "Numbers: profit not chores" | Real estate investors | Learn More | `https://bizstackperks.com?src=linkedin&camp=numbers` |
| 3 | "AI + humans (differentiator)" | Property management industry | Learn More | `https://bizstackperks.com?src=linkedin&camp=ai` |

### Targeting (recommended baseline)
- **Locations:** United States → Virginia, North Carolina (expand to national listings later)
- **Company size:** 1–10 and 11–50 employees (targets independent/owner-operators)
- **Industries:** Property Management, Real Estate, Accommodation / Travel
- **Job titles / seniority (OR):** Owner, Founder, Chief Executive Officer, General Manager, Vice President, Property Manager
- Audience education/Skills optional. Start narrow + cheap; let LinkedIn learn.

### Creative specs (single image)
- **Recommended size:** 1200 × 627 (1.91:1) or 1080 × 1080 (square)
- **Headline:** ≤ 70 chars from the ad variants
- **Text:** 150–300 chars from MARKETING.md
- **CTA button:** "Learn More"
- Intro/eyebrow: "Short-Term Rental Operations"

### Budget plan — $250 free credit (first ~14 days)
- You have **$250 in free LinkedIn ad credit** — LinkedIn applies it automatically to new spend; nothing special to do in billing
- **Daily budget:** $18/day total (≈ $250 over 14 days) split across the 3 groups (~$6 each)
- Run the full credit, then evaluate the scoreboard:
  - **Goal:** cost per lead < $25 · CTR > 0.8% · leads converting to host
  - Kill the worst-performing group and double the best after the credit is spent
- Note: LinkedIn still requires a **payment method on file** to launch ads — the card only gets charged if you exceed the credit

## Measurement
Every lead from ads lands in the dashboard under **source = linkedin** with a campaign badge (`autopilot` / `numbers` / `ai`) shown next to the channel on `/hosts` — so you can see which ad group converts.

## What I need back from you
1. Insight Tag ID (from task 3 above)
2. Campaign Manager account name (so I know which one to expect) — optional