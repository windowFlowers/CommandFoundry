import { useCallback, useEffect, useRef, useState } from "react";
import {
  getMemoryExtractionTask,
  getProfile,
  isTerminalMemoryExtractionStatus,
  memoryUpdateToast,
  normalizeMemoryUpdate,
  undoProfileMemory,
} from "../lib/profile.js";


const EXTRACTION_POLL_INTERVAL_MS = 2_000;


function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}


export function useProfileMemory() {
  const [profile, setProfile] = useState(null);
  const [profileError, setProfileError] = useState("");
  const [memoryRevision, setMemoryRevision] = useState(0);
  const [toast, setToast] = useState(null);
  const mountedRef = useRef(true);
  const pollsRef = useRef(new Map());
  const seenUpdatesRef = useRef(new Set());

  const refreshProfile = useCallback(async () => {
    try {
      const next = await getProfile();
      if (mountedRef.current) {
        setProfile(next);
        setProfileError("");
      }
      return next;
    } catch (error) {
      if (mountedRef.current) setProfileError(error.message || "无法读取个性化状态");
      throw error;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    refreshProfile().catch(() => {});
    return () => {
      mountedRef.current = false;
      pollsRef.current.forEach((poll) => { poll.cancelled = true; });
      pollsRef.current.clear();
    };
  }, [refreshProfile]);

  const memoriesChanged = useCallback(async () => {
    if (mountedRef.current) setMemoryRevision((value) => value + 1);
    try { await refreshProfile(); } catch { /* The page keeps its local error state. */ }
  }, [refreshProfile]);

  const showMemoryUpdateToast = useCallback((update) => {
    const notification = memoryUpdateToast(update);
    if (!mountedRef.current || !notification) return;
    setToast({
      id: `${Date.now()}-${update.taskId || update.sourceMessageId || notification.operation}`,
      ...notification,
      working: false,
    });
    if (notification.memoryIds.length) setMemoryRevision((value) => value + 1);
    refreshProfile().catch(() => {});
  }, [refreshProfile]);

  const pollQueuedUpdate = useCallback(async (update) => {
    const pollKey = update.taskId || update.sourceMessageId;
    if (!pollKey || pollsRef.current.has(pollKey)) return;
    const poll = { cancelled: false };
    pollsRef.current.set(pollKey, poll);
    try {
      // The provider timeout is 45 seconds. Keep polling until the persisted
      // task reaches a terminal state instead of treating a shorter client
      // timer as failure; a queue can legitimately add additional delay.
      for (let attempt = 0; !poll.cancelled; attempt += 1) {
        if (attempt > 0) await wait(EXTRACTION_POLL_INTERVAL_MS);
        if (poll.cancelled) break;
        try {
          const payload = await getMemoryExtractionTask(update.taskId);
          const completed = normalizeMemoryUpdate(payload);
          if (completed && isTerminalMemoryExtractionStatus(completed.status)) {
            showMemoryUpdateToast(completed);
            break;
          }
        } catch (error) {
          if (error?.status === 404) {
            showMemoryUpdateToast({ ...update, status: "cancelled", operation: "NOOP" });
            break;
          }
          // A transient network error does not erase a persisted task. Keep
          // polling so failure and no-op outcomes are eventually visible.
        }
      }
    } finally {
      pollsRef.current.delete(pollKey);
    }
  }, [showMemoryUpdateToast]);

  const notifyMemoryUpdate = useCallback((payload) => {
    const update = normalizeMemoryUpdate(payload);
    if (!update) return;
    const identity = [
      update.taskId || "",
      update.sourceMessageId || "",
      update.status,
      update.operation,
      update.memoryIds.join(","),
    ].join(":");
    if (seenUpdatesRef.current.has(identity)) return;
    seenUpdatesRef.current.add(identity);
    if (seenUpdatesRef.current.size > 80) seenUpdatesRef.current.delete(seenUpdatesRef.current.values().next().value);

    if (update.status === "saved" || isTerminalMemoryExtractionStatus(update.status)) {
      showMemoryUpdateToast(update);
    } else if (update.status === "queued") {
      void pollQueuedUpdate(update);
    }
  }, [pollQueuedUpdate, showMemoryUpdateToast]);

  const dismissToast = useCallback(() => setToast(null), []);

  const undoToast = useCallback(async () => {
    const current = toast;
    if (!current || !current.undoableMemoryIds?.length || current.working) return;
    setToast({ ...current, working: true });
    try {
      for (const memoryId of current.undoableMemoryIds || []) await undoProfileMemory(memoryId);
      if (mountedRef.current) {
        setToast({ id: `${Date.now()}-undone`, message: "已撤销记忆", memoryIds: [], undoableMemoryIds: [], operation: "UNDO", working: false });
        setMemoryRevision((value) => value + 1);
      }
      refreshProfile().catch(() => {});
    } catch (error) {
      if (mountedRef.current) setToast({ ...current, message: error.message || "撤销失败", working: false, operation: "ERROR" });
    }
  }, [refreshProfile, toast]);

  return {
    profile,
    profileError,
    memoryRevision,
    toast,
    refreshProfile,
    memoriesChanged,
    notifyMemoryUpdate,
    dismissToast,
    undoToast,
  };
}
