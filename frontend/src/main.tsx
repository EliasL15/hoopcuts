import "@fontsource/barlow-condensed/latin-600.css";
import "@fontsource/barlow-condensed/latin-700.css";
import "@fontsource/source-sans-3/latin-400.css";
import "@fontsource/source-sans-3/latin-600.css";
import "@fontsource/source-sans-3/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "./styles/tokens.css";
import "./styles/global.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getApp, getApps, initializeApp } from "@firebase/app";
import {
  createUserWithEmailAndPassword,
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  onAuthStateChanged,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut,
  type Auth,
  type User,
} from "@firebase/auth";
import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { createHostedApi, createLocalApi, loadRuntimeConfig } from "./api";
import { ProductApp, type AuthActions } from "./App";
import type { RuntimeConfig } from "./types";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
  },
});

function friendlyAuthError(error: unknown): string {
  const code = typeof error === "object" && error && "code" in error ? String(error.code) : "";
  const messages: Record<string, string> = {
    "auth/popup-closed-by-user": "Google sign-in was cancelled. Try again when you’re ready.",
    "auth/popup-blocked": "Allow popups for this site, then try Google sign-in again.",
    "auth/operation-not-allowed": "Google sign-in is not enabled yet. Use email and password.",
    "auth/unauthorized-domain": "Google sign-in is not configured for this website. Use email and password.",
    "auth/account-exists-with-different-credential": "Sign in using your existing account’s method for this email.",
    "auth/invalid-credential": "The email or password is incorrect.",
    "auth/invalid-email": "Enter a valid email address.",
    "auth/email-already-in-use": "An account already exists for this email.",
    "auth/weak-password": "Use a password with at least six characters.",
    "auth/too-many-requests": "Too many attempts. Wait a moment and try again.",
    "auth/network-request-failed": "The network request failed. Check your connection and try again.",
  };
  return messages[code] || (error instanceof Error ? error.message : "Authentication failed.");
}

function HostedBootstrap({ config }: { config: Extract<RuntimeConfig, { mode: "hosted" }> }) {
  const app = useMemo(() => (getApps().length ? getApp() : initializeApp(config.firebase)), [config.firebase]);
  const auth = useMemo(() => getAuth(app), [app]);
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);

  useEffect(
    () =>
      onAuthStateChanged(auth, async (nextUser) => {
        if (!nextUser) {
          setUser(null);
          setAuthReady(true);
          return;
        }
        try {
          await nextUser.reload();
          setUser(nextUser.emailVerified ? nextUser : null);
        } catch {
          setUser(null);
        } finally {
          setAuthReady(true);
        }
      }),
    [auth],
  );

  const actions = useMemo<AuthActions>(
    () => ({
      signInWithGoogle: async () => {
        try {
          const provider = new GoogleAuthProvider();
          provider.setCustomParameters({ prompt: "select_account" });
          const result = await signInWithPopup(auth, provider);
          await result.user.reload();
          if (!result.user.emailVerified) {
            await signOut(auth);
            throw new Error("Verify your Google account email before signing in.");
          }
          setUser(result.user);
        } catch (error) { throw new Error(friendlyAuthError(error)); }
      },
      signIn: async (email, password) => {
        try {
          const result = await signInWithEmailAndPassword(auth, email, password);
          await result.user.reload();
          if (!result.user.emailVerified) {
            await signOut(auth);
            throw new Error("Verify your email address before signing in.");
          }
          setUser(result.user);
        } catch (error) {
          throw new Error(friendlyAuthError(error));
        }
      },
      createAccount: async (email, password) => {
        try {
          const result = await createUserWithEmailAndPassword(auth, email, password);
          await sendEmailVerification(result.user, { url: `${window.location.origin}/sign-in` });
          await signOut(auth);
        } catch (error) {
          throw new Error(friendlyAuthError(error));
        }
      },
      resetPassword: async (email) => {
        if (!email.trim()) throw new Error("Enter your email address first.");
        try {
          await sendPasswordResetEmail(auth, email, { url: `${window.location.origin}/sign-in` });
        } catch (error) {
          throw new Error(friendlyAuthError(error));
        }
      },
      signOut: async () => {
        await signOut(auth);
        setUser(null);
        queryClient.clear();
      },
    }),
    [auth],
  );

  const api = useMemo(() => createHostedApi(auth as Auth), [auth]);
  return (
    <ProductApp
      api={api}
      configured={config.configured}
      user={user}
      authReady={authReady}
      authActions={actions}
    />
  );
}

function Bootstrap() {
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadRuntimeConfig().then(setConfig).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "Could not start the application.");
    });
  }, []);

  if (error) return <div className="boot-message"><strong>HoopCuts</strong><p>{error}</p></div>;
  if (!config) return <div className="boot-message"><span className="loader" aria-hidden="true" /><p>Opening the film room…</p></div>;
  if (config.mode === "hosted") return <HostedBootstrap config={config} />;
  return <ProductApp api={createLocalApi()} configured user={null} authReady />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Bootstrap />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
