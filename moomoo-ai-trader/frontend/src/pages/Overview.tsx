import {
  CheckCircleTwoTone,
  CloseCircleTwoTone,
  PauseCircleOutlined,
  PlayCircleOutlined,
  StopOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Alert, App, Button, Card, Checkbox, Col, Collapse, Descriptions, Empty, List, Modal, Row, Space, Statistic, Table, Tag, Typography } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { CommandRow, CurrencyView, OverviewPosition } from '../api/types';
import { Money, PageTitle, Explain, DownloadLink } from '../components/common';
import { useDrawers } from '../components/Drawers';
import { useAppState } from '../state/app';
import { useAction } from '../state/hooks';
import { fmtMoney, fmtPct, fmtPrice, sumDecimals } from '../utils/decimal';
import { fmtMyt } from '../utils/format';
import { ACTION_LABELS, AUDIT_CATEGORY_LABELS, LOCK_KIND_LABELS, NS_LABEL } from '../utils/labels';

const { Text, Paragraph } = Typography;

function CurrencyCard({ c }: { c: CurrencyView }) {
  const rows: { label: string; node: React.ReactNode; hint?: string }[] = [
    { label: '系统获分配额度', node: <Money v={c.allocation} ccy={c.currency} /> },
    { label: '现金', node: <Money v={c.cash} ccy={c.currency} /> },
    { label: '已预留（挂单占用）', node: <Money v={c.reserved} ccy={c.currency} /> },
    { label: '可用现金', node: <Money v={c.available} ccy={c.currency} /> },
    { label: '持仓市值', node: <Money v={c.market_value} ccy={c.currency} /> },
    { label: '策略净值', node: <Money v={c.equity} ccy={c.currency} /> },
    { label: '已实现盈亏', node: <Money v={c.realized_pnl} ccy={c.currency} pnl /> },
    { label: '未实现盈亏（浮动）', node: <Money v={c.unrealized_pnl} ccy={c.currency} pnl /> },
    { label: '今日盈亏', node: <Money v={c.day_pnl} ccy={c.currency} pnl /> },
    { label: '累计盈亏（净值 − 额度）', node: <Money v={c.total_pnl} ccy={c.currency} pnl /> },
    { label: '累计费用', node: <Money v={c.fees} ccy={c.currency} /> },
    { label: '最大回撤', node: fmtPct(c.max_drawdown_pct) },
  ];
  return (
    <Card size="small" title={<Space><Tag color={c.currency === 'MYR' ? 'blue' : 'purple'}>{c.currency}</Tag>{c.currency === 'MYR' ? '马股账本（令吉）' : '美股账本（美元）'}</Space>} className="card-fill">
      <Descriptions size="small" layout="vertical" colon={false} column={{ xs: 2, sm: 3, md: 4, lg: 4, xl: 3, xxl: 4 }} items={rows.map((r) => ({ key: r.label, label: r.label, children: r.node }))} />
      {c.missing_prices?.length ? (
        <Alert type="warning" showIcon style={{ marginTop: 8 }} message={`以下持仓暂无最新价，市值未计入：${c.missing_prices.join('、')}`} />
      ) : null}
    </Card>
  );
}

function PositionsTable({ items }: { items: OverviewPosition[] }) {
  return (
    <Table<OverviewPosition>
      size="small"
      rowKey="id"
      dataSource={items}
      pagination={false}
      scroll={{ x: 980 }}
      locale={{ emptyText: '系统当前没有管理的持仓' }}
      columns={[
        { title: '证券', render: (_, p) => <Space direction="vertical" size={0}><Text strong>{p.symbol}</Text><Text type="secondary" style={{ fontSize: 12 }}>{p.name}</Text></Space> },
        { title: '数量', dataIndex: 'qty' },
        { title: '成本价', dataIndex: 'avg_cost', render: (v: string) => fmtPrice(v) },
        { title: '最新价', render: (_, p) => (p.last ? <span>{fmtPrice(p.last)}<div><Text type="secondary" style={{ fontSize: 11 }}>{fmtMyt(p.quote_ts)}</Text></div></span> : <Text type="secondary">暂无报价</Text>) },
        { title: '市值', render: (_, p) => <Money v={p.market_value} ccy={p.currency} /> },
        { title: '浮动盈亏', render: (_, p) => <span><Money v={p.unrealized} pnl />{p.unrealized_pct ? <Text type="secondary">（{fmtPct(p.unrealized_pct, true)}）</Text> : null}</span> },
        { title: '保护：止损 / 止盈', render: (_, p) => `${fmtPrice(p.stop_price)} / ${fmtPrice(p.take_profit_price)}` },
        { title: '最长持有至', dataIndex: 'max_hold_until', render: (v: string | null) => fmtMyt(v) },
        { title: '状态', render: (_, p) => (p.reconcile_locked ? <Tag color="magenta">对账锁定</Tag> : <Tag color="green">受保护</Tag>) },
      ]}
    />
  );
}

/** 指令（紧急停止、清仓等）提交后，轮询后台处理结果。 */
function useCommandWatch() {
  const { ns } = useAppState();
  const [cmd, setCmd] = useState<CommandRow | null>(null);
  const watch = (id: number) => {
    let n = 0;
    const poll = async () => {
      n += 1;
      try {
        const c = await api.get<CommandRow>(`/api/${ns}/commands/${id}`);
        setCmd(c);
        if (c.status === 'PENDING' && n < 20) window.setTimeout(poll, 2000);
      } catch {
        /* ignore */
      }
    };
    void poll();
  };
  return { cmd, watch, clear: () => setCmd(null) };
}

function LiquidateFlow() {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<{ positions: OverviewPosition[]; note: string } | null>(null);
  const [agree, setAgree] = useState(false);
  const { cmd, watch } = useCommandWatch();

  const start = async () => {
    setAgree(false);
    const r = await run(() => api.get<{ positions: OverviewPosition[]; note: string }>(`/api/${ns}/control/liquidate/preview`), { silent: false });
    if (r) {
      setPreview(r);
      setOpen(true);
    }
  };
  const byCcy: Record<string, string[]> = {};
  for (const p of preview?.positions ?? []) (byCcy[p.currency] ??= []).push(p.market_value ?? '0');

  return (
    <>
      <Button icon={<WarningOutlined />} onClick={start} loading={busy} data-testid="liquidate-btn">
        手动清仓（高影响操作）…
      </Button>
      {cmd ? (
        <Alert
          style={{ marginTop: 8 }}
          type={cmd.status === 'DONE' ? 'success' : cmd.status === 'PENDING' ? 'info' : 'warning'}
          showIcon
          message={`清仓指令 #${cmd.id}：${cmd.status === 'PENDING' ? '等待后台处理' : cmd.status}`}
          description={cmd.result || '卖出订单与成交情况请到「订单与持仓」查看。'}
        />
      ) : null}
      <Modal
        open={open}
        title="手动清仓：卖出全部系统管理的持仓"
        okText="确认清仓"
        okButtonProps={{ danger: true, disabled: !agree || !preview?.positions.length }}
        cancelText="取消"
        confirmLoading={busy}
        onCancel={() => setOpen(false)}
        width={Math.min(720, window.innerWidth)}
        onOk={async () => {
          const r = await run(() => api.post<{ command_id: number }>(`/api/${ns}/control/liquidate`, { confirm: true }), {
            success: '清仓指令已提交，后台会按限价卖出系统管理的持仓',
          });
          if (r) {
            setOpen(false);
            watch(r.command_id);
          }
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="这不是「紧急停止」"
          description="紧急停止只停止新开仓、保留保护单和持仓；手动清仓会真正卖出持仓。只影响本系统管理的持仓，不会动账户里的其他投资。"
        />
        {preview?.positions.length ? (
          <>
            <Table<OverviewPosition>
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={preview.positions}
              scroll={{ x: true }}
              columns={[
                { title: '证券', render: (_, p) => `${p.symbol} ${p.name}` },
                { title: '数量', dataIndex: 'qty' },
                { title: '最新价', dataIndex: 'last', render: (v: string | null) => fmtPrice(v) },
                { title: '估算金额', render: (_, p) => <Money v={p.market_value} ccy={p.currency} /> },
              ]}
            />
            <Paragraph style={{ marginTop: 8 }}>
              估算合计：
              {Object.entries(byCcy).map(([ccy, vals]) => (
                <Tag key={ccy}>{fmtMoney(sumDecimals(vals))} {ccy}</Tag>
              ))}
              （按币种分别计算，不相加）
            </Paragraph>
          </>
        ) : (
          <Empty description="系统当前没有管理的持仓，无需清仓" />
        )}
        <Paragraph type="secondary">{preview?.note}</Paragraph>
        <Checkbox checked={agree} onChange={(e) => setAgree(e.target.checked)} data-testid="liquidate-agree">
          我明白：这会卖出以上全部持仓；按买一价附近的限价卖出，可能部分成交或无法成交；{ns === 'live' ? '这是真实账户操作，需要再次输入密码。' : '这是模拟操作。'}
        </Checkbox>
      </Modal>
    </>
  );
}

export function OverviewPage() {
  const { ns, overview: o, overviewError, reloadOverview } = useAppState();
  const nav = useNavigate();
  const { openDecision, openTrace } = useDrawers();
  const { run, busy } = useAction();
  const { modal } = App.useApp();
  const stopCmd = useCommandWatch();

  if (!o) {
    return (
      <>
        <PageTitle title="总览" />
        {overviewError ? <Alert type="error" showIcon message="无法读取总览" description={overviewError} /> : <Card loading />}
      </>
    );
  }
  const p = o.portfolio;

  if (!p) {
    return (
      <>
        <PageTitle title={`总览 · ${NS_LABEL[ns]}`} />
        <Card>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <Space direction="vertical">
                <Text strong style={{ fontSize: 16 }}>尚未启动{ns === 'live' ? '真实自动交易' : '自动模拟'}</Text>
                <Text type="secondary">只需设置可使用资金、可承受亏损和交易范围，确认一次授权后，AI 会在范围内自动分析、买卖，并由程序风控把关。</Text>
                {o.allow_new_entries.reasons.map((r) => (
                  <Text key={r} type="secondary">· {r}</Text>
                ))}
              </Space>
            }
          >
            <Button type="primary" size="large" icon={<PlayCircleOutlined />} onClick={() => nav('/wizard')} data-testid="go-wizard">
              打开启动向导
            </Button>
          </Empty>
        </Card>
      </>
    );
  }

  const runState = !p.running
    ? { text: '已停止', color: '#8c8c8c', tag: 'default' }
    : p.emergency_stop
      ? { text: '已紧急停止新交易', color: '#cf1322', tag: 'red' }
      : p.pause_new_entries
        ? { text: '运行中 · 已暂停新开仓', color: '#d46b08', tag: 'orange' }
        : { text: '运行中', color: '#389e0d', tag: 'green' };

  const confirmPause = () =>
    modal.confirm({
      title: p.pause_new_entries ? '恢复新开仓？' : '暂停新开仓？',
      content: p.pause_new_entries
        ? '恢复后，AI 可以在授权范围内重新开新仓（仍受全部风控限制）。'
        : '暂停后不再买入新的股票；已有持仓的止损、止盈、持有期限等保护性卖出继续运行。这不是清仓，不会卖出您的持仓。',
      okText: p.pause_new_entries ? '恢复新开仓' : '暂停新开仓',
      cancelText: '取消',
      onOk: () =>
        run(() => api.post(`/api/${ns}/control/pause`, { paused: !p.pause_new_entries }), {
          success: p.pause_new_entries ? '已恢复新开仓' : '已暂停新开仓（保护性退出继续运行）',
        }),
    });

  const confirmEmergency = () =>
    modal.confirm({
      title: '紧急停止新交易？',
      icon: <ThunderboltOutlined style={{ color: '#cf1322' }} />,
      content: (
        <div>
          <p>系统会：停止产生新的开仓、撤销系统挂着的入场买单，并暂停新开仓。</p>
          <p>
            <strong>不会清仓：</strong>保护性止损/止盈订单和已有持仓默认保留，继续按批准的规则保护。撤单结果以券商逐单确认为准。
          </p>
          <p>如果确实要卖出持仓，请使用下方单独的「手动清仓」。</p>
        </div>
      ),
      okText: '紧急停止新交易',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const r = await run(() => api.post<{ command_id: number }>(`/api/${ns}/control/emergency-stop`), {
          success: '已紧急停止新交易；持仓与保护单保留',
        });
        if (r) stopCmd.watch(r.command_id);
      },
    });

  const confirmClearEmergency = () =>
    modal.confirm({
      title: '解除紧急停止？',
      content: '解除后新开仓仍保持「暂停」，需要您再单独点击「恢复新开仓」。',
      okText: '解除紧急停止',
      cancelText: '取消',
      onOk: () => run(() => api.post(`/api/${ns}/control/clear-emergency`, { confirm: true }), { success: '已解除紧急停止（新开仓仍暂停）' }),
    });

  const confirmStopRun = () =>
    modal.confirm({
      title: '停止本次运行（撤销授权）？',
      content: '撤销运行授权后不再开新仓。已有持仓继续由程序按批准的止损/止盈/持有期限规则保护和自动退出，不会被立即卖出。以后需要重新用启动向导授权。',
      okText: '停止本次运行',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () =>
        run(() => api.post<{ note?: string; revoked?: number }>(`/api/${ns}/control/stop-run`), {
          success: (r) => r.note ?? '已停止本次运行',
        }),
    });

  const confirmEndSim = () =>
    modal.confirm({
      title: '结束本次模拟？',
      content: '结束后模拟时钟停止，这次模拟的记录、日报与订单仍保留可查。可以随时用启动向导开始新的模拟。',
      okText: '结束模拟',
      cancelText: '取消',
      onOk: () => run(() => api.post(`/api/${ns}/control/end-sim`), { success: '已结束本次模拟' }),
    });

  const today = o.today ?? {};
  const allowOk = o.allow_new_entries.ok;

  return (
    <div data-testid="overview">
      <PageTitle
        title={`总览 · ${NS_LABEL[ns]}`}
        sub={p.is_replay ? '回放/演示：业务时间为模拟时钟；所有成交为本地模拟，不涉及真实资金。' : undefined}
        extra={<Button onClick={() => void reloadOverview()}>刷新</Button>}
      />

      <Row gutter={[12, 12]}>
        <Col xs={24} md={8}>
          <Card size="small" className="card-fill" title="当前模式">
            <div className="big-state" style={{ color: ns === 'live' ? '#cf1322' : '#1677ff' }}>{NS_LABEL[ns]}</div>
            <Paragraph style={{ margin: '6px 0 0' }}>{p.mode_label}</Paragraph>
            <Text type="secondary">{p.data_source_label}</Text>
            {o.auth?.strategy ? <div><Text type="secondary">策略：{o.auth.strategy}</Text></div> : null}
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card size="small" className="card-fill" title="运行 / 暂停">
            <div className="big-state" style={{ color: runState.color }} data-testid="run-state">{runState.text}</div>
            {o.auth ? (
              <Paragraph style={{ margin: '6px 0 0' }}>
                授权：<Tag color={o.auth.ok ? 'green' : 'red'}>{o.auth.ok ? '有效' : o.auth.reason}</Tag>
                到期 {fmtMyt(o.auth.expires_at)}
              </Paragraph>
            ) : (
              <Paragraph type="secondary">没有运行授权</Paragraph>
            )}
            <Text type="secondary">业务时间 {p.business_time_myt}（马来西亚）</Text>
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card size="small" className="card-fill" title="现在能否开新仓" data-testid="allow-card">
            <Space align="start">
              {allowOk ? <CheckCircleTwoTone twoToneColor="#52c41a" style={{ fontSize: 28 }} /> : <CloseCircleTwoTone twoToneColor="#ff4d4f" style={{ fontSize: 28 }} />}
              <div>
                <div className="big-state" style={{ color: allowOk ? '#389e0d' : '#cf1322' }}>{allowOk ? '允许新开仓' : '不允许新开仓'}</div>
                {o.allow_new_entries.reasons.map((r) => (
                  <div key={r}><Text type="danger">· {r}</Text></div>
                ))}
                {allowOk ? <Text type="secondary">AI 有合格机会时会在授权范围内自动买入；没有合格机会时不交易也是正常结果。</Text> : null}
              </div>
            </Space>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="控制（这些都不是清仓）" style={{ marginTop: 12 }}>
        <Row gutter={[12, 12]}>
          <Col xs={24} sm={12} lg={6}>
            <Button
              block
              size="large"
              type={p.pause_new_entries ? 'primary' : 'default'}
              icon={p.pause_new_entries ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
              onClick={confirmPause}
              loading={busy}
              disabled={!p.running}
              data-testid="pause-btn"
            >
              {p.pause_new_entries ? '恢复新开仓' : '暂停新开仓'}
            </Button>
            <div className="control-explain">只停止买入新股票；已批准的减仓与止损/止盈保护继续运行，不卖出持仓。</div>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            {p.emergency_stop ? (
              <Button block size="large" onClick={confirmClearEmergency} loading={busy} data-testid="clear-emergency-btn">
                解除紧急停止
              </Button>
            ) : (
              <Button block size="large" danger type="primary" icon={<ThunderboltOutlined />} onClick={confirmEmergency} loading={busy} disabled={!p.running} data-testid="emergency-btn">
                紧急停止新交易
              </Button>
            )}
            <div className="control-explain">停止新开仓并撤销系统挂着的入场单；保护性订单与持仓默认保留，不会卖出您的资产。</div>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Button block size="large" icon={<StopOutlined />} onClick={confirmStopRun} loading={busy} disabled={!o.auth || o.auth.status !== 'ACTIVE'}>
              停止本次运行（撤销授权）
            </Button>
            <div className="control-explain">撤销运行授权、不再开新仓；已有持仓继续按批准的保护规则管理。</div>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            {ns === 'sim' ? (
              <>
                <Button block size="large" onClick={confirmEndSim} loading={busy} disabled={!p.running}>
                  结束模拟
                </Button>
                <div className="control-explain">停止模拟时钟，保留全部记录。</div>
              </>
            ) : (
              <>
                <Button block size="large" onClick={() => run(() => api.post(`/api/${ns}/control/reconcile`), { success: '已请求立即对账' })} loading={busy}>
                  立即对账
                </Button>
                <div className="control-explain">与券商核对持仓、现金和订单；有差异时会锁定相关标的。</div>
              </>
            )}
          </Col>
        </Row>
        {stopCmd.cmd ? (
          <Alert
            style={{ marginTop: 12 }}
            showIcon
            type={stopCmd.cmd.status === 'PENDING' ? 'info' : 'success'}
            message={`紧急停止指令 #${stopCmd.cmd.id}：${stopCmd.cmd.status === 'PENDING' ? '等待后台处理' : stopCmd.cmd.status}`}
            description={stopCmd.cmd.result || '撤单结果以券商逐单确认为准，可在「订单与持仓」查看。'}
          />
        ) : null}
      </Card>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card size="small" className="card-fill" title={`今天做了什么（${today.day ?? '—'}，马来西亚时间）`} data-testid="today-card">
            <Space wrap style={{ marginBottom: 8 }}>
              <Text type="secondary">决策：</Text>
              {today.decisions && Object.keys(today.decisions).length ? (
                Object.entries(today.decisions).map(([k, v]) => <Tag key={k}>{k} {v}</Tag>)
              ) : (
                <Text type="secondary">今天还没有决策</Text>
              )}
            </Space>
            <Space wrap style={{ marginBottom: 8 }}>
              <Text type="secondary">订单：</Text>
              {today.orders && Object.keys(today.orders).length ? (
                Object.entries(today.orders).map(([k, v]) => <Tag key={k}>{k} {v}</Tag>)
              ) : (
                <Text type="secondary">今天没有订单</Text>
              )}
            </Space>
            <List
              size="small"
              header={<Text strong>已自动执行的决策</Text>}
              dataSource={today.executed ?? []}
              locale={{ emptyText: '今天没有自动执行的交易' }}
              renderItem={(x) => (
                <List.Item className="clickable-row" onClick={() => openDecision(x.id)}>
                  <Space wrap>
                    <Tag color={x.action === 'BUY_PROPOSAL' ? 'red' : 'green'}>{ACTION_LABELS[x.action] ?? x.action}</Tag>
                    <Text strong>{x.symbol}</Text>
                    <Text>{x.reason}</Text>
                    <Text type="secondary">{fmtMyt(x.as_of)}</Text>
                  </Space>
                </List.Item>
              )}
            />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" className="card-fill" title="为什么交易或没有交易" data-testid="why-card">
            {o.allow_new_entries.reasons.length ? (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message="当前不能开新仓的原因"
                description={<ul className="compact-list">{o.allow_new_entries.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
              />
            ) : null}
            <List
              size="small"
              header={<Text strong>今天没有交易的主要原因（按次数）</Text>}
              dataSource={o.why_no_trade}
              locale={{ emptyText: '暂无数据' }}
              renderItem={(x) => (
                <List.Item>
                  <Space>
                    <Tag>{x.count} 次</Tag>
                    <Text>{x.reason}</Text>
                  </Space>
                </List.Item>
              )}
            />
            <Button type="link" onClick={() => nav('/decisions')} style={{ paddingLeft: 0 }}>查看全部 AI 决策 →</Button>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="资金：三者分开显示，不能混为一谈" style={{ marginTop: 12 }}>
        <Row gutter={[12, 12]}>
          <Col xs={24} md={8}>
            <Card size="small" type="inner" title="① 账户总资产" className="card-fill">
              {o.account?.broker_total_assets ? (
                <Statistic value={fmtMoney(o.account.broker_total_assets)} />
              ) : (
                <div className="big-state" style={{ color: '#8c8c8c' }}>{ns === 'live' || p.mode === 'BROKER_PAPER' ? '尚未连接' : '不适用'}</div>
              )}
              <Text type="secondary">{o.account?.note ?? '暂无数据'}</Text>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" type="inner" title="② 系统获分配额度" className="card-fill">
              {o.auth?.capital && Object.keys(o.auth.capital).length ? (
                Object.entries(o.auth.capital).map(([ccy, v]) => (
                  <div key={ccy}>
                    <Text strong style={{ fontSize: 18 }}>{fmtMoney(v)}</Text> <Text type="secondary">{ccy}</Text>
                    {o.auth?.max_planned_loss?.[ccy] ? <Text type="secondary">　可承受计划亏损 {fmtMoney(o.auth.max_planned_loss[ccy])} {ccy}</Text> : null}
                  </div>
                ))
              ) : (
                <Text type="secondary">没有运行授权</Text>
              )}
              <div><Text type="secondary">AI 只能使用这部分资金，不能自行提高。</Text></div>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" type="inner" title="③ 系统管理的持仓" className="card-fill">
              <div className="big-state">{o.positions.length} 只</div>
              <Text type="secondary">只包括本系统买入并管理的持仓，不包括账户里的其他投资。</Text>
            </Card>
          </Col>
        </Row>
        <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
          {o.currencies.length ? (
            o.currencies.map((c) => (
              <Col xs={24} xl={12} key={c.currency}>
                <CurrencyCard c={c} />
              </Col>
            ))
          ) : (
            <Col span={24}><Empty description="暂无资金数据" /></Col>
          )}
        </Row>
        <div style={{ marginTop: 8 }}>
          {o.combined_myr ? (
            <Alert
              type="info"
              showIcon
              message={<span>折算合计（参考）：<Text strong>{fmtMoney(o.combined_myr.equity)} MYR</Text></span>}
              description={o.combined_myr.note}
            />
          ) : (
            <Text type="secondary">MYR 与 USD 分别计算，不直接相加（未录入汇率时不折算）。</Text>
          )}
        </div>
      </Card>

      <Card size="small" title={`③ 系统管理的持仓（${o.positions.length}）`} style={{ marginTop: 12 }} data-testid="positions-card">
        <PositionsTable items={o.positions} />
      </Card>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card size="small" className="card-fill" title={`风控锁定（${o.locks.length}）`} extra={<Button size="small" type="link" onClick={() => nav('/risk')}>管理锁定</Button>}>
            {o.locks.length ? (
              <List
                size="small"
                dataSource={o.locks}
                renderItem={(l) => (
                  <List.Item>
                    <Space direction="vertical" size={0}>
                      <Space wrap>
                        <Tag color="red">{LOCK_KIND_LABELS[l.kind] ?? l.kind}</Tag>
                        <Text>范围：{l.scope === 'ALL' ? '全部新开仓' : l.scope}</Text>
                        {l.requires_manual_review ? <Tag color="orange">需要人工复核</Tag> : null}
                      </Space>
                      <Text>{l.reason}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>{fmtMyt(l.as_of)}{l.release_not_before ? `；最早解除 ${fmtMyt(l.release_not_before)}` : ''}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            ) : (
              <Text type="secondary">没有生效中的锁定</Text>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card
            size="small"
            className="card-fill"
            title="最新日报"
            extra={o.latest_daily_report ? <DownloadLink href={`/api/${ns}/reviews/${o.latest_daily_report.id}.txt`}>下载 .txt</DownloadLink> : null}
          >
            {o.latest_daily_report ? <pre className="report-text">{o.latest_daily_report.summary}</pre> : <Text type="secondary">暂无日报（每个马来西亚交易日结束后自动生成）</Text>}
          </Card>
        </Col>
      </Row>

      <Card size="small" title="最近事件" style={{ marginTop: 12 }} extra={<Button size="small" type="link" onClick={() => nav('/logs')}>全部日志</Button>}>
        <List
          size="small"
          dataSource={o.recent_events ?? []}
          locale={{ emptyText: '暂无事件' }}
          renderItem={(e) => (
            <List.Item
              className={e.decision_id || e.intent_id ? 'clickable-row' : undefined}
              onClick={e.decision_id || e.intent_id ? () => openTrace({ decision_id: e.decision_id || undefined, intent_id: e.intent_id || undefined }) : undefined}
            >
              <Space wrap>
                <Text type="secondary">{fmtMyt(e.as_of || e.created_at_utc)}</Text>
                <Tag>{AUDIT_CATEGORY_LABELS[e.category] ?? e.category}</Tag>
                <Text>{e.message}</Text>
              </Space>
            </List.Item>
          )}
        />
      </Card>

      <Collapse
        style={{ marginTop: 12 }}
        className="danger-zone"
        items={[
          {
            key: 'liq',
            label: <Text type="secondary">高影响操作：手动清仓（与上面的暂停/紧急停止完全不同）</Text>,
            children: (
              <Space direction="vertical">
                <Explain type="warning">
                  手动清仓会按限价卖出本系统管理的全部持仓（不涉及账户里的其他投资）。会先显示范围与估算金额，需要勾选确认；实盘还需要再次输入密码。
                </Explain>
                <LiquidateFlow />
              </Space>
            ),
          },
        ]}
      />

      {o.auth?.summary_text ? (
        <Card size="small" title="当前运行授权摘要" style={{ marginTop: 12 }}>
          <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{o.auth.summary_text}</Paragraph>
        </Card>
      ) : null}
    </div>
  );
}
