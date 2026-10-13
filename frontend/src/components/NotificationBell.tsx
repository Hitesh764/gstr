import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getUnreadCount,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "../api/client";
import type { AppNotification } from "../types";
import { relativeTime } from "../utils/time";

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Polled rather than pushed. A websocket would be better, but polling a
  // single integer once a minute is cheap and avoids holding a connection
  // open per signed-in user for a feature this small.
  const countQuery = useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: getUnreadCount,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  const listQuery = useQuery({
    queryKey: ["notifications", "list"],
    queryFn: () => listNotifications(false),
    enabled: open,
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["notifications"] });
  }

  const readOne = useMutation({ mutationFn: markNotificationRead, onSuccess: refresh });
  const readAll = useMutation({ mutationFn: markAllNotificationsRead, onSuccess: refresh });

  // Close when clicking outside or pressing Escape, so the panel does not
  // strand itself over the page.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const unread = countQuery.data ?? 0;

  function openItem(item: AppNotification) {
    if (!item.read_status) readOne.mutate(item.id);
    setOpen(false);
    if (item.link) navigate(item.link);
  }

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        aria-expanded={open}
        className="relative rounded border border-slate-300 px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
      >
        <span aria-hidden="true">🔔</span>
        {unread > 0 && (
          <span className="absolute -right-1.5 -top-1.5 min-w-5 rounded-full bg-rose-600 px-1 text-center text-xs font-medium leading-5 text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-96 max-w-[90vw] rounded-lg border border-slate-200 bg-white shadow-lg">
          <div className="flex items-center gap-2 border-b border-slate-200 px-3 py-2">
            <span className="text-sm font-semibold text-slate-900">Notifications</span>
            {unread > 0 && (
              <button
                onClick={() => readAll.mutate()}
                disabled={readAll.isPending}
                className="ml-auto text-xs text-slate-600 underline hover:text-slate-900"
              >
                Mark all read
              </button>
            )}
          </div>

          <div className="max-h-96 overflow-y-auto">
            {listQuery.isLoading && (
              <p className="px-3 py-6 text-center text-sm text-slate-500">Loading…</p>
            )}
            {listQuery.data?.length === 0 && (
              <p className="px-3 py-6 text-center text-sm text-slate-500">
                Nothing yet.
              </p>
            )}
            {listQuery.data?.map((item) => (
              <button
                key={item.id}
                onClick={() => openItem(item)}
                className={`block w-full border-b border-slate-100 px-3 py-2 text-left hover:bg-slate-50 ${
                  item.read_status ? "" : "bg-sky-50/50"
                }`}
              >
                <div className="flex items-start gap-2">
                  {!item.read_status && (
                    <span
                      aria-hidden="true"
                      className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-sky-600"
                    />
                  )}
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-900">
                      {item.title}
                    </p>
                    {item.message && (
                      <p className="mt-0.5 line-clamp-2 text-xs text-slate-600">
                        {item.message}
                      </p>
                    )}
                    <p className="mt-0.5 text-xs text-slate-400">
                      {relativeTime(item.created_at)}
                    </p>
                  </div>
                </div>
              </button>
            ))}
          </div>

          <div className="border-t border-slate-200 px-3 py-2 text-right">
            <Link
              to="/settings/notifications"
              onClick={() => setOpen(false)}
              className="text-xs text-slate-600 underline hover:text-slate-900"
            >
              Notification settings
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
