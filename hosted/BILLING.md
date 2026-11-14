# Subscription setup and verification

The implementation defaults to `STRIPE_MODE=test` and accepts matching test keys. Live mode requires `STRIPE_MODE=live`, `STRIPE_LIVE_ENABLED=true`, matching live objects and a separately verified account transition. The hosted web app and worker now have subscriptions enabled in test mode after the verified legacy migration. Existing one-time credit purchases continue while `SUBSCRIPTIONS_ENABLED=false`. See [the dated verification report](../verification/stripe-test-rollout-2026-10-12.md) for deployment and sandbox results.

## Stripe configuration

In the same Stripe sandbox as `STRIPE_SECRET_KEY`, create these USD prices with no usage metering, quantity changes, coupons or trial periods:

| Environment variable | Product | Amount | Billing interval |
|---|---|---:|---|
| `STRIPE_PRICE_PRO_MONTHLY` | Basketball Highlights Pro | $29.99 | Monthly |
| `STRIPE_PRICE_PRO_ANNUAL` | Basketball Highlights Pro | $299.90 | Yearly |
| `STRIPE_PRICE_BUSINESS_MONTHLY` | Basketball Highlights Business | $69.99 | Monthly |
| `STRIPE_PRICE_BUSINESS_ANNUAL` | Basketball Highlights Business | $699.90 | Yearly |
| `STRIPE_PRICE_TOPUP` | 90 extra video minutes | $9.99 | One-time |

Use a mode-matching `sk_test_...` or `sk_live_...` in `STRIPE_SECRET_KEY`. Live mode also requires a public HTTPS `APP_BASE_URL` and explicit activation. Keep secrets in the local ignored `.env` or Secret Manager. The server checks Price currency, amount, active/mode status, licensed recurring interval and interval count before creating Checkout. Free is an application allowance and needs no Stripe Price.

Create a Customer Portal configuration and set `STRIPE_PORTAL_CONFIGURATION_ID`. Enable payment-method updates, invoice history and cancellation **at period end**. Disable subscription updates, quantity updates and plan switching. The server rejects a configuration permitting plan changes or immediate cancellation. Enable Stripe's payment retries and have an unpaid subscription end or become unpaid after retry exhaustion. The app independently limits its grace period to three days; retries do not repeatedly extend grace.

Downgrades and billing-frequency changes use server-created Stripe subscription schedules: the current price continues until the existing paid period ends, then the new price/interval applies. This includes annual-to-monthly changes at the end of the prepaid year. Same-frequency Pro to Business upgrades instead use confirmed, immediately invoiced prorations and a prorated extra-minute grant, as described below. Selecting the current plan and interval removes a pending change. Opening Customer Portal first releases a pending schedule, because Stripe Portal cannot cancel a schedule-managed subscription; account settings explain that opening management removes the pending plan change. Payment details, invoices and cancellation are then managed in Portal.

Checkout creates or reuses a Stripe customer bound to the Firebase UID. Pending Checkout sessions are reused or expired before creating another. The server checks both Stripe subscriptions and Firestore, and serializes competing billing operations. Only one nonterminal subscription per account is supported. Do not create unrelated subscriptions on the app's Stripe customers.

## Webhook configuration

Create an endpoint at `APP_BASE_URL/api/stripe/webhook` using **Snapshot** payloads. Choose **2025-04-30.basil** if offered; **2024-06-20** is also supported when the Dashboard offers only the account's current version. The server retrieves canonical resources separately using its pinned **2025-04-30.basil** request version. Set its `whsec_...` signing secret in `STRIPE_WEBHOOK_SECRET`.

Subscribe to:

- `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`
- `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`
- `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.paused`, `customer.subscription.resumed`
- `subscription_schedule.created`, `subscription_schedule.updated`, `subscription_schedule.completed`, `subscription_schedule.released`, `subscription_schedule.canceled`, `subscription_schedule.aborted`

Signatures are checked against the raw body. Subscription handlers retrieve canonical Stripe state, including the latest invoice and schedule, rather than applying an incoming stale object. A paid invoice must contain a line for the configured current price and current subscription period before extending paid access or granting minutes. Duplicate events, invoice delivery and retries cannot repeat an allowance. Terminal events from an older subscription do not replace a newer subscription's projection. Stripe/network/database errors return an error so Stripe can retry; unrelated products are ignored. Checkout redirects never grant access.

For local forwarding, start FastAPI on port 8001 and use:

```sh
stripe listen --forward-to http://127.0.0.1:8001/api/stripe/webhook
```

Use the signing secret printed by `stripe listen` for local testing and restart FastAPI after editing environment variables. Use the deployed endpoint's separate signing secret in staging. Complete purchases through the application's Checkout links with Stripe's documented sandbox cards. Confirm account state only after the webhook processes. Keep the old `STRIPE_CREDIT_PACKS` configuration until outstanding legacy Checkout sessions have expired or settled: their successful payments still receive legacy prepaid minutes.

## Safe migration and enablement

1. Deploy matching web and worker code with `SUBSCRIPTIONS_ENABLED=false`. Set `BILLING_MIGRATION_MODE=true` on the web service. This temporarily rejects mutating `/api/` requests, including webhooks (Stripe retries them), while allowing reads and internal dispatch/recovery. Wait for active analyses and reel jobs to finish. Migration refuses to proceed with active processing. The worker can retain its normal configuration while draining.
2. Using the existing internal dispatcher OIDC identity/audience, POST `{}` to `/internal/billing/migrate`. Existing available prepaid balances move to the separate legacy bucket. Existing games receive legacy feature access. Repeat after any interrupted pass: it never moves balances twice.
3. POST `{"verify":true}` as a separate pass. Require `verified: true` and `missing: 0`. Verification is recorded at `maintenance/subscriptions-v1`. Keep maintenance enabled between verification and cutover so new old-format records cannot invalidate the scan.
4. Configure all five Price IDs, Portal configuration, test key and webhook secret. Set `SUBSCRIPTIONS_ENABLED=true` on both web and worker, then `BILLING_MIGRATION_MODE=false` on the web. Subscription operations refuse to run without the verified marker.
5. Keep the existing minute-by-minute `/internal/dispatch-pending` scheduler. It refreshes annual subscribers' current monthly allowance. Account access and reservations also refresh lazily, so a delayed scheduler does not accumulate missed months or block a valid current allowance.
6. Complete the sandbox checklist below before accepting customers. For live activation, follow the separate environment transition below; changing keys alone is insufficient.

For rollback, disable new subscription sales with the flag and restore the previous offering only after considering existing subscribers. Subscription webhooks continue reconciling with the flag off. Migrated prepaid buckets remain readable/reservable by the updated code with subscriptions disabled; never restore an older image that understands only the original prepaid bucket. Turning the flag off also removes subscriber feature gates/allowance consumption, so use maintenance for a short emergency pause instead of a prolonged commercial rollback. Do not undo the migration by copying balances back.

## Stored data and access rules

- `users/{uid}/account/subscription`: Stripe customer/subscription/price IDs, billing interval and status, paid tier and paid-through date, subscription period, grace date, pending schedule and short billing-operation lease.
- `users/{uid}/account/credits`: integer milli-minute subscription, once-only free, legacy prepaid and new prepaid balances, current allowance identity/deadline and combined reservations. Existing prepaid balances remain valid.
- Run reservations store a funding breakdown and authorized duration. Workers validate actual video duration against the reservation and authorization. Settlement preserves the existing partial-processing charge and refunds unused minutes to their original buckets; expired subscription minutes are discarded rather than becoming permanent prepaid credits.
- `users/{uid}/credit_ledger`: existing reservations/settlements/purchases plus deterministic monthly allowance receipts. `stripe_events/{eventId}` records processed subscription/top-up events.
- `users/{uid}/reel_usage/{lineageId}`: successful-render count and one pending render across a game's rerun history. Only successfully published exports count; failure and timeout release the slot. The latest identical export is returned without rendering or using quota. Keep this document if an ancestor run is deleted, because descendants still share the lineage.
- Run CSV snapshots are compressed Firestore blobs. After downgrade, download returns the latest authorized snapshot; it does not export subsequent edits. Legacy games continue generating CSV and sponsor reels under their original feature rules. The existing latest-reel download remains available after downgrade or source expiry.

All data remains server-only under the existing default-deny Firestore rules. Customer identity, prices, tier and grants cannot be supplied by the browser. Invoices and customers are scoped to the verified Firebase UID. No team workspace, player tracking or public sharing is added.

A subscription's paid monthly or annual invoice authorizes its paid period. Within that period, subscription allowances renew monthly on the subscription-period start day/time in UTC; days such as the 31st clamp to shorter months without drifting the original anchor. Missed allowance windows expire without rollover. Free gives 15 minutes once. Subscription minutes are consumed first, then remaining free minutes, then legacy prepaid, then new prepaid. New top-ups require current paid access. Legacy prepaid customers can upload full games while legacy minutes remain; Free accounts with only new prepaid minutes retain the 15-minute upload limit after cancellation.

Cancellation at period end keeps current access until that date. Failed renewals allow three days to use remaining previous allowance, without granting new minutes. Saved downloads remain available afterward. Accepted processing jobs carry their authorization snapshot and finish even if access changes while they run.

## Verification checklist

Run from the repository root:

```sh
.venv/bin/python -m pytest tests -q
cd frontend
npm run build
```

Automated tests use isolated Stripe and atomic Firestore doubles, not real payments. They exercise read-before-write transactions, concurrent reservations, account isolation, one-time grants, annual month-end resets, skipped months, bucket refunds, payment failures/grace, duplicate/reordered delivery, scheduled changes, Portal restrictions, legacy migration, CSV snapshots and reel quotas/cache/failures.

In staging with real Firebase/Firestore/R2/Cloud Run and Stripe sandbox:

- Purchase Pro and Business monthly and annually; confirm the correct invoice, plan and allowance after webhook delivery. Complete required authentication and test a declined card and abandoned Checkout.
- Repeat a webhook delivery and deliver older events after newer ones. Confirm no extra grants or reverted subscription state. Interrupt a webhook/database operation and verify Stripe retries it.
- Purchase a top-up; confirm exactly 90 minutes once. Cancel or fail payment; confirm zero grant. Free must be denied top-up Checkout.
- Advance Stripe Test Clocks through monthly renewals, annual monthly windows and a renewal failure. Compare paid-through date, three-day grace and allowance resets. No new grant may occur before the new invoice is paid.
- Schedule upgrade, downgrade and both interval directions. Confirm no immediate charge/tier change, correct future invoice and removal of a scheduled change when keeping the current plan or opening Portal. Cancel at period end and confirm downloads remain.
- Submit concurrent analyses/reels, cancel jobs, simulate worker/render failures, and settle a job across an allowance boundary. Confirm no negative balances, expired refunds or duplicate successful-render counts.
- Verify Free/Pro/Business CSV/sponsor/duration gates through direct API requests, including a foreign UID. Verify accepted jobs finish after cancellation. Test migrated legacy balances and new games funded by those credits.
- Inspect `/pricing`, `/account` and review actions on desktop/mobile, with keyboard navigation and reduced motion.

The dated verification report records authenticated Checkout, Portal, webhook, Test Clock and deployed worker checks. Local UI checks use synthetic account data and are separate from those external checks.

Run the explicit real-Stripe clock harness from the repository root with `.venv/bin/python verification/stripe_clock_checks.py`. It requires configured sandbox prices and creates ephemeral Stripe Test Clocks/customers/subscriptions, then removes them afterward. It reuses the test suite's transactional in-memory database and advances only the isolated application's clock. It does not change Firebase accounts, deployed settings or existing customer data. Stripe clocks do not advance Cloud Run's wall clock. The harness refuses live credentials.

## Updated subscription allowances

Pro includes 360 video minutes each month at $29.99/month or $299.90/year. Business includes 900 video minutes each month at $69.99/month or $699.90/year. Free remains 15 minutes once. Game estimates assume 90-minute recordings: around 4 games for Pro and 10 for Business. Usage counts the duration of footage analyzed, including breaks. The separate paid upload limit remains 180 minutes; renders, features, retention and subscription billing rules are unchanged.

Deploy the matching hosted web image (including the rebuilt frontend) and worker image. No Stripe Price changes, balance migration or manual grants are required. New paid subscriptions use the new allowances. Existing allowance windows retain their remaining balances; the next eligible window grants the new plan amount once, replacing expired subscription minutes without rollover. Annual subscribers still receive monthly windows. Window identity uses the subscription ID and window start, so changing the plan definition does not reset an existing balance or produce another receipt. Prepaid/free balances and reservations are preserved. There are no live subscribers at this prelaunch stage. The earlier dated sandbox report records the allowances tested before this update; rerun its sandbox checks for the new release if needed.

## Price revision — 2026-10-13

Pro is now $29.99/month or $299.90/year; Business is $69.99/month or $699.90/year. Allowances remain 360 and 900 minutes, respectively. Create five replacement Stripe sandbox Prices with these amounts and update the corresponding STRIPE_PRICE_* environment variables on both web and worker before deploying the matching code. Existing Price IDs with the old amounts fail Checkout validation. Replace the top-up Price with $9.99 for 90 minutes (pack ID `90`).

Changing configured Price IDs does not migrate existing subscriptions. Before switching an environment with subscribers, arrange an explicit migration and verify webhook reconciliation for old Prices; the current prelaunch environment is documented as having no live subscribers. Preserve the dated verification report as historical evidence and rerun sandbox Checkout and renewal checks with the replacement Prices.

### Earlier deployment verification — 2026-10-13 ($59/$139 catalog)

Created four replacement Stripe sandbox Prices after confirming no nonterminal subscriptions used the old Prices. Cloud Build `8578be6b-06ac-45e2-805a-4fb0579e42fe` succeeded, including container frontend tests/build. Deployed digest `sha256:a2964e4beadf7b09128cc1f7821073a1c18997d34b3a7459452467f358706e97` to both web and worker with matching replacement Price IDs. Web revision `highlights-web-00025-6ds` serves 100% of traffic. Public `/api/billing/plans` verified Pro 59/590/360 and Business 139/1390/900, with subscriptions enabled. Backend tests: 108 passed. Stripe remains in test mode; a complete post-deployment Checkout purchase and renewal were not exercised.


### Decimal pricing rollout — 2026-10-13

Approved catalog: Pro $29.99/month or $299.90/year, Business $69.99/month or $699.90/year, and a subscriber-only $9.99 top-up granting 90 minutes. New top-up pack ID is `90`; the legacy non-subscription credit-pack configuration is unchanged. Dollar amounts are converted using Decimal to exact integer cents for Stripe validation. The UI displays two decimal places for paid monthly and annual totals.

Five replacement Stripe sandbox Prices were created after verifying no nonterminal subscriptions used the replaced Prices. One unpaid open sandbox Checkout using an old Price was expired before cutover. Existing purchased balances are preserved. Deploy matching web/worker through `.github/workflows/build-and-push.yml` on main, then update all five STRIPE_PRICE_* environment values on both resources to the replacement catalog. During that brief configuration transition, mismatched Prices fail closed at Checkout. No live-mode switch is included.

Local validation and visual inspection are recorded in [the decimal pricing report](../verification/decimal-pricing-2026-10-13.md). The earlier Cloud Build verification above describes the superseded catalog.


## Immediate Pro to Business upgrades

Same-frequency tier upgrades use a five-minute server-stored Stripe Invoice preview followed by explicit customer confirmation. The update uses `pending_if_incomplete`, `always_invoice`, the preview proration timestamp and a stable quote idempotency key. Pending card authentication or declined payments preserve Pro access and expose the hosted invoice payment link; paid canonical subscription/invoice state grants Business. A tier upgrade with a different billing frequency remains scheduled for renewal.

Extra minutes are floor-rounded to milli-minutes: `(900 - 360) × remaining current allowance-window seconds / total window seconds`. Add them once to the existing subscription bucket, preserving its window identity, used minutes and reservation funding. A late payment after the allowance window expires receives the current Business window without expired extra minutes. Future full windows grant 900. Upgrade receipts use the paid invoice ID. No Price changes or balance migration are required. Both web and worker must use the matching new image. Existing scheduled upgrades are not automatically applied or charged; the user must request and confirm an immediate quote.

The webhook handler supports `customer.subscription.pending_update_applied` and `customer.subscription.pending_update_expired` in addition to existing subscription-updated/invoice events. Include those events in the Stripe endpoint configuration when configuring billing. An expired projected pending payment is hidden by account responses even if the expiry webhook is delayed.


## Sandbox to live environment transition

1. Run `verification/stripe_live_preflight.py` with the configured live key. It only reads Stripe: payment/payout activation, all five USD Prices, Portal settings and snapshot webhook events/version. It does not create a live payment or Checkout. Webhook signing-secret correspondence cannot be read through Stripe’s API and is confirmed by genuine delivery after activation. Keep synthetic payments in sandbox.
2. Deploy compatible code through GitHub main with the existing sandbox configuration. The default mode stays test; no credentials or data are switched by a code deployment. Stage live secrets separately and pin their versions so sandbox bindings remain available.
3. Enable billing maintenance on the web service and drain analyses/reels and credit reservations. Prepare matching web/worker live configuration with `STRIPE_LIVE_ENABLED=false`, `SUBSCRIPTIONS_ENABLED=true`, the public HTTPS origin, five live Price IDs, live Portal ID and pinned live secret bindings. Maintenance blocks new API mutations.
4. Using the existing dispatcher OIDC identity, POST `{"dry_run":true}` to `/internal/billing/live-transition`. Review the sandbox subscription/prepaid totals and preserved legacy credits. The endpoint defaults to a dry run. It requires live mode, maintenance and live activation disabled, and refuses active jobs/reservations.
5. POST `{"dry_run":false}` to archive old credit snapshots under each credits document’s `environment_archive/stripe-test-v1`, clear sandbox subscription/top-up and expired-allowance buckets, and mark accounts live. Preserve legacy credits, free-trial history, files, saved selections and exports. Original sandbox subscription documents remain unchanged. Repeat safely after interruptions.
6. Separately POST `{"dry_run":false,"verify":true}`. Require `verified:true` and `missing:0` before activation. `maintenance/stripe-live-v1` gates live billing. Live subscription/customer references use `users/{uid}/account/subscription-live`; test mode retains the original `subscription` document. Sandbox subscription IDs cannot be reused for live checkout.
7. Disable this app’s sandbox webhook endpoints, add the two optional pending-update lifecycle events to its live endpoint, enable `STRIPE_LIVE_ENABLED=true` on both web and worker, then remove billing maintenance. Both remain on the same deployed image. Verify public health, plans, authenticated account reads and live endpoint/signature configuration; monitor the first genuine purchase.

Do not roll the database back to sandbox mode or deploy an image predating mode isolation after taking live payments. Use billing maintenance for an emergency pause. Keep the archived snapshots and existing records for audit/explicit recovery; they are not automatically restored. No sandbox subscription is automatically converted, charged or canceled by the account migration.
