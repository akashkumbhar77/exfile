// App shell. PATCH-003 A.1 build order: Approvals first; later pages are added as they're built.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { ApprovalsPage } from "./pages/approvals/ApprovalsPage";
import { SessionGate, useSession } from "./session";
import styles from "./App.module.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

function Shell() {
  const { actor, signOut } = useSession();
  return (
    <>
      <nav className={styles.nav}>
        <span className={styles.brand}>Sheets Automation</span>
        <NavLink to="/approvals" className={({ isActive }) => (isActive ? `${styles.link} ${styles.on}` : styles.link)}>
          Approvals
        </NavLink>
        <span className={styles.spacer} />
        <span className={styles.who}>{actor}</span>
        <button type="button" className={styles.signOut} onClick={signOut}>
          Sign out
        </button>
      </nav>
      <Routes>
        <Route path="/approvals" element={<ApprovalsPage />} />
        <Route path="/approvals/:configId" element={<ApprovalsPage />} />
        <Route path="*" element={<Navigate to="/approvals" replace />} />
      </Routes>
    </>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionGate>
          <Shell />
        </SessionGate>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
