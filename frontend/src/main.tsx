import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App.tsx";
import { AuthProvider } from "./auth/AuthContext";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Reconciliation data changes only when someone uploads or re-runs,
      // so refetching on every window focus is noise.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      // A 401 is handled by the axios interceptor, which refreshes the token
      // and retries once. Retrying here as well would multiply that work.
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
