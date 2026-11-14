import { useEffect } from "react";
import { Link } from "react-router-dom";
import styles from "./CustomerInfo.module.css";

export const SUPPORT_EMAIL = "elias2.liassides@gmail.com";
const contact = <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>;
const operator = <p>HoopCuts is operated by Elias Liassides, an individual based in the United Kingdom. For support, privacy requests or billing questions, contact {contact}.</p>;
const retention = <>
  <p>Original uploads and review clips are available for 14 days after analysis completes, including cancelled analyses with partial results. Failed analyses and abandoned uploads expire 14 days after creation. Active analysis is protected from automatic cleanup.</p>
  <p>After expiry, you cannot play the original footage or review clips, run analysis again, or create new reels from that footage. Expired media is removed by scheduled cleanup; storage deletion can take longer if a cleanup attempt fails. An export started before expiry may finish afterward.</p>
  <p>Game records, analysis results, selections and your latest successful reel remain until you delete the game. Download any footage or exports you want to keep. Deleting a game removes its records and requests removal of its stored media; failed storage deletions are retried.</p>
</>;

const pages = {
  privacy: {
    title: "Privacy policy",
    intro: "How HoopCuts uses your account information and game footage.",
    sections: [
      ["Who is responsible", operator],
      ["Information we process", <><p>We process your account email and identity, uploaded video and audio, filenames, sponsor logos, analysis results, clip selections, saved reels, usage balances and billing records. Recordings can contain personal information about players, spectators and other people.</p><p>Authentication, hosting and payment providers also process technical information such as IP addresses, device information and security logs. Stripe processes your payment details; HoopCuts uses payment identifiers and subscription status rather than storing your full card details.</p></>],
      ["Why we use it", <><p>We use your information to sign you in, store your private game library, analyze recordings, generate exports, manage subscriptions and respond to requests. Processing needed to provide your account and paid service is based on performing our agreement with you.</p><p>We also use necessary records to prevent abuse, diagnose failures and protect accounts, based on our legitimate interests in operating a secure service. Where applicable, we retain records to meet legal obligations. If processing requires consent, we will ask for it separately.</p></>],
      ["Services that help us run HoopCuts", <><p>Google Firebase and Google Cloud provide authentication, database storage, hosting and processing. Cloudflare R2 stores private media. OpenAI processes sampled frames and analysis instructions to identify candidate highlights. Stripe provides checkout, payments and subscription management. Relevant data is shared with these providers to deliver those functions.</p><p>Your library is private by default. Signed playback and download links provide temporary access, so share them carefully. An operator may access information when needed for support, security or maintenance. Uploading a game does not authorize us to publish it as a sample or use it in marketing; that requires separate permission.</p><p>Our infrastructure is configured for Europe, but providers may process some information outside the UK or Europe. Their privacy information describes their processing and transfer arrangements: <a href="https://policies.google.com/privacy">Google</a>, <a href="https://www.cloudflare.com/privacypolicy/">Cloudflare</a>, <a href="https://openai.com/policies/privacy-policy/">OpenAI</a> and <a href="https://stripe.com/privacy">Stripe</a>. Contact us for information about the arrangements relevant to your data.</p></>],
      ["How long we keep it", <>{retention}<p>Account, usage and billing information is separate from the 14-day media limit. We retain it while needed to provide the service, resolve disputes, prevent abuse or meet applicable record-keeping duties. Contact us to request account deletion or details about a particular record.</p></>],
      ["Browser storage", <p>Authentication uses browser storage to keep you signed in. The interface may also store preferences needed for the app to work. You can clear site data in your browser; this may sign you out or reset preferences.</p>],
      ["Your choices and rights", <><p>You can delete games in your library. To request account deletion, access, correction, a copy of your personal information, or to object to or restrict processing, email {contact}. We may need to verify your identity. Rights depend on the circumstances, and some records may need to be retained under law.</p><p>You can also raise a concern with the <a href="https://ico.org.uk/make-a-complaint/">UK Information Commissioner’s Office</a> or your local data protection authority. If you appear in a recording and have a privacy concern, contact us with enough information to identify the game. Please do not send another person’s footage by email.</p></>],
      ["Changes to this policy", <p>We will update this page when our processing changes. For material changes affecting existing accounts, we will provide notice through the service or account email.</p>],
    ],
  },
  terms: {
    title: "Terms of service",
    intro: "The agreement for using your HoopCuts account and uploading games.",
    sections: [
      ["Who provides the service", operator],
      ["Your account", <p>By creating an account, you agree to these terms. Keep your login secure and provide an email you can access. Plans are for one account. If you are not old enough to enter an agreement in your country, a parent or guardian must manage the account and uploads for you.</p>],
      ["Permission to upload and share", <><p>You retain your rights in your recordings and logos. You must own them or have permission to upload, process and use them. Make sure you have any permissions required from the recording owner, teams, venue and people shown, including appropriate permission for footage involving children.</p><p>You give HoopCuts permission to store, copy, analyze and edit your uploads only as needed to provide and maintain the service. This includes using our processing and storage providers. You are responsible for permissions to publish downloaded reels, game audio and sponsor logos on other platforms.</p><p>Do not upload unlawful content, infringe copyright or privacy, impersonate others, or misuse the service. We may restrict access or remove content to address misuse, security risks or legal obligations. Where practical, we will explain the reason and let you contact us about it.</p></>],
      ["Analysis and exports", <p>Automatic analysis produces candidate highlights and can miss plays or select the wrong moments. Review your clips before exporting. Results depend on recording quality and game conditions; a particular number of highlights or processing time is not guaranteed.</p>],
      ["Minutes, subscriptions and cancellation", <><p>The pricing page and checkout show the price, billing interval, allowance and limits before purchase. Subscriptions renew automatically unless cancelled. Annual plans are charged upfront; their video-minute allowances still refresh monthly. Unused subscription minutes do not roll over. Top-ups require a subscription and are separate from the monthly allowance.</p><p>Analysis uses video minutes for footage processed, including breaks. Running analysis again uses minutes again. We reserve minutes when analysis starts and release unused reserved minutes after settlement. An expired subscription allowance cannot become permanent prepaid credit.</p><p>Manage cancellation through your account’s subscription management link. Cancellation, downgrades and billing-frequency changes take effect at the end of the paid period. Pro to Business upgrades within the current billing frequency activate after Stripe confirms the prorated payment. Before confirming, you see the charge and the prorated extra minutes for the remaining monthly allowance window. Used and reserved minutes remain accounted for. Contact us if you cannot access subscription management. See the <Link to="/refunds">refund policy</Link> for billing problems and cancellation rights.</p></>],
      ["Footage retention", retention],
      ["Privacy and service availability", <p>Our <Link to="/privacy">privacy policy</Link> explains how we handle your information. Processing can be interrupted by service failures or provider limits. If something fails, contact support with the game name and error message. Keep your own copy of important recordings and downloaded reels.</p>],
      ["Your legal rights and changes", <><p>These terms do not exclude or limit rights or remedies that cannot lawfully be excluded, including applicable consumer rights. Contact us to resolve a concern about the service or these terms.</p><p>We may update these terms as the service changes. Material changes affecting existing accounts will be communicated through the service or account email before taking effect. Updated terms do not remove rights relating to an earlier purchase.</p></>],
    ],
  },
  refunds: {
    title: "Refunds and cancellation",
    intro: "How to stop renewal or ask us to review a payment.",
    sections: [
      ["Cancel a subscription", <p>Open Account, choose Manage subscription, then cancel in the billing portal. Cancellation stops the next renewal; your current plan continues until the end of the paid period. Cancelling renewal does not automatically refund a previous payment. If you cannot cancel in the portal, contact {contact}.</p>],
      ["Billing errors and service failures", <><p>Contact {contact} if you were charged incorrectly, have a duplicate charge, or a service failure prevented you from using a purchase. We review discretionary refund requests case by case, considering the payment, usage and problem reported.</p><p>Include your account email, payment date, receipt reference if available, and a short explanation. Do not send card numbers, passwords or full recordings. Refunds that we approve are returned through Stripe to the original payment method; timing depends on your payment provider.</p></>],
      ["Analysis minutes and money refunds", <p>Releasing unused reserved analysis minutes adjusts your app balance; it is separate from refunding money. A partially processed recording can use minutes for the footage successfully analyzed. If the charge or balance looks wrong, contact us so we can review the run.</p>],
      ["Consumer cancellation rights", <><p>Any statutory cancellation or refund rights you have remain available. For UK consumers, an online service purchase generally has a 14-day cancellation period from the date the agreement is made, subject to the rules that apply to that purchase. Contact us to exercise those rights. This policy does not treat using the app as a blanket waiver of them.</p><p>You can email: “I wish to cancel my HoopCuts purchase”, followed by your name, account email, purchase date and receipt reference if available. You do not need to use this wording to make a valid request. Where the law requires a refund or another remedy, that takes priority over our discretionary review policy.</p></>],
    ],
  },
  support: {
    title: "Contact support",
    intro: "Help with your game, account or payment.",
    sections: [
      ["Email HoopCuts", <><p>Contact {contact}. HoopCuts is operated by Elias Liassides in the United Kingdom. This inbox also handles privacy, deletion and refund requests.</p><p>Include your account email, game name if relevant, what happened and any error message. For payments, include the receipt reference and payment date. Never send your password, full card details or private game footage by email.</p></>],
      ["Footage or privacy concerns", <p>If you appear in uploaded footage or believe a recording was uploaded without permission, email us with details that help identify it. You can also request account deletion or access to your information. See our <Link to="/privacy">privacy policy</Link>.</p>],
      ["Before your footage expires", <p>Original uploads and review clips expire 14 days after analysis completes. Failed analyses and abandoned uploads expire 14 days after creation. Download what you need before the deadline shown in your game. Your latest successful reel remains available until you delete the game.</p>],
      ["Billing and cancellations", <p>You can stop renewal in Account → Manage subscription. For a disputed charge or service failure, see our <Link to="/refunds">refund policy</Link> and contact us for a review.</p>],
    ],
  },
};

export type InfoPage = keyof typeof pages;
export function CustomerLinks() {
  return <nav className={styles.links} aria-label="Customer information"><Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link><Link to="/refunds">Refunds</Link><Link to="/support">Support</Link></nav>;
}

export function CustomerInfo({ page }: { page: InfoPage }) {
  const content = pages[page];
  useEffect(() => {
    window.scrollTo(0, 0);
    document.getElementById("customer-page-title")?.focus();
  }, [content]);
  return <main className={styles.page}>
    <header className={styles.header}><Link className={styles.brand} to="/">HoopCuts</Link><Link to="/">Back to overview</Link></header>
    <div className={styles.layout}>
      <aside><CustomerLinks /><p>Updated {page === "terms" ? "14 October 2026" : "13 October 2026"}</p></aside>
      <article className={styles.article}>
        <h1 id="customer-page-title" tabIndex={-1}>{content.title}</h1><p className={styles.intro}>{content.intro}</p>
        {content.sections.map(([title, body]) => <section key={String(title)}><h2>{title}</h2>{body}</section>)}
      </article>
    </div>
    <footer className={styles.footer}><CustomerLinks /></footer>
  </main>;
}
