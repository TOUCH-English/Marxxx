import {
  ApiOutlined,
  BellOutlined,
  BulbOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  HomeOutlined,
  LogoutOutlined,
  ProfileOutlined,
  RobotOutlined,
  RocketOutlined,
  SafetyOutlined,
  SettingOutlined,
  StockOutlined,
} from '@ant-design/icons';
import { Alert, App, Badge, Button, Drawer, Empty, Grid, Layout, List, Menu, Segmented, Space, Tag, Tooltip, Typography } from 'antd';
import { useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Items, NotificationRow } from '../api/types';
import { useAppState } from '../state/app';
import { useAction } from '../state/hooks';
import { fmtAge, fmtMyt } from '../utils/format';
import { NS_LABEL, type Ns } from '../utils/labels';

const { Sider, Header, Content } = Layout;
const { Text } = Typography;

export const NAV = [
  { key: '/simple', icon: <HomeOutlined />, label: '我的账户（简单版）' },
  { key: '/overview', icon: <DashboardOutlined />, label: '总览（专业版）' },
  { key: '/wizard', icon: <RocketOutlined />, label: '启动向导' },
  { key: '/connection', icon: <ApiOutlined />, label: '连接与权限诊断' },
  { key: '/instruments', icon: <StockOutlined />, label: '自选股与股票详情' },
  { key: '/decisions', icon: <RobotOutlined />, label: 'AI 分析与决策' },
  { key: '/risk', icon: <SafetyOutlined />, label: '策略与风控设置' },
  { key: '/orders', icon: <ProfileOutlined />, label: '订单与持仓' },
  { key: '/sim', icon: <ExperimentOutlined />, label: '模拟、回测和复盘' },
  { key: '/learning', icon: <BulbOutlined />, label: '复盘与改进' },
  { key: '/logs', icon: <SettingOutlined />, label: '日志与设置' },
];

function StatusItem({ label, children, tip }: { label: string; children: React.ReactNode; tip?: string }) {
  const body = (
    <span className="status-item">
      <span className="status-label">{label}</span>
      <span className="status-value">{children}</span>
    </span>
  );
  return tip ? <Tooltip title={tip}>{body}</Tooltip> : body;
}

export function StatusBar() {
  const { ns, overview, status, lastUpdated, streamOk, overviewError, statusError } = useAppState();
  const p = overview?.portfolio;
  const w = overview?.worker ?? status?.workers?.[ns];
  const runtimeAi = p?.runtime?.ai;
  // 优先显示当前运行实际使用的 AI（演示/回放即使配置了密钥也使用固定测试响应）
  const ai = status?.ai
    ? { ...status.ai, ...(runtimeAi ? { label: runtimeAi.label, is_fixture: runtimeAi.is_fixture } : {}) }
    : undefined;
  return (
    <div className={`status-bar ${ns === 'live' ? 'status-bar-live' : ''}`} data-testid="status-bar">
      <StatusItem label="运行模式">{p ? <Text strong>{p.mode_label}</Text> : <Text type="secondary">尚未启动运行</Text>}</StatusItem>
      <StatusItem label="数据来源">
        {p ? (
          <Tag color={p.data_source === 'MOOMOO_REALTIME' ? 'green' : 'gold'} style={{ marginInlineEnd: 0 }}>
            {p.data_source_label}
          </Tag>
        ) : (
          <Text type="secondary">尚未连接</Text>
        )}
      </StatusItem>
      <StatusItem label="业务时间（马来西亚）" tip={p?.is_replay ? '回放/演示模式下，业务时间是模拟时钟，不是现在的真实时间' : undefined}>
        {p ? (
          <>
            {p.business_time_myt}
            {p.is_replay ? <Tag style={{ marginLeft: 4, marginInlineEnd: 0 }}>回放时钟</Tag> : null}
          </>
        ) : (
          <Text type="secondary">暂无数据</Text>
        )}
      </StatusItem>
      <StatusItem label="最后更新">{lastUpdated ? `${lastUpdated}（MYT）` : <Text type="secondary">暂无数据</Text>}</StatusItem>
      <StatusItem label="工作进程" tip="后台工作进程负责行情、AI 决策、风控与下单；浏览器关闭不影响它运行">
        {w ? (
          w.alive ? (
            <Badge status="success" text={`运行中（心跳 ${fmtAge(w.age_s)}）`} />
          ) : (
            <Badge status="error" text={w.heartbeat_at ? `未运行（最后心跳 ${fmtMyt(w.heartbeat_at)}）` : '未运行'} />
          )
        ) : (
          <Text type="secondary">暂无数据</Text>
        )}
      </StatusItem>
      <StatusItem label="OpenD" tip={status ? `地址 ${status.opend.address}` : undefined}>
        {status ? (
          status.opend.reachable ? <Badge status="success" text="可连接" /> : <Badge status="default" text="尚未连接" />
        ) : (
          <Text type="secondary">暂无数据</Text>
        )}
      </StatusItem>
      <StatusItem label="AI">
        {ai ? (
          ai.is_fixture ? (
            <Tag color="orange" style={{ marginInlineEnd: 0 }}>固定测试响应（非真实 AI）</Tag>
          ) : (
            <span>
              {ai.label}
              {ai.model ? <Text type="secondary"> · {ai.model}</Text> : null}
              {runtimeAi?.pending_jobs ? <Text type="secondary">（排队 {runtimeAi.pending_jobs}）</Text> : null}
            </span>
          )
        ) : (
          <Text type="secondary">暂无数据</Text>
        )}
      </StatusItem>
      <StatusItem label="推送">{streamOk ? <Badge status="processing" text="实时" /> : <Badge status="warning" text="轮询（10 秒）" />}</StatusItem>
      {overviewError || statusError ? (
        <Text type="danger" style={{ fontSize: 12 }}>
          {overviewError || statusError}
        </Text>
      ) : null}
    </div>
  );
}

function NotificationBell() {
  const { ns, overview } = useAppState();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<NotificationRow[]>();
  const [loading, setLoading] = useState(false);
  const { run, busy } = useAction();
  const unread = overview?.notifications_unread ?? 0;

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.get<Items<NotificationRow>>(`/api/${ns}/notifications`);
      setItems(r.items);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <Badge count={unread} size="small" overflowCount={99}>
        <Button
          type="text"
          aria-label="通知"
          data-testid="bell"
          icon={<BellOutlined style={{ fontSize: 18 }} />}
          onClick={() => {
            setOpen(true);
            void load();
          }}
        />
      </Badge>
      <Drawer
        title={`站内通知（${NS_LABEL[ns]}）`}
        open={open}
        onClose={() => setOpen(false)}
        width={Math.min(460, window.innerWidth)}
        extra={
          <Button
            size="small"
            loading={busy}
            disabled={!items?.some((n) => !n.read)}
            onClick={async () => {
              await run(() => api.post(`/api/${ns}/notifications/read`), { success: '已全部标为已读' });
              void load();
            }}
          >
            全部标为已读
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="首版只提供站内通知与异常日志；邮件/推送未配置。关闭页面后您可能收不到通知。"
        />
        {items && items.length === 0 ? <Empty description="暂无通知" /> : null}
        <List<NotificationRow>
          loading={loading}
          dataSource={items ?? []}
          renderItem={(n) => (
            <List.Item>
              <List.Item.Meta
                title={
                  <Space wrap>
                    {!n.read ? <Badge status="processing" /> : null}
                    <Tag color={n.level === 'error' ? 'red' : n.level === 'warning' ? 'orange' : 'blue'}>
                      {n.level === 'error' ? '异常' : n.level === 'warning' ? '警告' : '信息'}
                    </Tag>
                    <span>{n.title}</span>
                  </Space>
                }
                description={
                  <>
                    <div style={{ whiteSpace: 'pre-wrap' }}>{n.body}</div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {fmtMyt(n.created_at_utc, true)}
                    </Text>
                  </>
                }
              />
            </List.Item>
          )}
        />
      </Drawer>
    </>
  );
}

export function AppLayout({ onLoggedOut }: { onLoggedOut: () => void }) {
  const { ns, setNs, overview, status } = useAppState();
  const nav = useNavigate();
  const loc = useLocation();
  const { modal, message } = App.useApp();
  const screens = Grid.useBreakpoint();
  const [collapsed, setCollapsed] = useState(false);
  const selected = NAV.find((n) => loc.pathname.startsWith(n.key))?.key ?? '/overview';
  const liveEnabled = overview?.deployment.live_trading_enabled ?? status?.deployment.live_trading_enabled;

  const logout = () =>
    modal.confirm({
      title: '退出登录？',
      content: '退出只结束这个网页会话。已获授权的后台运行会继续按授权运行（包括持仓保护）；如需停止，请先在总览页「停止本次运行」。',
      okText: '退出登录',
      cancelText: '取消',
      onOk: async () => {
        try {
          const r = await api.post<{ note?: string }>('/api/auth/logout');
          if (r?.note) message.info(r.note, 6);
        } catch {
          /* 即使失败也回到登录页 */
        }
        // 先让确认框关闭，再切换到登录页
        window.setTimeout(onLoggedOut, 0);
      },
    });

  return (
    <Layout className={`app-root ${ns === 'live' ? 'ns-live' : 'ns-sim'}`}>
      <Sider
        breakpoint="lg"
        collapsedWidth={0}
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={208}
        className="app-sider"
        theme="light"
      >
        <div className="logo">
          <span className="logo-mark">M</span>
          <span>AI 自动交易工作台</span>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[selected]}
          items={NAV}
          onClick={(e) => {
            nav(e.key);
            if (!screens.lg) setCollapsed(true);
          }}
        />
        <div className="sider-foot">
          <Text type="secondary">本系统不构成投资建议。模拟结果不代表真实收益。</Text>
        </div>
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space wrap size={8} className="header-left">
            <Segmented<Ns>
              data-testid="ns-switch"
              value={ns}
              onChange={(v) => setNs(v)}
              options={[
                { label: '自动模拟', value: 'sim' },
                { label: <span className={ns === 'live' ? 'live-label' : undefined}>真实自动交易</span>, value: 'live' },
              ]}
            />
            {ns === 'live' ? <Tag color="red">实盘区域：涉及真实资金</Tag> : <Tag color="blue">模拟区域：不涉及真实资金</Tag>}
          </Space>
          <Space size={4}>
            <NotificationBell />
            <Tooltip title="退出登录（后台运行不受影响）">
              <Button type="text" icon={<LogoutOutlined />} onClick={logout} data-testid="logout">
                {screens.md ? '退出' : null}
              </Button>
            </Tooltip>
          </Space>
        </Header>
        <StatusBar />
        {ns === 'live' && liveEnabled === false ? (
          <Alert
            banner
            type="error"
            className="live-banner"
            message="真实交易在部署配置中关闭（默认）"
            description="实盘自动交易需要在本机 .env 中开启部署开关，并通过「连接与权限诊断」里的全部实盘检查；网页上的开关不能单独开启实盘。"
          />
        ) : null}
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
