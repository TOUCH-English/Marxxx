import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, errText } from '../api/client';
import type { Overview, SystemStatus } from '../api/types';
import { nowMytClock } from '../utils/format';
import type { Ns } from '../utils/labels';

const NS_KEY = 'mt_ns';

function loadNs(): Ns {
  try {
    const v = window.localStorage.getItem(NS_KEY);
    return v === 'live' ? 'live' : 'sim';
  } catch {
    return 'sim';
  }
}

function saveNs(ns: Ns) {
  try {
    window.localStorage.setItem(NS_KEY, ns);
  } catch {
    /* 隐私模式等情况下忽略 */
  }
}

interface AppCtx {
  ns: Ns;
  setNs: (ns: Ns) => void;
  /** 每当后台有新事件（SSE）或用户操作后递增，页面据此重新取数。 */
  tick: number;
  refresh: () => void;
  overview?: Overview;
  overviewError?: string;
  reloadOverview: () => Promise<void>;
  status?: SystemStatus;
  statusError?: string;
  reloadStatus: () => Promise<void>;
  lastUpdated?: string;
  streamOk: boolean;
}

const Ctx = createContext<AppCtx | null>(null);

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [ns, setNsState] = useState<Ns>(loadNs);
  const [tick, setTick] = useState(0);
  const [overview, setOverview] = useState<Overview>();
  const [overviewError, setOverviewError] = useState<string>();
  const [status, setStatus] = useState<SystemStatus>();
  const [statusError, setStatusError] = useState<string>();
  const [lastUpdated, setLastUpdated] = useState<string>();
  const [streamOk, setStreamOk] = useState(false);
  const nsRef = useRef(ns);
  nsRef.current = ns;

  const setNs = useCallback((v: Ns) => {
    saveNs(v);
    setOverview(undefined);
    setNsState(v);
  }, []);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  const reloadOverview = useCallback(async () => {
    const want = nsRef.current;
    try {
      const o = await api.get<Overview>(`/api/${want}/overview`);
      if (nsRef.current !== want) return;
      setOverview(o);
      setOverviewError(undefined);
      setLastUpdated(nowMytClock());
    } catch (e) {
      if (nsRef.current === want) setOverviewError(errText(e));
    }
  }, []);

  const reloadStatus = useCallback(async () => {
    try {
      setStatus(await api.get<SystemStatus>('/api/system/status'));
      setStatusError(undefined);
    } catch (e) {
      setStatusError(errText(e));
    }
  }, []);

  // 总览：切换命名空间、收到推送时刷新；另外每 10 秒轮询一次作为后备。
  useEffect(() => {
    void reloadOverview();
  }, [ns, tick, reloadOverview]);

  useEffect(() => {
    const t = window.setInterval(() => void reloadOverview(), 10000);
    return () => window.clearInterval(t);
  }, [reloadOverview]);

  useEffect(() => {
    void reloadStatus();
    const t = window.setInterval(() => void reloadStatus(), 10000);
    return () => window.clearInterval(t);
  }, [reloadStatus]);

  // 实时推送：同源 EventSource。只有出现新的审计事件时才触发页面刷新（节流）。
  useEffect(() => {
    let es: EventSource | null = null;
    let lastAudit: number | null = null;
    let pending: number | null = null;
    let lastFire = 0;
    let retry: number | null = null;
    let closed = false;

    const fire = () => {
      pending = null;
      lastFire = Date.now();
      setTick((t) => t + 1);
    };

    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(`/api/${ns}/stream`);
      } catch {
        setStreamOk(false);
        return;
      }
      es.onopen = () => setStreamOk(true);
      es.onmessage = (ev) => {
        setStreamOk(true);
        let auditId: number | null = null;
        try {
          const d = JSON.parse(ev.data) as { audit_id?: number };
          auditId = typeof d.audit_id === 'number' ? d.audit_id : null;
        } catch {
          return;
        }
        if (auditId === null) return;
        if (lastAudit === null) {
          lastAudit = auditId;
          return;
        }
        if (auditId === lastAudit) return;
        lastAudit = auditId;
        if (pending !== null) return;
        const wait = Math.max(800, 3000 - (Date.now() - lastFire));
        pending = window.setTimeout(fire, wait);
      };
      es.onerror = () => {
        setStreamOk(false);
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          es = null;
          if (!closed) retry = window.setTimeout(connect, 10000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      if (pending !== null) window.clearTimeout(pending);
      if (retry !== null) window.clearTimeout(retry);
      es?.close();
    };
  }, [ns]);

  const value = useMemo<AppCtx>(
    () => ({
      ns,
      setNs,
      tick,
      refresh,
      overview,
      overviewError,
      reloadOverview,
      status,
      statusError,
      reloadStatus,
      lastUpdated,
      streamOk,
    }),
    [ns, setNs, tick, refresh, overview, overviewError, reloadOverview, status, statusError, reloadStatus, lastUpdated, streamOk],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAppState(): AppCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error('AppStateProvider missing');
  return c;
}
