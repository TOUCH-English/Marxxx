import { App } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, errText } from '../api/client';
import { useAppState } from './app';

export interface ApiState<T> {
  data: T | undefined;
  error: string | undefined;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * GET 取数：路径变化时重新加载；后台推送（tick）时静默刷新（保留旧数据，不闪烁）。
 * path 为 null 时不请求。
 */
export function useApi<T>(path: string | null, opts: { live?: boolean } = {}): ApiState<T> {
  const { tick } = useAppState();
  const live = opts.live !== false;
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);
  const pathRef = useRef(path);
  pathRef.current = path;
  const lastPath = useRef<string | null>(null);

  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const d = await api.get<T>(path);
      if (pathRef.current === path) {
        setData(d);
        setError(undefined);
      }
    } catch (e) {
      if (pathRef.current === path) {
        setError(errText(e));
        if (e instanceof ApiError && e.status === 404) setData(undefined);
      }
    } finally {
      if (pathRef.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    if (lastPath.current !== path) {
      lastPath.current = path;
      setData(undefined);
      setError(undefined);
    }
    void reload();
  }, [reload, path]);

  const firstTick = useRef(tick);
  useEffect(() => {
    if (!live || tick === firstTick.current) return;
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  return { data, error, loading, reload };
}

/**
 * 执行一个会改变状态的操作：失败时显示中文错误，成功时可显示提示并触发全局刷新。
 */
export function useAction() {
  const { message } = App.useApp();
  const { refresh, reloadOverview } = useAppState();
  const [busy, setBusy] = useState(false);
  const run = useCallback(
    async <R,>(fn: () => Promise<R>, opts: { success?: string | ((r: R) => string); silent?: boolean } = {}): Promise<R | undefined> => {
      setBusy(true);
      try {
        const r = await fn();
        if (opts.success) message.success(typeof opts.success === 'function' ? opts.success(r) : opts.success, 4);
        refresh();
        void reloadOverview();
        return r;
      } catch (e) {
        if (!opts.silent) message.error(errText(e), 6);
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [message, refresh, reloadOverview],
  );
  return { run, busy };
}
