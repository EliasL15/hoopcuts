import {
  ArrowLeft,
  ArrowRight,
  Basketball,
  Check,
  Clock,
  Coins,
  DownloadSimple,
  FilmStrip,
  Funnel,
  LockKey,
  MagnifyingGlass,
  Play,
  Repeat,
  ShieldCheck,
  SignOut,
  Trash,
  UploadSimple,
  X,
} from "@phosphor-icons/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { User } from "@firebase/auth";
import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import type { ClipEvent, ProductApi, RunDetail, RunSummary, UploadPhase } from "./types";
import { defaultFreeThrowFilter, includedInReel } from "./clipSelection";
import styles from "./App.module.css";
import { Seo } from "./Seo";
import { CustomerInfo, CustomerLinks } from "./CustomerInfo";
import infoStyles from "./CustomerInfo.module.css";
import { PricingSection, AccountPage } from "./Billing";
import { videoDuration } from "./api";
import { estimateRemaining, formatEstimate, observeProgress, type EstimateState } from "./progressEstimate";
import { cloudComputeCost } from "./costs";
import { analysisStage, analysisPasses } from "./analysisProgress";

export type AuthActions = {
  signInWithGoogle: () => Promise<void>;
  signIn: (email: string, password: string) => Promise<void>;
  createAccount: (email: string, password: string) => Promise<void>;
  resetPassword: (email: string) => Promise<void>;
  signOut: () => Promise<void>;
};

type ProductAppProps = {
  api: ProductApi;
  configured: boolean;
  user: User | null;
  authReady: boolean;
  authActions?: AuthActions;
};

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <Link to="/" className={styles.brand} aria-label="HoopCuts home">
      <img className={styles.brandMark} src="/assets/brand/hoopcuts-symbol.svg" alt="" width="42" height="42" />
      <span>
        <strong>HoopCuts</strong>
        {!compact && <small>Full games. Shareable highlights.</small>}
      </span>
    </Link>
  );
}

export function Landing({ api, signedIn }: { api: ProductApi; signedIn: boolean }) {
  const { hash } = useLocation();
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView();
  }, [hash]);
  return (
    <main className={styles.landing}>
      <header className={styles.publicNav}>
        <Brand />
        <nav aria-label="Primary navigation">
          <a href="#workflow">How it works</a>
          <a href="#pricing">Pricing</a>
          <Link className="button button--quiet" to={signedIn ? "/library" : "/sign-in"}>
            {signedIn ? "Open library" : "Sign in"}
          </Link>
        </nav>
      </header>

      <section className={styles.hero}>
        <div className={styles.heroCopy}>
          <h1>Turn full basketball games into highlight reels worth sharing.</h1>
          <p>
            Upload your full game and let us find the highlights. Choose your moments, add optional
            sponsor logos, and download a reel ready for Instagram and TikTok.
          </p>
          <div className={styles.heroActions}>
            <Link className="button" to={signedIn ? "/library" : "/sign-in?mode=create"}>
              {signedIn ? "Open your library" : "Create your first reel"}<ArrowRight weight="bold" />
            </Link>
            <a className="button button--ghost" href="#workflow">See how it works</a>
          </div>
          <ul className={styles.assurances} aria-label="Product assurances">
            <li><LockKey /> Your footage stays private</li>
            <li><Check /> You choose the highlights</li>
            <li><FilmStrip /> Vertical reels for social</li>
          </ul>
        </div>
      </section>

      <section className={styles.demoIntro} id="sample-game" aria-labelledby="landing-demo-title">
        <div>
          <h2 id="landing-demo-title">Explore a sample game</h2>
          <p>Watch clips from the first quarter of a real basketball game. Try selecting moments and see the finished example reel.</p>
          <p>{signedIn ? "Your analysis minutes stay available for your own footage." : "Create a free account to explore the sample. Your free minutes stay available for your own footage."}</p>
        </div>
        <Link className="button button--quiet" to={signedIn ? "/demo" : "/sign-in?mode=create"}>
          <Play weight="fill" />{signedIn ? "Open sample game" : "Try the sample free"}
        </Link>
      </section>

      <section className={styles.workflow} id="workflow">
        <div className={styles.sectionIntro}>
          <h2>Upload a game. Leave with a reel.</h2>
          <p>We find the moments. You decide what makes the final cut.</p>
        </div>
        <ol className={styles.workflowTrack}>
          <li><span>1</span><div><strong>Upload your full game</strong><p>Add your basketball recording. We analyze the footage to find highlight moments.</p></div></li>
          <li><span>2</span><div><strong>Pick your highlights</strong><p>Review the clips we find and choose the plays you want in your reel.</p></div></li>
          <li><span>3</span><div><strong>Download and post</strong><p>Export a vertical reel for Instagram or TikTok, with an optional sponsor end card.</p></div></li>
        </ol>
      </section>

      <section className={styles.trustBand}>
        <div>
          <ShieldCheck weight="duotone" />
          <h2>Give your sponsors a place in the reel.</h2>
        </div>
        <p>
          Add up to two sponsor logos to an optional end card when you export. Your game library
          stays private, and plans are based on the video minutes you analyze.
        </p>
      </section>

      <PricingSection api={api} signedIn={signedIn} />

      <section className={styles.finalCta}>
        <div><h2>Make the next game a reel worth posting.</h2><p>Upload the full game. Pick your highlights. Get your reel ready to share.</p></div>
        <Link className="button" to={signedIn ? "/library" : "/sign-in?mode=create"}>
          {signedIn ? "Open your library" : "Create your first reel"}<ArrowRight weight="bold" />
        </Link>
      </section>

      <footer className={styles.footer}><Brand compact /><CustomerLinks /></footer>
    </main>
  );
}

function AuthPage({ configured, actions, signedIn }: { configured: boolean; actions?: AuthActions; signedIn: boolean }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const creating = params.get("mode") === "create";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (signedIn) return <Navigate to="/library" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!actions) return;
    if (creating && password !== confirmation) {
      setError("Passwords do not match. Enter the same password in both fields.");
      return;
    }
    setBusy(true); setError(""); setMessage("");
    try {
      if (creating) {
        await actions.createAccount(email, password);
        setMessage("Account created. Check your inbox, verify your email, then sign in.");
        setParams({});
      } else {
        await actions.signIn(email, password);
        navigate("/library", { replace: true });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Authentication failed.");
    } finally {
      setBusy(false);
    }
  }

  async function googleSignIn() {
    if (!actions) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await actions.signInWithGoogle();
      navigate("/library", { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not sign in with Google.");
    } finally { setBusy(false); }
  }

  async function resetPassword() {
    if (!actions) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await actions.resetPassword(email);
      setMessage("If that email has an account, a password reset link is on its way.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not send the reset email.");
    } finally { setBusy(false); }
  }

  return (
    <main className={styles.authPage}>
      <header className={styles.authNav}><Brand /><Link to="/">Back to overview</Link></header>
      <section className={styles.authLayout}>
        <div className={styles.authContext}>
          <span className={styles.authIcon}><FilmStrip weight="duotone" /></span>
          <h1>Your private film room.</h1>
          <p>Return to saved games, review candidate clips, and build the reel at your own pace.</p>
          <ul><li><ShieldCheck /> Source videos stay private</li><li><Check /> Selections save as you review</li></ul>
        </div>
        <div className={styles.authFormWrap}>
          <form className={styles.authForm} onSubmit={submit}>
            <div><h2>{creating ? "Create your account" : "Welcome back"}</h2><p>{creating ? "Start a private game library." : "Sign in to open your saved runs."}</p></div>
            {!configured && <div className="notice notice--error">Firebase Authentication is not configured yet.</div>}
            <label>Email address<input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
            <label>Password<input type="password" autoComplete={creating ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} minLength={6} required /></label>
            {creating && <label>Confirm password<input type="password" autoComplete="new-password" value={confirmation} onChange={(e) => setConfirmation(e.target.value)} minLength={6} required /></label>}
            <p className={infoStyles.notice}>By creating an account with email or Google, you agree to the <Link to="/terms">Terms</Link>. Read our <Link to="/privacy">Privacy policy</Link> to understand how your information and footage are processed.</p>
            {error && <div className="notice notice--error" role="alert">{error}</div>}
            {message && <div className="notice notice--success" role="status">{message}</div>}
            <button type="submit" disabled={busy || !configured}>{busy ? "Working…" : creating ? "Create account" : "Sign in"}<ArrowRight /></button>
            <button className="button--quiet" type="button" onClick={googleSignIn} disabled={busy || !configured}>Continue with Google</button>
            {!creating && <button className="text-button" type="button" onClick={resetPassword} disabled={busy}>Reset password</button>}
            <p className={styles.authSwitch}>
              {creating ? "Already have an account?" : "New to HoopCuts?"}{" "}
              <button type="button" className="text-button" onClick={() => { setParams(creating ? {} : { mode: "create" }); setError(""); setMessage(""); }}>
                {creating ? "Sign in" : "Create one"}
              </button>
            </p>
          </form>
        </div>
      </section>
      <footer className={styles.footer}><CustomerLinks /></footer>
    </main>
  );
}

function LoadingScreen() {
  return <div className="boot-message"><span className="loader" aria-hidden="true" /><p>Opening your film room…</p></div>;
}

function RequireAccess({ mode, user, authReady, children }: { mode: ProductApi["mode"]; user: User | null; authReady: boolean; children: ReactNode }) {
  if (mode === "local") return children;
  if (!authReady) return <LoadingScreen />;
  return user ? children : <Navigate to="/sign-in" replace />;
}

function AccountTools({ api, onSignOut }: { api: ProductApi; onSignOut?: () => Promise<void> }) {
  const account = useQuery({ queryKey: [api.mode, "account"], queryFn: api.account!, enabled: Boolean(api.account) });
  const packs = useQuery({ queryKey: [api.mode, "packs"], queryFn: api.creditPacks!, enabled: Boolean(api.creditPacks) });
  const [pack, setPack] = useState("");
  const checkout = useMutation({ mutationFn: () => api.checkout!(pack || packs.data?.[0]?.id || "60") });

  return (
    <div className={styles.accountTools}>
      <Link className={styles.accountLink} to="/account">Account</Link>
      <Link className={styles.accountLink} to="/#pricing">Plans</Link>
      {account.data && <span className={styles.creditBalance}><Coins weight="duotone" /><strong>{account.data.available_minutes.toFixed(1)}</strong> min available <i /> {account.data.reserved_minutes.toFixed(1)} reserved</span>}
      {packs.data?.length && !account.data?.subscriptions_enabled ? (
        <div className={styles.creditBuy}>
          <label className="sr-only" htmlFor="credit-pack">Credit pack</label>
          <select id="credit-pack" value={pack} onChange={(e) => setPack(e.target.value)}>
            {packs.data.map((item) => <option key={item.id} value={item.id}>{item.minutes} minutes</option>)}
          </select>
          <button className="button--quiet" onClick={() => checkout.mutate()} disabled={checkout.isPending}>Buy credits</button>
        </div>
      ) : null}
      <ErrorNotice error={checkout.error} />
      <button className="icon-button" onClick={onSignOut} aria-label="Sign out" title="Sign out"><SignOut /></button>
    </div>
  );
}

function AppFrame({ api, authActions, children }: { api: ProductApi; authActions?: AuthActions; children: ReactNode }) {
  return (
    <div className={styles.appShell}>
      <header className={styles.appHeader}>
        <Brand />
        {api.mode === "hosted" ? <AccountTools api={api} onSignOut={authActions?.signOut} /> : <span className={styles.localBadge}><LockKey /> Local studio · files stay on this Mac</span>}
      </header>
      {children}
      {api.mode === "hosted" && <footer className={styles.footer}><CustomerLinks /></footer>}
    </div>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="notice notice--error" role="alert">{error instanceof Error ? error.message : "Something went wrong."}</div>;
}

function useDialogFocus(ref: React.RefObject<HTMLElement | null>, canClose: boolean, onClose: () => void) {
  const canCloseRef = useRef(canClose);
  const onCloseRef = useRef(onClose);
  canCloseRef.current = canClose;
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!canClose) ref.current?.focus();
  }, [canClose, ref]);

  useEffect(() => {
    const returnTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusableSelector = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const dialog = ref.current;
    const initialTarget = dialog?.querySelector<HTMLElement>(focusableSelector) || dialog;
    initialTarget?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && canCloseRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !ref.current) return;
      const focusable = Array.from(ref.current.querySelectorAll<HTMLElement>(focusableSelector));
      if (!focusable.length) {
        event.preventDefault();
        ref.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      returnTarget?.focus();
    };
  }, [ref]);
}

function UploadDialog({ api, onClose }: { api: ProductApi; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase | "">("");
  const [hasPermission, setHasPermission] = useState(false);
  const [metadata, setMetadata] = useState<{ file: File; seconds?: number; error?: Error } | null>(null);
  const account = useQuery({ queryKey: [api.mode, "account"], queryFn: api.account!,
    enabled: api.mode === "hosted" && Boolean(api.account), refetchOnMount: "always", refetchOnWindowFocus: true });
  useEffect(() => {
    if (!file || api.mode !== "hosted") return;
    let active = true;
    videoDuration(file).then(seconds => { if (active) setMetadata({ file, seconds }); })
      .catch(error => { if (active) setMetadata({ file, error: error instanceof Error ? error : new Error("Could not read the video duration.") }); });
    return () => { active = false; };
  }, [file, api.mode]);
  const checking = Boolean(file && api.mode === "hosted" && (metadata?.file !== file || account.isFetching));
  const seconds = metadata?.file === file ? metadata.seconds : undefined;
  const required = seconds === undefined ? undefined : Math.ceil(seconds / 60 * 1000) / 1000;
  const available = account.data?.available_minutes;
  const limit = account.data?.entitlements?.max_minutes;
  const eligibilityError = api.mode !== "hosted" || !file ? null
    : metadata?.file === file && metadata.error ? metadata.error
    : account.error ? new Error("Could not check your credits. Try again before uploading.")
    : required !== undefined && available !== undefined && required > available
      ? new Error(`Not enough credits. This video needs ${required} video minutes; you have ${available} available. Add minutes or choose a shorter video.`)
    : seconds !== undefined && limit !== undefined && seconds / 60 > limit
      ? new Error(`Your plan allows uploads up to ${limit} minutes. Choose a shorter video or change your plan.`)
    : null;
  const cannotUpload = Boolean(eligibilityError || checking || (api.mode === "hosted" && (!hasPermission || !account.data || required === undefined)));
  const upload = useMutation({
    mutationFn: () => {
      if (cannotUpload) throw eligibilityError || new Error("Wait for your video and credits to be checked.");
      return api.upload(file!, setPhase);
    },
    onSuccess: async ({ id }) => {
      await queryClient.invalidateQueries({ queryKey: [api.mode, "runs"] });
      navigate(`/runs/${id}`);
    },
    onError: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [api.mode, "account"] }),
        queryClient.invalidateQueries({ queryKey: [api.mode, "runs"] }),
      ]);
    },
    onSettled: () => setPhase(""),
  });

  useDialogFocus(dialogRef, !upload.isPending, onClose);

  return (
    <div className={styles.dialogBackdrop} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !upload.isPending) onClose(); }}>
      <section ref={dialogRef} className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby="upload-title" tabIndex={-1}>
        <button className="icon-button" onClick={onClose} disabled={upload.isPending} aria-label="Close upload"><X /></button>
        <span className={styles.dialogIcon}><UploadSimple weight="duotone" /></span>
        <h2 id="upload-title">Start a new analysis</h2>
        <p>{api.mode === "hosted" ? "Choose a game recording. Trim halftime and other long breaks before uploading to save analysis minutes. We’ll reserve its video minutes before analysis starts. Footage and highlights expire 14 days after analysis finishes; your latest reel stays available." : "Choose a game recording. It will be processed and stored in your local project library."}</p>
        <label className={styles.fileDrop}>
          <input type="file" accept="video/*" onChange={(event) => { upload.reset(); setPhase(""); setFile(event.target.files?.[0] || null); setHasPermission(false); }} disabled={upload.isPending} />
          <UploadSimple />
          <span>{file ? <><strong>{file.name}</strong><small>{formatBytes(file.size)}</small></> : <><strong>Choose a game video</strong><small>MP4, MOV, MKV, M4V, AVI or WEBM</small></>}</span>
        </label>
        {api.mode === "hosted" && <><label className={infoStyles.permission}><input type="checkbox" checked={hasPermission} onChange={event => setHasPermission(event.target.checked)} disabled={upload.isPending} /><span>I own this recording or have permission to upload and process it, including any required permissions for people shown.</span></label><p className={infoStyles.notice}>Read the <Link to="/terms" target="_blank" rel="noopener noreferrer">upload terms (opens a new tab)</Link> and <Link to="/privacy" target="_blank" rel="noopener noreferrer">privacy and retention policy (opens a new tab)</Link>. Failed analyses and abandoned uploads expire 14 days after creation.</p></>}
        {checking && !upload.isPending && <p role="status">Checking video duration and available minutes…</p>}
        {!eligibilityError && required !== undefined && available !== undefined && <p>This video needs {required} minutes. You have {available} available.</p>}
        {phase && upload.isPending && <div className={styles.uploadPhase} role="status"><span className="loader loader--small" />{phase}…</div>}
        <ErrorNotice error={eligibilityError || upload.error} />
        {eligibilityError && account.data && <p><Link to="/account" onClick={onClose}>Manage your minutes and plan</Link></p>}
        {account.isError && <button className="button--quiet" onClick={() => account.refetch()}>Check credits again</button>}
        <div className={styles.dialogActions}><button className="button--ghost" onClick={onClose} disabled={upload.isPending}>Cancel</button><button onClick={() => upload.mutate()} disabled={!file || upload.isPending || cannotUpload}>{upload.isPending ? "Working…" : "Upload and analyze"}<ArrowRight /></button></div>
      </section>
    </div>
  );
}

function ConfirmDialog({ title, description, busy, error, onCancel, onConfirm }: { title: string; description: string; busy: boolean; error?: unknown; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useRef<HTMLElement>(null);
  useDialogFocus(dialogRef, !busy, onCancel);

  return (
    <div className={styles.dialogBackdrop} role="presentation">
      <section ref={dialogRef} className={`${styles.dialog} ${styles.confirmDialog}`} role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-description" tabIndex={-1}>
        <span className={`${styles.dialogIcon} ${styles.dangerIcon}`}><Trash /></span>
        <h2 id="confirm-title">{title}</h2><p id="confirm-description">{description}</p>
        <ErrorNotice error={error} />
        <div className={styles.dialogActions}><button className="button--ghost" onClick={onCancel} disabled={busy}>Keep run</button><button className="button--danger" onClick={onConfirm} disabled={busy}>{busy ? "Deleting…" : "Delete permanently"}</button></div>
      </section>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`${styles.statusBadge} ${styles[`status_${status}`] || ""}`}>{humanize(status)}</span>;
}

function RunLedgerRow({ run, api }: { run: RunSummary; api: ProductApi }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const rerun = useMutation({
    mutationFn: () => api.rerun(run.id),
    onSuccess: async ({ id }) => { await queryClient.invalidateQueries({ queryKey: [api.mode, "runs"] }); navigate(`/runs/${id}`); },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteRun!(run.id),
    onSuccess: async () => { setConfirming(false); await queryClient.invalidateQueries({ queryKey: [api.mode, "runs"] }); },
  });
  const isRunning = ["queued", "running", "cancel_requested"].includes(run.status);

  return (
    <>
      <article className={styles.runRow}>
        <button className={styles.runPrimary} onClick={() => navigate(`/runs/${run.id}`)}>
          <span className={styles.runPlay}><Play weight="fill" /></span>
          <span className={styles.runIdentity}><strong>{run.name}</strong><small>{formatDate(run.created_at)}</small></span>
        </button>
        <StatusBadge status={run.status} />
        <span className={styles.runStat}><strong>{run.event_count || 0}</strong><small>candidates</small></span>
        <span className={styles.runStat}><strong>{run.included_count || 0}</strong><small>included</small></span>
        {run.status === "running" && run.progress?.total ? <span className={styles.runProgress}><i style={{ width: `${Math.min(100, 100 * (run.progress.completed || 0) / run.progress.total)}%` }} /></span> : <span />}
        <div className={styles.rowActions}>
          <button className="icon-button" onClick={() => rerun.mutate()} disabled={rerun.isPending || isRunning || run.media_available === false || ["queued", "rendering"].includes(run.reel_job?.status || "")} aria-label={`Run ${run.name} again`} title="Run again"><Repeat /></button>
          {api.deleteRun && <button className="icon-button icon-button--danger" onClick={() => { remove.reset(); setConfirming(true); }} disabled={rerun.isPending || isRunning || ["queued", "rendering"].includes(run.reel_job?.status || "")} aria-label={`Delete ${run.name}`} title={rerun.isPending ? "Wait for Run again to finish copying the video" : isRunning ? "Cancel this run before deleting" : "Delete run"}><Trash /></button>}
          <button className="icon-button" onClick={() => navigate(`/runs/${run.id}`)} aria-label={`Open ${run.name}`}><ArrowRight /></button>
        </div>
      </article>
      {rerun.error && <ErrorNotice error={rerun.error} />}
      {rerun.isPending && <p role="status">Copying your saved video for a new analysis. Large files can take a few minutes.</p>}
      {confirming && <ConfirmDialog title="Delete this run?" description="The saved source video, clips, selections, and run history will be removed. This cannot be undone." busy={remove.isPending} error={remove.error} onCancel={() => setConfirming(false)} onConfirm={() => remove.mutate()} />}
    </>
  );
}

function LibraryPage({ api }: { api: ProductApi }) {
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [uploadOpen, setUploadOpen] = useState(searchParams.get("upload") === "1");
  const account = useQuery({ queryKey: [api.mode, "account"], queryFn: api.account!, enabled: Boolean(api.account) });
  const demo = useQuery({ queryKey: [api.mode, "demo-summary"], queryFn: api.demoSummary!, enabled: Boolean(api.demoSummary) });
  const runs = useQuery({ queryKey: [api.mode, "runs"], queryFn: api.listRuns, refetchOnWindowFocus: true, refetchInterval: (query) => query.state.data?.some(isProcessing) ? 5000 : false });
  const visible = useMemo(() => (runs.data || []).filter((run) => {
    const matchesStatus = status === "all" || run.status === status;
    const matchesSearch = !search.trim() || run.name.toLowerCase().includes(search.trim().toLowerCase());
    return matchesStatus && matchesSearch;
  }), [runs.data, search, status]);

  return (
    <main className={styles.libraryPage}>
      {searchParams.get("payment") === "success" && <div className="notice notice--success">Payment received. Your balance will update as soon as Stripe confirms it.</div>}
      {searchParams.get("payment") === "cancelled" && <div className="notice">Checkout was cancelled. Your balance has not changed.</div>}
      <div className={styles.pageHeading}>
        <div><span className={styles.headingIcon}><FilmStrip weight="duotone" /></span><h1>Your game library</h1><p>{api.mode === "hosted" ? "Saved reels, analysis results, and footage available for 14 days." : "Game analysis saved on this Mac."}</p></div>
        <button onClick={() => setUploadOpen(true)}><UploadSimple weight="bold" />New analysis</button>
      </div>
      {demo.data?.available && account.data && !account.data.has_subscription && <section className={styles.demoIntro} aria-labelledby="demo-title">
        <div><h2 id="demo-title">Explore a sample game</h2><p>Watch real clips, try selecting moments, and see the finished example reel. Your free minutes stay available for your own footage.</p></div>
        <Link className="button button--quiet" to="/demo"><Play weight="fill" />Open sample game</Link>
      </section>}
      <section className={styles.ledger}>
        <div className={styles.ledgerTools}>
          <label className={styles.searchField}><MagnifyingGlass /><span className="sr-only">Search saved games</span><input type="search" placeholder="Search saved games" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
          <label className={styles.filterField}><Funnel /><span className="sr-only">Filter by status</span><select value={status} onChange={(e) => setStatus(e.target.value)}><option value="all">All statuses</option><option value="complete">Complete</option><option value="running">Processing</option><option value="queued">Queued</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="uploaded">Uploaded</option></select></label>
          <span className={styles.resultCount}>{visible.length} of {runs.data?.length || 0} runs</span>
        </div>
        <ErrorNotice error={runs.error} />
        {runs.isLoading ? <div className={styles.emptyState}><span className="loader" /><h2>Loading your library</h2></div> : visible.length ? <div className={styles.runLedger}>{visible.map((run) => <RunLedgerRow key={run.id} run={run} api={api} />)}</div> : <div className={styles.emptyState}><span className={styles.emptyCourt}><Basketball weight="duotone" /></span><h2>{runs.data?.length ? "No runs match those filters" : "Your first game starts here"}</h2><p>{runs.data?.length ? "Clear the search or choose another status." : "Upload a full game to create a saved analysis run."}</p>{!runs.data?.length && <button onClick={() => setUploadOpen(true)}><UploadSimple />Choose a game video</button>}</div>}
      </section>
      {uploadOpen && <UploadDialog api={api} onClose={() => setUploadOpen(false)} />}
    </main>
  );
}

function AnalysisEstimate({ run }: { run: RunDetail }) {
  const stage = analysisStage(run.status, run.progress);
  const { phase, completed, total } = stage;
  const [observation, setObservation] = useState<{ id: string; state: EstimateState } | null>(null);
  const [time, setTime] = useState(() => performance.now() / 1000);
  useEffect(() => {
    if (run.status !== "running" || !["analyzing", "refining_candidates", "checking_game_context", "creating_clips", "uploading_clips"].includes(phase) || !total) {
      setObservation(null);
      return;
    }
    const now = performance.now() / 1000;
    setTime(now);
    setObservation(previous => ({ id: run.id, state: observeProgress(previous?.id === run.id ? previous.state : null,
      phase, completed, total, now) }));
  }, [run.id, run.status, phase, completed, total]);
  useEffect(() => {
    if (run.status !== "running") return;
    const timer = window.setInterval(() => setTime(performance.now() / 1000), 5000);
    return () => window.clearInterval(timer);
  }, [run.status]);
  if (run.status !== "running") return null;
  if (!stage.hasCount || completed >= total) return null;
  const state = observation?.id === run.id && observation.state.phase === phase && observation.state.total === total ? observation.state : null;
  const remaining = estimateRemaining(state, time);
  return <div className={styles.analysisEstimate}>
    <p><Clock aria-hidden="true" />{remaining === null ? "Estimating time remaining…" : `${formatEstimate(remaining)} left in this pass.`}</p>
    {remaining !== null && <small>{stage.next} Timing may change as processing progresses.</small>}
  </div>;
}

function ProgressView({ run, api, onRefresh }: { run: RunDetail; api: ProductApi; onRefresh: () => void }) {
  const navigate = useNavigate();
  const cancel = useMutation({ mutationFn: () => api.cancel!(run.id), onSuccess: onRefresh });
  const rerun = useMutation({ mutationFn: () => api.rerun(run.id), onSuccess: ({ id }) => navigate(`/runs/${id}`) });
  const stage = analysisStage(run.status, run.progress);
  const { completed, total } = stage;
  const percent = total ? Math.min(100, 100 * completed / total) : 0;
  const failed = ["failed", "cancelled"].includes(run.status);
  const cloudCost = cloudComputeCost(run);

  return (
    <main className={styles.progressPage}>
      <Link className={styles.backLink} to="/library"><ArrowLeft />Back to library</Link>
      <section className={styles.progressStage}>
        <div className={styles.progressVisual} aria-hidden="true"><span className={styles.scanLine} /><Basketball weight="duotone" /></div>
        <div className={styles.progressContent}>
          <StatusBadge status={run.status} />
          <h1>{failed ? run.status === "cancelled" ? "Analysis cancelled" : "Analysis stopped" : run.status === "cancel_requested" ? "Stopping analysis" : stage.title}</h1>
          <p className={styles.progressFilename}>{run.name}</p>
          {!failed && <p className={styles.passDescription} role="status">{run.status === "cancel_requested" ? "Stopping work and saving the results available so far." : stage.description}</p>}
          {!failed && <ol className={styles.analysisPasses} aria-label="Analysis passes">{analysisPasses.map((label, index) => {
            const done = index < stage.step || (index === 0 && !!run.progress?.total && (run.progress.completed || 0) >= run.progress.total);
            const active = index === stage.step;
            return <li key={label} aria-current={active ? "step" : undefined} data-state={done ? "done" : active ? "active" : "pending"}>
              <span className={styles.passMarker} aria-hidden="true">{done ? <Check weight="bold" /> : index + 1}</span>
              <span>{label}</span><small>{done ? "Done" : active ? "Now" : "Next"}</small>
            </li>;
          })}</ol>}
          {!failed && <>{stage.hasCount ? <div className={styles.largeProgress} role="progressbar" aria-label={stage.title} aria-valuemin={0} aria-valuemax={total} aria-valuenow={completed} aria-valuetext={`${completed.toLocaleString()} of ${total.toLocaleString()} ${stage.unit}`}><i style={{ width: `${percent}%` }} /></div> : <p className={styles.passWorking}><span className="loader loader--small" aria-hidden="true" />{run.status === "queued" ? "Waiting for processing to begin" : "In progress"}</p>}<div className={styles.progressNumbers}>{stage.hasCount && <span><strong>{completed.toLocaleString()}</strong> of {total.toLocaleString()} {stage.unit}</span>}{run.is_admin && ["analyzing", "refining_candidates", "checking_game_context"].includes(stage.phase) && run.progress?.active_requests ? <span>{run.progress.active_requests} requests active</span> : null}{run.is_admin && (run.progress?.spend_usd != null || run.spend_usd != null) ? <span>${(run.progress?.spend_usd ?? run.spend_usd ?? 0).toFixed(4)} model spend</span> : null}</div></>}
          {!failed && stage.phase === "creating_clips" && run.progress?.uploads_completed != null && <p className={styles.previewUploads}>{run.progress.uploads_completed.toLocaleString()} {run.progress.uploads_completed === 1 ? "preview" : "previews"} uploaded.</p>}
          {run.is_admin && api.mode === "hosted" && <div className={styles.cloudCost}>
            <p>Cloud compute (est.): <strong>{cloudCost === null ? "Unavailable" : `$${cloudCost.toFixed(4)}`}</strong></p>
            <small>Analysis worker only. Excludes storage, network, database and reel exports.</small>
          </div>}
          <AnalysisEstimate run={run} />
          {run.error && <div className="notice notice--error" role="alert">{run.error}</div>}
          <div className={styles.progressActions}>
            {api.cancel && ["queued", "running"].includes(run.status) && <button className="button--quiet" onClick={() => cancel.mutate()} disabled={cancel.isPending}>{cancel.isPending ? "Requesting…" : "Cancel analysis"}</button>}
            {failed && <button onClick={() => rerun.mutate()} disabled={rerun.isPending}><Repeat />{rerun.isPending ? "Starting…" : "Run again"}</button>}
            <Link className="button button--ghost" to="/library">View all runs</Link>
          </div>
          <ErrorNotice error={cancel.error || rerun.error} />
        </div>
      </section>
    </main>
  );
}

type ClipFilter = "all" | "included" | "excluded" | "review";

function ClipVideo({ src, onError, onLoadedData }: { src: string; onError: () => void; onLoadedData: () => void }) {
  // Progress responses rotate signed URLs for the same asset. Keep the active
  // source so polling does not interrupt playback; use the newest link if it fails.
  const [playbackSrc, setPlaybackSrc] = useState(src);
  return <video controls preload="metadata" src={playbackSrc} onLoadedData={onLoadedData} onError={() => {
    if (src && src !== playbackSrc) setPlaybackSrc(src);
    else onError();
  }} />;
}

function ClipItem({ clip, runId, api, onToggle, changing, expired = false }: { clip: ClipEvent; runId: string; api: ProductApi; onToggle: (clip: ClipEvent, included: boolean) => void; changing: boolean; expired?: boolean }) {
  const [mediaError, setMediaError] = useState(false);
  const clipSrc = api.clipUrl(runId, clip);
  return (
    <article className={`${styles.clipItem} ${clip.included ? styles.clipIncluded : ""}`}>
      <div className={styles.videoWrap}>
        {!expired && <ClipVideo key={clipSrc.split("?")[0]} src={clipSrc} onError={() => setMediaError(true)} onLoadedData={() => setMediaError(false)} />}
        {expired && <div className={styles.mediaError}><Clock /><strong>Highlight expired</strong><span>The event data is still available.</span></div>}
        {mediaError && !expired && <div className={styles.mediaError} role="status"><FilmStrip /><strong>Clip unavailable</strong><span>Reload the page or return to this moment later.</span></div>}
        <span className={styles.timecode}>{formatTimestamp(clip.timestamp_seconds)}</span>
      </div>
      <div className={styles.clipBody}>
        <div className={styles.clipMeta}><div><strong>{humanize(clip.shot_type || "basketball moment")}</strong><span>{clip.model_confidence == null ? "Confidence unavailable" : `${Math.round(clip.model_confidence * 100)}% confidence`}</span></div><StatusBadge status={clip.classification} /></div>
        {clip.needs_review && <p className={styles.reviewHint}>{clip.game_context === "uncertain"
          ? "Review suggested: couldn’t confirm whether this basket occurred during the game."
          : "Review suggested: the model marked this moment as uncertain."}</p>}
        <label className={styles.includeControl}><input type="checkbox" checked={clip.included} disabled={changing} onChange={(e) => onToggle(clip, e.target.checked)} /><span><Check weight="bold" /></span>Include in reel</label>
      </div>
    </article>
  );
}

function LatestReel({ run, api, rebuilding }: { run: RunDetail; api: ProductApi; rebuilding: boolean }) {
  const [playbackError, setPlaybackError] = useState(false);
  const preview = useQuery({
    // A queued job still refers to the previous saved reel. Replace its preview
    // only when a new successful export changes generated_at.
    queryKey: [api.mode, "reel-preview", run.id, run.latest_reel?.generated_at, run.example_reel_url],
    queryFn: () => api.reelPreview!(run.id),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  useEffect(() => { setPlaybackError(false); }, [preview.data]);
  return <section className={styles.latestReel} aria-labelledby="latest-reel-heading">
    <h2 id="latest-reel-heading">{run.example_reel_url ? "Example highlight reel" : "Your latest reel"}</h2>
    <p>{run.example_reel_url ? "This saved example shows the finished result. Trying selections below does not change this reel." : rebuilding ? "Your previous reel is available while the new one renders." : "Watch your saved reel here, then download it when you’re ready to share."}</p>
    {preview.isPending && <p role="status">Loading your reel…</p>}
    {preview.data?.url && <video key={preview.data.url} className={styles.reelPlayer} src={preview.data.url} controls playsInline preload="metadata" aria-label={run.example_reel_url ? "Example highlight reel" : "Latest highlight reel"} onError={() => setPlaybackError(true)} />}
    <ErrorNotice error={preview.error} />
    {(preview.isError || playbackError) && <div role="alert"><p>{playbackError ? "The video could not load. Refresh its playback link and try again." : "Could not load the reel preview."}</p><button className="button--quiet" onClick={() => { setPlaybackError(false); void preview.refetch(); }} disabled={preview.isFetching}>Reload preview</button></div>}
  </section>;
}

function ReviewView({ run: sourceRun, api, demo = false }: { run: RunDetail; api: ProductApi; demo?: boolean }) {
  const [demoSelections, setDemoSelections] = useState<Record<string, boolean>>({});
  const run = demo ? { ...sourceRun, events: sourceRun.events.map(clip => ({ ...clip, included: demoSelections[clip.event_id] ?? clip.included })) } : sourceRun;
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ClipFilter>("all");
  const [format, setFormat] = useState<"vertical" | "landscape">("vertical");
  const [combineOverlaps, setCombineOverlaps] = useState(sourceRun.reel_job?.combine_overlaps ?? sourceRun.latest_reel?.combine_overlaps ?? true);
  const [excludeFreeThrows, setExcludeFreeThrows] = useState(demo ? false : defaultFreeThrowFilter(sourceRun));
  const [downloadError, setDownloadError] = useState<Error | null>(null);
  const expired = run.media_available === false || Boolean(run.media_expires_at && Date.parse(run.media_expires_at) <= Date.now());
  const reelBusy = ["queued", "rendering"].includes(run.reel_job?.status || "");
  const refresh = async () => { await queryClient.invalidateQueries({ queryKey: [api.mode, "run", run.id] }); };
  const download = useMutation({ mutationFn: async () => {
    setDownloadError(null);
    // Open immediately on the user gesture, then navigate to a fresh signed URL.
    const windowRef = window.open("about:blank", "_blank");
    if (windowRef) windowRef.opener = null;
    try {
      const result = await api.reelDownload!(run.id);
      if (!result.url) throw new Error("No saved reel is available.");
      if (windowRef) windowRef.location.href = result.url;
      else window.location.assign(result.url);
    } catch (error) { windowRef?.close(); setDownloadError(error instanceof Error ? error : new Error("Could not download reel.")); }
  }});
  const sponsorUpload = useMutation({ mutationFn: (file: File) => {
    if (file.size > 5 * 1024 * 1024) throw new Error("Choose a logo up to 5 MB.");
    return api.uploadSponsor!(run.id, file);
  }, onSuccess: (updated) => { queryClient.setQueryData([api.mode, "run", run.id], updated); } });
  const sponsorRemove = useMutation({ mutationFn: (id: string) => api.removeSponsor!(run.id, id),
    onSuccess: (updated) => { queryClient.setQueryData([api.mode, "run", run.id], updated); } });
  const selection = useMutation({
    mutationFn: ({ clip, included }: { clip: ClipEvent; included: boolean }) => api.setIncluded(run.id, clip.event_id, included),
    onSuccess: (updated) => {
      queryClient.setQueryData([api.mode, "run", run.id], updated);
      queryClient.invalidateQueries({ queryKey: [api.mode, "runs"] });
    },
  });
  const buildReel = useMutation({ mutationFn: () => api.buildReel(run.id, { format, combine_overlaps: combineOverlaps, exclude_free_throws: excludeFreeThrows, sponsor_ids: run.entitlements?.sponsors === false ? [] : (run.sponsors || []).map((sponsor) => sponsor.id) }), onSuccess: refresh });
  const reviewEvents = run.events.filter(clip => !excludeFreeThrows || clip.shot_type !== "free_throw");
  const visible = reviewEvents.filter((clip) => filter === "all" || (filter === "included" && clip.included) || (filter === "excluded" && !clip.included) || (filter === "review" && clip.needs_review));
  const included = run.events.filter(clip => includedInReel(clip, excludeFreeThrows)).length;
  const filteredFreeThrows = run.events.filter(clip => clip.included && clip.shot_type === "free_throw").length;
  const cloudCost = cloudComputeCost(run);

  return (
    <main className={styles.reviewPage}>
      <Link className={styles.backLink} to="/library"><ArrowLeft />Back to library</Link>
      <div className={styles.reviewGrid}>
        <aside className={styles.reviewSidebar}>
          <StatusBadge status={run.status} />
          <h1>{run.name}</h1>{demo ? <p>This sample was created from the first quarter of a basketball game. Try the review workflow without using your analysis minutes.</p> : <p>{formatDate(run.created_at)}</p>}
          <dl className={styles.reviewStats}><div><dt>Selected</dt><dd>{included}</dd></div><div><dt>Candidates</dt><dd>{run.events.length}</dd></div>{run.is_admin && run.spend_usd != null && <div><dt>Model spend</dt><dd>${run.spend_usd.toFixed(3)}</dd></div>}{run.is_admin && api.mode === "hosted" && <div><dt>Cloud compute (est.)</dt><dd>{cloudCost === null ? "Unavailable" : `$${cloudCost.toFixed(4)}`}</dd></div>}</dl>
          {run.is_admin && api.mode === "hosted" && <p className={styles.costNote}>Analysis worker only. Excludes storage, network, database and reel exports.</p>}
          {!demo && (api.mode === "hosted" || run.media_expires_at) && <p className={styles.retentionNote}>{expired ? "Game footage and highlights have expired. Your saved reel remains available." : run.media_expires_at ? `Footage and highlights available until ${formatDate(run.media_expires_at)}. Your latest reel stays available.` : "Footage and highlights will be available for 14 days after analysis finishes. Your latest reel stays available."}</p>}
          {!demo && !expired && <div className={styles.reelSettings}>
            <label htmlFor="reel-format">Reel format</label>
            <select id="reel-format" value={format} disabled={reelBusy || buildReel.isPending} onChange={(event) => setFormat(event.target.value as "vertical" | "landscape")}>
              <option value="vertical">Vertical · Instagram / TikTok</option><option value="landscape">Landscape · original dimensions</option>
            </select>
            <p>{format === "vertical" ? "Full footage on a vertical canvas with a blurred background." : "Keep the original aspect ratio and dimensions."} Short fades and game audio are included.</p>
            <label htmlFor="reel-overlaps">Overlapping clips</label>
            <select id="reel-overlaps" aria-describedby="reel-overlaps-help" value={combineOverlaps ? "combine" : "separate"} disabled={reelBusy || buildReel.isPending} onChange={(event) => setCombineOverlaps(event.target.value === "combine")}>
              <option value="combine">Combine overlaps</option><option value="separate">Keep clips separate</option>
            </select>
            <p id="reel-overlaps-help">{combineOverlaps ? "Nearby selected moments play continuously. Shared footage plays once; every selected basket stays included." : "Each selected clip plays in full. Shared footage may repeat between nearby moments."}</p>
            <label className={styles.shotTypeFilter}>
              <input type="checkbox" checked={excludeFreeThrows} disabled={reelBusy || buildReel.isPending || expired} aria-describedby="free-throws-help" onChange={(event) => setExcludeFreeThrows(event.target.checked)} />
              Exclude free throws
            </label>
            <p id="free-throws-help">Hide clips labelled as free throws and leave them out of the reel. Turn this off to restore them.</p>
            {excludeFreeThrows && <p role="status">{filteredFreeThrows} {filteredFreeThrows === 1 ? "free throw" : "free throws"} left out of this reel.{!included && filteredFreeThrows > 0 ? " Turn off the filter or select another clip to build a reel." : ""}</p>}
            {api.uploadSponsor && run.entitlements?.sponsors !== false && <div className={styles.sponsorSettings}>
              <h2>Sponsors <span>(optional)</span></h2>
              <p>Add up to two logos for a three-second end card.</p>
              {(run.sponsors || []).map((sponsor) => <div className={styles.sponsorRow} key={sponsor.id}>
                {sponsor.url && <img src={sponsor.url} alt={`${sponsor.name} logo`} />}<span>{sponsor.name}</span>
                <button type="button" className="icon-button" aria-label={`Remove ${sponsor.name}`} disabled={reelBusy || sponsorRemove.isPending || buildReel.isPending} onClick={() => sponsorRemove.mutate(sponsor.id)}><X /></button>
              </div>)}
              <label className={styles.sponsorUpload} htmlFor="sponsor-logo">{sponsorUpload.isPending ? "Uploading logo…" : "Upload sponsor logo"}</label>
              <input id="sponsor-logo" type="file" accept="image/png,image/jpeg,image/webp" disabled={reelBusy || buildReel.isPending || sponsorUpload.isPending || sponsorRemove.isPending || (run.sponsors || []).length >= 2} onChange={(event) => { const file = event.target.files?.[0]; if (file) sponsorUpload.mutate(file); event.target.value = ""; }} />
              <small>PNG, JPEG, or WebP · up to 5 MB each</small>
            </div>}
          </div>}
          {run.entitlements?.sponsors === false && <p className={styles.retentionNote}><Link to="/#pricing">Business</Link> includes sponsor end cards.</p>}
          {run.entitlements?.renders != null && <p className={styles.retentionNote}>{run.reels_used || 0} of {run.entitlements.renders} successful renders used for this game. Repeating the latest unchanged export is free. <Link to="/#pricing">Compare plans</Link></p>}
          {demo && <p className={styles.retentionNote}>Upload your own recording to build and download a reel from your selections.</p>}
          <div className={styles.reviewActions}>
            {demo && <Link className="button" to="/library?upload=1"><UploadSimple />Try your own footage</Link>}
            {!demo && !expired && <button onClick={() => buildReel.mutate()} disabled={reelBusy || buildReel.isPending || sponsorUpload.isPending || sponsorRemove.isPending || !included}><FilmStrip />{reelBusy ? "Reel processing…" : buildReel.isPending ? "Starting reel…" : "Build highlight reel"}</button>}
            {run.latest_reel && api.reelDownload && <button className="button--quiet" onClick={() => download.mutate()} disabled={download.isPending}><DownloadSimple />{download.isPending ? "Preparing download…" : "Download latest reel"}</button>}
          </div>
          <div aria-live="polite">
            {reelBusy && <p className="notice">{run.reel_job?.status === "queued" ? "Your reel is queued." : "Rendering your selected moments from the original footage."} You can leave this page and return later.</p>}
            {!demo && run.latest_reel && <p className={`notice notice--success ${styles.savedReelNotice}`}>Saved {run.latest_reel.format} reel with {run.latest_reel.count} {run.latest_reel.count === 1 ? "clip" : "clips"}{run.latest_reel.combine_overlaps !== undefined ? run.latest_reel.combine_overlaps ? " · overlaps combined" : " · clips kept separate" : ""}{run.latest_reel.generated_at ? ` · ${formatDate(run.latest_reel.generated_at)}` : ""}.</p>}
            {run.reel_job?.error && <p className="notice notice--error">{run.reel_job.error}</p>}
          </div>
          <ErrorNotice error={buildReel.error || selection.error || sponsorUpload.error || sponsorRemove.error || downloadError} />
          <p className={styles.savedNote}><Check />{demo ? "Practice selections reset when you leave the sample." : "Selections save immediately."}</p>
        </aside>
        <section className={styles.clipWorkspace}>
          {run.latest_reel && api.reelPreview && <LatestReel run={run} api={api} rebuilding={reelBusy} />}
          <div className={styles.clipToolbar}><div><h2>{expired ? "Game analysis" : "Review candidates"}</h2><p>{expired ? "Event details and saved selections remain available after footage expires." : "Watch each moment and decide what belongs in the final cut."}</p></div><div className={styles.filterTabs} role="group" aria-label="Filter clips">{(["all", "included", "excluded", "review"] as ClipFilter[]).map((value) => <button key={value} className={filter === value ? styles.activeFilter : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === "review" ? "Needs review" : humanize(value)}</button>)}</div></div>
          <p className={styles.clipCount}>Showing {visible.length} of {run.events.length} {expired ? "events" : "clips"}</p>
          {visible.length ? <div className={styles.clipList}>{visible.map((clip) => <ClipItem key={clip.event_id} clip={clip} runId={run.id} api={api} expired={expired} changing={selection.isPending && selection.variables?.clip.event_id === clip.event_id} onToggle={(item, next) => demo ? setDemoSelections(previous => ({ ...previous, [item.event_id]: next })) : selection.mutate({ clip: item, included: next })} />)}</div> : <div className={styles.emptyClips}><FilmStrip /><h3>{run.events.length ? "No clips match this filter" : "No candidate clips were found"}</h3><p>{run.events.length ? (excludeFreeThrows ? "Turn off the free-throw filter or choose another view." : "Choose another view to continue reviewing.") : "The run completed without producing reviewable moments."}</p></div>}
        </section>
      </div>
    </main>
  );
}

function isProcessing(run: RunSummary) {
  return ["queued", "running", "cancel_requested", "uploaded"].includes(run.status) || ["queued", "rendering"].includes(run.reel_job?.status || "");
}

function RunPage({ api }: { api: ProductApi }) {
  const { runId = "" } = useParams();
  const started = useRef(false);
  const run = useQuery({
    queryKey: [api.mode, "run", runId],
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => query.state.data && isProcessing(query.state.data) ? 5000 : false,
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    if (run.data?.status === "uploaded" && run.data.media_available !== false && api.start && !started.current) {
      started.current = true;
      api.start(runId).then(() => run.refetch()).catch(() => run.refetch());
    }
  }, [api, run, run.data?.status, runId]);

  if (run.isLoading) return <main className={styles.loadingPage}><span className="loader" /><p>Loading game film…</p></main>;
  if (!run.data) return <main className={styles.loadingPage}><ErrorNotice error={run.error || new Error("Run not found.")} /><Link className="button button--ghost" to="/library">Back to library</Link></main>;
  const showReview = run.data.status === "complete" || run.data.events.length > 0;
  return showReview ? <ReviewView run={run.data} api={api} /> : <ProgressView run={run.data} api={api} onRefresh={() => run.refetch()} />;
}

function DemoPage({ api }: { api: ProductApi }) {
  const sample = useQuery({ queryKey: [api.mode, "demo-game"], queryFn: api.demoGame!, enabled: Boolean(api.demoGame), staleTime: 0, refetchOnWindowFocus: true });
  if (sample.isPending) return <main className={styles.loadingPage}><span className="loader" /><p>Loading sample game…</p></main>;
  if (!sample.data) return <main className={styles.loadingPage}><ErrorNotice error={sample.error || new Error("The sample game is not available yet.")} /><button onClick={() => void sample.refetch()}>Try again</button><Link to="/library">Back to library</Link></main>;
  const sampleApi: ProductApi = { ...api, reelDownload: undefined, uploadSponsor: undefined, removeSponsor: undefined, reelPreview: async () => { const fresh = await api.demoGame!(); return { count: fresh.latest_reel?.count || 0, url: fresh.example_reel_url || null }; } };
  return <ReviewView key={sample.data.id} run={sample.data} api={sampleApi} demo />;
}

function PricingRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/${search}#pricing`} replace />;
}

export function ProductApp({ api, configured, user, authReady, authActions }: ProductAppProps) {
  const protectedPage = (node: ReactNode) => <RequireAccess mode={api.mode} user={user} authReady={authReady}><AppFrame api={api} authActions={authActions}>{node}</AppFrame></RequireAccess>;
  return (
    <><Seo /><Routes>
      <Route path="/" element={api.mode === "hosted" ? <Landing api={api} signedIn={Boolean(user)} /> : <Navigate to="/library" replace />} />
      <Route path="/sign-in" element={api.mode === "hosted" ? <AuthPage configured={configured} actions={authActions} signedIn={Boolean(user)} /> : <Navigate to="/library" replace />} />
      <Route path="/privacy" element={<CustomerInfo page="privacy" />} />
      <Route path="/terms" element={<CustomerInfo page="terms" />} />
      <Route path="/refunds" element={<CustomerInfo page="refunds" />} />
      <Route path="/support" element={<CustomerInfo page="support" />} />
      <Route path="/pricing" element={api.mode === "hosted" ? <PricingRedirect /> : <Navigate to="/library" replace />} />
      <Route path="/account" element={api.mode === "hosted" ? protectedPage(<AccountPage api={api} />) : <Navigate to="/library" replace />} />
      <Route path="/library" element={protectedPage(<LibraryPage api={api} />)} />
      <Route path="/demo" element={api.mode === "hosted" ? protectedPage(<DemoPage api={api} />) : <Navigate to="/library" replace />} />
      <Route path="/runs/:runId" element={protectedPage(<RunPage api={api} />)} />
      <Route path="*" element={<main className={styles.loadingPage}><h1>Page not found</h1><p>This page may have moved. Return to HoopCuts to find your next step.</p><Link to="/">Back to HoopCuts</Link></main>} />
    </Routes></>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDate(value?: string | null): string {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 ** 2) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 ** 2).toFixed(bytes > 1024 ** 3 ? 0 : 1)} MB`;
}
