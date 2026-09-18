// Pilot auth (PATCH-003 C): static bearer token + a display name for approvals, entered once and
// held in React state for this tab only. Reloading the page asks again -- by design.
import { createContext, useContext, useState, type ReactNode } from "react";
import { setToken } from "./api/client";
import styles from "./session.module.css";

interface Session {
  actor: string;
  signOut: () => void;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const s = useContext(SessionContext);
  if (!s) throw new Error("useSession outside SessionGate");
  return s;
}

export function SessionProvider({ actor, signOut, children }: Session & { children: ReactNode }) {
  return <SessionContext.Provider value={{ actor, signOut }}>{children}</SessionContext.Provider>;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ actor: string } | null>(null);
  const [token, setTokenInput] = useState("");
  const [actor, setActor] = useState("");

  if (session) {
    return (
      <SessionProvider
        actor={session.actor}
        signOut={() => {
          setToken(null);
          setSession(null);
        }}
      >
        {children}
      </SessionProvider>
    );
  }

  return (
    <main className={styles.gate}>
      <form
        className={styles.card}
        onSubmit={(e) => {
          e.preventDefault();
          setToken(token);
          setSession({ actor: actor.trim() });
        }}
      >
        <h1 className={styles.title}>Sheets Automation</h1>
        <label className={styles.field}>
          API token
          <input type="password" autoComplete="off" value={token} onChange={(e) => setTokenInput(e.target.value)} required />
        </label>
        <label className={styles.field}>
          Your name (recorded on approvals)
          <input value={actor} onChange={(e) => setActor(e.target.value)} required maxLength={200} />
        </label>
        <p className={styles.note}>Kept in this tab's memory only; reloading the page asks again.</p>
        <button type="submit" className={styles.primary} disabled={!token.trim() || !actor.trim()}>
          Continue
        </button>
      </form>
    </main>
  );
}
