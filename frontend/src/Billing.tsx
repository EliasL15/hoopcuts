import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { SUPPORT_EMAIL } from "./CustomerInfo";
import type { Account, BillingInterval, PaidPlan, ProductApi, UpgradeQuote } from "./types";
import styles from "./Billing.module.css";

function Failure({ error }: { error: unknown }) {
  return error ? <p className="notice notice--error" role="alert">{error instanceof Error ? error.message : "Billing is unavailable. Try again shortly."}</p> : null;
}

function date(value?: string | null) {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value)) : "Awaiting payment confirmation";
}

export function PricingSection({ api, signedIn }: { api: ProductApi; signedIn: boolean }) {
  const [interval, setInterval] = useState<BillingInterval>("monthly");
  const [message, setMessage] = useState("");
  const [quote, setQuote] = useState<UpgradeQuote | null>(null);
  const [paymentUrl, setPaymentUrl] = useState<string | null>(null);
  const [waitingSince, setWaitingSince] = useState<number | null>(null);
  const quoteHeading = useRef<HTMLHeadingElement>(null);
  const intervalChosen = useRef(false);
  useEffect(() => { if (quote) { quoteHeading.current?.focus(); quoteHeading.current?.scrollIntoView({ block: "center" }); } }, [quote]);
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const plans = useQuery({ queryKey: [api.mode, "plans"], queryFn: api.plans!, enabled: Boolean(api.plans) });
  const account = useQuery({ queryKey: [api.mode, "account"], queryFn: api.account!, enabled: signedIn && Boolean(api.account), refetchOnWindowFocus: true,
    refetchInterval: query => query.state.data?.pending_upgrade || (waitingSince && Date.now() - waitingSince < 90000 && query.state.data?.plan !== "business") ? 5000 : false });
  useEffect(() => {
    if (!intervalChosen.current && account.data?.interval) {
      intervalChosen.current = true;
      setInterval(account.data.interval);
    }
  }, [account.data?.interval]);
  const preview = useMutation({ mutationFn: (plan: PaidPlan) => api.previewUpgrade!(plan, interval), onSuccess: result => { setQuote(result); setMessage(""); setPaymentUrl(null); } });
  const change = useMutation({ mutationFn: async ({ plan, frequency, quoteId }: { plan: PaidPlan; frequency: BillingInterval; quoteId?: string }) => {
    if (account.data?.has_subscription) {
      const result = await api.changeSubscription!(plan, frequency, quoteId);
      setQuote(null);
      setPaymentUrl(result.payment_url || null);
      setMessage(result.status === "payment_required" ? "Complete the upgrade payment in Stripe. Your current plan remains active until payment succeeds."
        : result.status === "upgraded" ? "Your Business upgrade is active. Your remaining minutes include the prorated upgrade allowance."
        : result.status === "confirming" ? "Your upgrade is being confirmed. Your plan and minutes update after Stripe confirms payment."
        : result.scheduled_change ? `Your change to ${plan === "pro" ? "Pro" : "Business"} takes effect on ${date(result.scheduled_change.effective_at)}.` : "Your scheduled change has been removed.");
      if (result.status) setWaitingSince(Date.now());
      await queryClient.invalidateQueries({ queryKey: [api.mode, "account"] });
    } else {
      await api.subscribe!(plan, frequency);
    }
  }});
  const choose = (plan: PaidPlan) => {
    setQuote(null); setMessage(""); setPaymentUrl(null); preview.reset(); change.reset();
    if (account.data?.has_subscription && account.data.plan === "pro" && plan === "business" && account.data.interval === interval) preview.mutate(plan);
    else change.mutate({ plan, frequency: interval });
  };
  return <section id="pricing" className={styles.pricingSection} aria-labelledby="pricing-title">
      <div className={styles.intro}><h2 id="pricing-title">More game film.<br />More moments to share.</h2><p>Choose the video minutes you need. Review every moment, then export a reel ready to post.</p></div>
      <div className={styles.billingChoice}><div className={styles.toggle} role="group" aria-label="Billing frequency">{(["monthly", "annual"] as const).map(value => <button key={value} type="button" aria-pressed={interval === value} onClick={() => { intervalChosen.current = true; setInterval(value); setQuote(null); preview.reset(); }}>{value === "monthly" ? "Monthly" : "Annual"}</button>)}</div><span>Annual saves 16.7%</span></div>
      {params.get("payment") === "cancelled" && <p className="notice">Checkout was cancelled. You can choose a plan when you’re ready.</p>}
      {message && <p className="notice notice--success" role="status">{message}</p>}
      {paymentUrl && account.data?.plan !== "business" && <p><a className="button" href={paymentUrl}>Complete upgrade payment in Stripe</a></p>}
      {account.data?.pending_upgrade && !paymentUrl && <p className="notice">Your upgrade is awaiting payment. {account.data.pending_upgrade.payment_url && <a href={account.data.pending_upgrade.payment_url}>Complete payment in Stripe</a>}</p>}
      <Failure error={plans.error || account.error || preview.error || change.error} />
      {preview.isPending && <p role="status">Getting your upgrade quote from Stripe…</p>}
      {quote && <section className={styles.upgradeQuote} aria-labelledby="upgrade-quote-title">
        <h3 id="upgrade-quote-title" tabIndex={-1} ref={quoteHeading}>Upgrade to Business now</h3>
        <p>Stripe will charge <strong>{new Intl.NumberFormat(undefined, { style: "currency", currency: quote.currency }).format(quote.amount_due / 100)}</strong> now for the remaining {quote.interval === "annual" ? "annual" : "monthly"} paid period. Your billing date stays the same.</p>
        <p>After payment succeeds, Business features activate and <strong>{quote.extra_minutes.toFixed(2)} extra minutes</strong> are added for the rest of your current allowance window, until {date(quote.allowance_end)}. Your used and reserved minutes stay accounted for. Future full windows include 900 minutes.</p>
        {account.data?.scheduled_change && <p>Confirming this upgrade removes your previously scheduled change.</p>}
        <p className={styles.note}>Quote valid for five minutes. Stripe may ask you to authenticate your card.</p>
        <div className={styles.actions}><button disabled={change.isPending} onClick={() => change.mutate({ plan: quote.plan, frequency: quote.interval, quoteId: quote.quote_id })}>{change.isPending ? "Confirming upgrade…" : "Confirm and pay for upgrade"}</button><button className="button--ghost" disabled={change.isPending} onClick={() => setQuote(null)}>Keep current plan</button></div>
      </section>}
      {plans.isLoading && <p role="status">Loading plans…</p>}
      {plans.data && !plans.data.enabled && <p className="notice">Subscriptions are coming soon. Existing prepaid accounts can continue using their credits.</p>}
      <div className={styles.comparison}>{plans.data?.plans.map(plan => {
        const current = account.data?.plan === plan.id;
        const exact = current && (plan.id === "free" || account.data?.interval === interval);
        return <section key={plan.id} className={`${styles.plan} ${plan.id === "pro" ? styles.recommended : ""}`} aria-labelledby={`plan-${plan.id}`}>
          <div className={styles.planHeading}><h3 id={`plan-${plan.id}`}>{plan.name}</h3><span>{current ? "Current plan" : plan.id === "pro" ? "For players & families" : plan.id === "business" ? "For frequent creators" : "Try your footage"}</span></div>
          <p className={styles.price}>${plan.id === "free" ? 0 : interval === "annual" ? (plan.annual / 12).toFixed(2) : plan.monthly.toFixed(2)}<span>/month</span></p>
          <p className={styles.billed}>{plan.id === "free" ? "No card required" : interval === "annual" ? `$${plan.annual.toFixed(2)} charged annually` : `$${plan.monthly.toFixed(2)} charged monthly`}</p>
          {plan.id !== "free" && <p>Around {Math.floor(plan.minutes / 90)} games per month</p>}
          <p className={styles.allowance}><strong>{plan.minutes}</strong> video minutes{plan.id === "free" ? " once" : " each month"}</p>
          <ul><li>Automatic analysis and clip review</li><li>Vertical and landscape reels</li><li>{plan.renders} successful {plan.renders === 1 ? "render" : "renders"} per game</li><li>Uploads up to {plan.id === "free" ? 15 : 180} minutes</li><li>14-day footage retention</li><li>{plan.sponsors ? "End card with up to two sponsor logos" : "Sponsor end cards on Business"}</li></ul>
          {plan.id === "free" ? <Link className="button button--quiet" to={signedIn ? "/library" : "/sign-in?mode=create"}>{signedIn ? "Open game library" : "Try Free"}</Link> : !signedIn ? <Link className="button" to="/sign-in">Sign in to subscribe</Link> : <button disabled={!plans.data.enabled || account.isLoading || account.isError || preview.isPending || change.isPending || Boolean(account.data?.pending_upgrade) || exact} onClick={() => choose(plan.id as PaidPlan)}>{(change.isPending && change.variables?.plan === plan.id) || (preview.isPending && preview.variables === plan.id) ? "Opening billing…" : exact ? "Current plan" : account.data?.has_subscription ? current ? `Switch to ${interval === "annual" ? "annual" : "monthly"}` : account.data.plan === "pro" && plan.id === "business" && account.data.interval === interval ? "Upgrade to Business now" : `Change to ${plan.name} at renewal` : `Choose ${plan.name}`}</button>}
        </section>;
      })}</div>
      <section className={styles.customPlan} aria-labelledby="custom-plan-title">
        <div>
          <h3 id="custom-plan-title">Need a custom plan?</h3>
          <p>Running a club, league or basketball business? Tell us how much footage you need to process, and we can discuss a package that fits.</p>
        </div>
        <div className={styles.customPlanAction}>
          <a className="button button--quiet" href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent("HoopCuts custom plan enquiry")}&body=${encodeURIComponent("Hi Elias,\n\nI'd like to discuss a custom HoopCuts plan.\n\nOrganisation: \nEstimated footage per month (hours or games): \nWhat we need: \n\nName: \nContact email: \n")}`}>Discuss a custom plan</a>
          <span>Opens your email app</span>
        </div>
      </section>
      <section className={styles.details}><h3>Clear limits. Your footage stays private.</h3><div><p>Game estimates assume 90-minute recordings. Usage counts the duration of footage analyzed, including breaks. Trim halftime and other long breaks before uploading to make your minutes go further. Running analysis again uses minutes again. Subscription minutes reset monthly and do not roll over, including on annual plans.</p><p>Subscribers can buy 90 extra minutes for $9.99. There are no automatic overage charges. Pro to Business upgrades within your current billing frequency take effect after payment, with a prorated charge and extra minutes for the remaining allowance window. Downgrades, billing-frequency changes and cancellations take effect at the end of your paid period.</p><p>Original footage and review clips last 14 days. Your latest saved reel remains available. Every plan is for one account; Business does not include a shared team workspace.</p></div></section>
      <p className={styles.note}>Subscriptions renew automatically until cancelled. Before purchasing, read our <Link to="/terms">Terms</Link>, <Link to="/privacy">Privacy policy</Link> and <Link to="/refunds">refund and cancellation policy</Link>.</p>
      <footer className={styles.footer}><p>Already have a plan?</p><Link className="button button--quiet" to={signedIn ? "/account" : "/sign-in"}>Manage your account</Link></footer>
  </section>;
}

function Balances({ account }: { account: Account }) {
  return <dl className={styles.balances}>{[
    ["Subscription minutes", account.subscription_minutes], ["Free trial minutes", account.free_minutes],
    ["Prepaid minutes", account.prepaid_minutes], ["Legacy prepaid minutes", account.legacy_minutes],
    ["Reserved for analysis", account.reserved_minutes],
  ].map(([label, value]) => <div key={String(label)}><dt>{label}</dt><dd>{Number(value || 0).toFixed(1)}</dd></div>)}</dl>;
}

export function AccountPage({ api }: { api: ProductApi }) {
  const [openedAt] = useState(() => Date.now());
  const [params] = useSearchParams();
  const account = useQuery({ queryKey: [api.mode, "account"], queryFn: api.account!, refetchOnWindowFocus: true,
    refetchInterval: () => params.get("payment") === "success" && Date.now() - openedAt < 90000 ? 5000 : false });
  const portal = useMutation({ mutationFn: () => api.portal!() });
  const topup = useMutation({ mutationFn: () => api.checkout!("90") });
  const cancelChange = useMutation({ mutationFn: () => api.changeSubscription!(account.data!.subscription_plan as PaidPlan, account.data!.interval!), onSuccess: () => account.refetch() });
  const data = account.data;
  return <main className={styles.account}><Link to="/library">Back to game library</Link><h1>Your plan and minutes</h1>
    {params.get("payment") === "success" && <p className="notice" role="status">Checkout completed. Your plan and minutes update after payment is confirmed. <button className="text-button" disabled={account.isFetching} onClick={() => account.refetch()}>Refresh balance</button></p>}
    {params.get("payment") === "cancelled" && <p className="notice">Checkout was cancelled.</p>}
    {data?.pending_upgrade && <div className="notice"><p>Your Business upgrade is awaiting payment. Your current plan remains active until payment succeeds.</p>{data.pending_upgrade.payment_url && <a className="button" href={data.pending_upgrade.payment_url}>Complete upgrade payment in Stripe</a>}<button className="text-button" disabled={account.isFetching} onClick={() => account.refetch()}>Refresh plan</button></div>}
    <Failure error={account.error || portal.error || topup.error || cancelChange.error} />
    {account.isLoading && <p role="status">Loading your account…</p>}
    {data && <>
      <section className={styles.summary}><div><h2>{data.plan ? data.plan === "free" ? "Free" : data.plan === "pro" ? "Pro" : "Business" : "Prepaid"}</h2><p>{data.status === "past_due" ? `Payment needs attention. Remaining allowance is available until ${date(data.grace_until)}.` : data.has_subscription ? `${data.interval === "annual" ? "Annual" : "Monthly"} subscription` : "Your video minutes are ready to use."}</p>
        {data.renewal_at && <p>{data.cancel_at_period_end ? "Subscription ends" : "Paid period ends"} {date(data.renewal_at)}</p>}
        {data.allowance_end && <p>Monthly allowance ends {date(data.allowance_end)}. Unused subscription minutes do not roll over.</p>}
      </div><div className={styles.total}><strong>{data.available_minutes.toFixed(1)}</strong><span>video minutes available</span></div></section>
      <Balances account={data} />
      {data.scheduled_change && <div className="notice"><p>Scheduled: {data.scheduled_change.plan === "pro" ? "Pro" : "Business"}, billed {data.scheduled_change.interval === "annual" ? "annually" : "monthly"}, from {date(data.scheduled_change.effective_at)}.</p><button className="button--quiet" disabled={cancelChange.isPending} onClick={() => cancelChange.mutate()}>Keep current plan</button></div>}
      <div className={styles.actions}><Link className="button" to="/#pricing">{data.has_subscription ? "Change plan" : "Compare plans"}</Link>
        {data.can_manage && <button className="button--quiet" disabled={portal.isPending} onClick={() => portal.mutate()}>{portal.isPending ? "Opening…" : "Manage subscription"}</button>}
        {data.can_top_up && <button className="button--quiet" disabled={topup.isPending} onClick={() => topup.mutate()}>{topup.isPending ? "Opening checkout…" : "Add 90 minutes · $9.99"}</button>}
      </div>
      {data.scheduled_change && <p className={styles.note}>Opening subscription management removes your scheduled plan change so you can manage payment details or cancel your current subscription.</p>}
      <p className={styles.note}>New top-ups are available to subscribers. Existing prepaid minutes remain valid; legacy-funded games retain their original export features.</p>
    </>}
    <p className={styles.note}>Questions about a charge or your account? <Link to="/support">Contact support</Link> or read our <Link to="/refunds">refund and cancellation policy</Link>.</p>
  </main>;
}
