import { Result, Spin, Button } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { api, apiHandlers, errText } from './api/client';
import type { AuthState } from './api/types';
import { AppLayout } from './components/AppLayout';
import { DrawerProvider } from './components/Drawers';
import { ReauthHost } from './components/Reauth';
import { AppStateProvider } from './state/app';
import { LoginPage, SetupPage } from './pages/Login';
import { OverviewPage } from './pages/Overview';
import { SimplePage } from './pages/Simple';
import { WizardPage } from './pages/Wizard';
import { ConnectionPage } from './pages/Connection';
import { InstrumentsPage, InstrumentDetailPage } from './pages/Instruments';
import { DecisionsPage } from './pages/Decisions';
import { RiskPage } from './pages/Risk';
import { OrdersPage } from './pages/Orders';
import { SimPage } from './pages/Sim';
import { LearningPage } from './pages/Learning';
import { LogsPage } from './pages/Logs';

export default function Root() {
  const [auth, setAuth] = useState<AuthState>();
  const [err, setErr] = useState<string>();

  const check = useCallback(async () => {
    try {
      setAuth(await api.get<AuthState>('/api/auth/state'));
      setErr(undefined);
    } catch (e) {
      setErr(errText(e));
    }
  }, []);

  useEffect(() => {
    void check();
    apiHandlers.onUnauthorized = () => setAuth((a) => ({ password_set: a?.password_set ?? true, logged_in: false }));
    return () => {
      apiHandlers.onUnauthorized = undefined;
    };
  }, [check]);

  if (err && !auth) {
    return (
      <Result
        status="warning"
        title="无法连接本机后端"
        subTitle={`${err}。请确认已运行启动脚本（scripts/start），然后刷新页面。`}
        extra={<Button onClick={() => void check()}>重试</Button>}
      />
    );
  }
  if (!auth) return <Spin fullscreen tip="正在连接本机后端…" />;
  if (!auth.password_set) return <SetupPage onDone={() => void check()} />;
  if (!auth.logged_in) return <LoginPage onDone={() => void check()} />;

  return (
    <AppStateProvider>
      <DrawerProvider>
        <ReauthHost />
        <Routes>
          <Route element={<AppLayout onLoggedOut={() => void check()} />}>
            <Route index element={<Navigate to="/simple" replace />} />
            <Route path="/simple" element={<SimplePage />} />
            <Route path="/overview" element={<OverviewPage />} />
            <Route path="/wizard" element={<WizardPage />} />
            <Route path="/connection" element={<ConnectionPage />} />
            <Route path="/instruments" element={<InstrumentsPage />} />
            <Route path="/instruments/:symbol" element={<InstrumentDetailPage />} />
            <Route path="/decisions" element={<DecisionsPage />} />
            <Route path="/risk" element={<RiskPage />} />
            <Route path="/orders" element={<OrdersPage />} />
            <Route path="/sim" element={<SimPage />} />
            <Route path="/learning" element={<LearningPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="*" element={<Navigate to="/simple" replace />} />
          </Route>
        </Routes>
      </DrawerProvider>
    </AppStateProvider>
  );
}
