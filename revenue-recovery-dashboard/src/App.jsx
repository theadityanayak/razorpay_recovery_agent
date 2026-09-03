import { useCallback, useState } from "react";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import StatsHeader from "./components/StatsHeader";
import RootCauseChart from "./components/RootCauseChart";
import OutcomeDonut from "./components/OutcomeDonut";
import ActivityFeed from "./components/ActivityFeed";
import ReviewQueue from "./components/ReviewQueue";
import { api } from "./api/client";
import { useApi } from "./hooks/useApi";
import { resetIndex } from "./api/decisionIndex";

export default function App() {
  const [view, setView] = useState("overview");
  const [menuOpen, setMenuOpen] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [refreshedAt, setRefreshedAt] = useState(() => new Date().toISOString());

  const {
    data: stats,
    loading: statsLoading,
    error: statsError,
    reload: reloadStats,
  } = useApi((opts) => api.stats(opts), [refreshToken]);

  // One refresh control for the whole page. It also drops the decision index,
  // otherwise expanded rows would keep serving decisions cached before the
  // refresh while every other panel showed fresh numbers.
  const refreshAll = useCallback(() => {
    resetIndex();
    reloadStats();
    setRefreshToken((t) => t + 1);
    setRefreshedAt(new Date().toISOString());
  }, [reloadStats]);

  const navigate = useCallback((next) => {
    setView(next);
    setMenuOpen(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  return (
    <div className="min-h-screen bg-canvas">
      <Sidebar
        view={view}
        onNavigate={navigate}
        reviewCount={stats?.requires_human_review ?? 0}
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
      />

      {/*
        The rail is 248px; the content column is inset a further 8px so the
        cards' shadows have somewhere to fall. Gaps step up from the old 20px
        to 28px — on a screen this dense, spacing is the whole redesign.
      */}
      <div className="lg:pl-[248px]">
        <TopBar
          view={view}
          onOpenMenu={() => setMenuOpen(true)}
          onRefresh={refreshAll}
          refreshedAt={refreshedAt}
        />

        <main className="mx-auto max-w-[1480px] space-y-7 px-5 pb-10 pt-4 sm:px-8">
          {view === "overview" ? (
            <>
              <StatsHeader
                stats={stats}
                loading={statsLoading}
                error={statsError}
                onRetry={reloadStats}
              />

              <div className="grid grid-cols-1 gap-7 xl:grid-cols-[1.6fr_1fr]">
                <RootCauseChart refreshToken={refreshToken} />
                <OutcomeDonut
                  stats={stats}
                  loading={statsLoading}
                  error={statsError}
                  onRetry={reloadStats}
                  onShowReview={() => navigate("review")}
                />
              </div>

              <ActivityFeed
                refreshToken={refreshToken}
                title="Live feed"
                subtitle="Open any row to see the full chain: root cause, guardrail check, chosen strategy and the message that was drafted."
              />
            </>
          ) : null}

          {view === "feed" ? (
            <ActivityFeed
              refreshToken={refreshToken}
              title="All recovery opportunities"
              subtitle="Every opportunity the agent has seen, newest first. Open a row for the full reasoning chain."
            />
          ) : null}

          {view === "review" ? (
            <ReviewQueue refreshToken={refreshToken} onBack={() => navigate("overview")} />
          ) : null}

          <footer className="px-1 pt-1 text-2xs leading-relaxed text-ink-400">
            Razorpay branding and palette are used here as a visual placeholder
            only. This project is not affiliated with or endorsed by Razorpay.
          </footer>
        </main>
      </div>
    </div>
  );
}
