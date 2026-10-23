import { Alert, Button, Card, Col, Collapse, DatePicker, Descriptions, Empty, Form, InputNumber, Radio, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { useMemo, useState } from 'react';
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api/client';
import type { BacktestFull, BacktestMetrics, BacktestSummary, Items, ReviewRun } from '../api/types';
import { DownloadLink, ErrorAlert, Explain, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { chartNumber, fmtMoney, fmtPct, fmtPrice, signOf } from '../utils/decimal';
import { fmtMyt, fmtMytDate, fmtValue } from '../utils/format';
import { DATA_SOURCE_LABELS } from '../utils/labels';

const { Text, Paragraph } = Typography;
const { RangePicker } = DatePicker;

interface BtForm {
  strategy_key: 'BASELINE_MA' | 'AI_AUTONOMOUS';
  range: [Dayjs, Dayjs];
  cap_MYR?: string;
  cap_USD?: string;
  slippage_bps: number;
  entry_premium_bps: number;
  data_source: string;
}

function PctStat({ title, v }: { title: string; v: string | undefined }) {
  const s = signOf(v);
  return <Statistic title={title} value={v !== undefined ? fmtPct(v, true) : '暂无数据'} valueStyle={{ color: s > 0 ? '#389e0d' : s < 0 ? '#cf1322' : undefined, fontSize: 20 }} />;
}

function MetricsView({ m }: { m: BacktestMetrics }) {
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap>
        <Tag color="volcano" className="sim-badge">模拟报告</Tag>
        <Text strong>{m.label}</Text>
        <Tag>{DATA_SOURCE_LABELS[m.data_source ?? ''] ?? m.data_source}</Tag>
      </Space>
      {m.sample_warning ? <Alert type="warning" showIcon message={`样本提示：${m.sample_warning}`} /> : null}
      {m.leakage_note ? <Alert type="error" showIcon message="数据泄漏提示" description={m.leakage_note} /> : null}
      <Row gutter={[12, 12]}>
        {Object.entries(m.per_currency ?? {}).map(([ccy, v]) => (
          <Col xs={24} md={12} key={ccy}>
            <Card size="small" title={`${ccy} 账本（分开计算，不相加）`}>
              <Row gutter={8}>
                <Col span={12}><Statistic title="初始资金" value={fmtMoney(v.initial)} suffix={ccy} valueStyle={{ fontSize: 18 }} /></Col>
                <Col span={12}><Statistic title="期末净值" value={fmtMoney(v.final_equity)} suffix={ccy} valueStyle={{ fontSize: 18 }} /></Col>
                <Col span={12}><PctStat title="净收益（已扣费用）" v={v.net_return_pct} /></Col>
                <Col span={12}><Statistic title="最大回撤" value={fmtPct(v.max_drawdown_pct)} valueStyle={{ fontSize: 20 }} /></Col>
                <Col span={12}><PctStat title="基准：等权买入持有" v={v.benchmark_buy_hold_equal_weight_pct} /></Col>
                <Col span={12}><PctStat title="基准：持有现金" v={v.benchmark_cash_pct} /></Col>
              </Row>
              <Descriptions
                size="small"
                column={2}
                style={{ marginTop: 8 }}
                items={[
                  { key: 'rt', label: '完成交易', children: v.round_trips ?? '—' },
                  { key: 'pnl', label: '已实现盈亏', children: v.net_pnl ? `${fmtMoney(v.net_pnl)} ${ccy}` : '—' },
                  { key: 'pr', label: '盈亏比', children: v.payoff_ratio ?? '—' },
                  { key: 'pf', label: '盈利因子', children: v.profit_factor ?? '—' },
                  { key: 'fee', label: '费用', children: v.fees ? `${fmtMoney(v.fees)} ${ccy}` : '—' },
                  {
                    key: 'fs',
                    label: '费用占毛盈亏',
                    children: m.fee_share_of_gross_pct?.[ccy] !== undefined ? fmtPct(m.fee_share_of_gross_pct[ccy]) : '—',
                  },
                ]}
              />
            </Card>
          </Col>
        ))}
      </Row>
      <Descriptions
        size="small"
        bordered
        column={{ xs: 1, sm: 2, md: 3 }}
        items={[
          { key: 'rt', label: '完成交易（全部币种，笔数）', children: m.round_trips ?? '—' },
          { key: 'b', label: '买入次数', children: m.buys ?? '—' },
          { key: 'wr', label: '胜率', children: m.win_rate_pct !== undefined ? fmtPct(m.win_rate_pct) : '—' },
          { key: 'ah', label: '平均持有天数', children: m.avg_holding_days ?? '—' },
          { key: 'sd', label: '样本交易日', children: m.sample_days ?? '—' },
          { key: 'op', label: '期末未平仓', children: m.open_positions_at_end ?? '—' },
        ]}
      />
      {m.assumptions?.length ? (
        <Collapse
          items={[
            {
              key: 'a',
              label: '撮合与数据假设（请阅读）',
              children: (
                <ul className="compact-list">
                  {m.assumptions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              ),
            },
          ]}
        />
      ) : null}
    </Space>
  );
}

function EquityCharts({ curve }: { curve: Record<string, string>[] }) {
  const ccys = useMemo(() => {
    const s = new Set<string>();
    for (const r of curve.slice(0, 5)) for (const k of Object.keys(r)) if (k !== 'day' && k !== '标记') s.add(k);
    return [...s];
  }, [curve]);
  if (!curve.length) return <Empty description="没有净值曲线" />;
  return (
    <Row gutter={[12, 12]}>
      {ccys.map((c, i) => (
        <Col xs={24} lg={12} key={c}>
          <Text strong>{c} 净值曲线</Text>
          <div className="chart-box">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={curve.map((r) => ({ day: r.day, v: chartNumber(r[c]) }))} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="day" tick={{ fontSize: 11 }} minTickGap={40} />
                <YAxis domain={['auto', 'auto']} tick={{ fontSize: 11 }} width={64} />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="v" name={`${c} 净值`} stroke={i === 0 ? '#1677ff' : '#722ed1'} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Col>
      ))}
    </Row>
  );
}

function BacktestDetail({ id }: { id: number }) {
  const bt = useApi<BacktestFull>(`/api/sim/backtests/${id}`, { live: false });
  if (bt.error) return <ErrorAlert error={bt.error} />;
  if (!bt.data) return <Card loading />;
  const r = bt.data;
  const p = r.params as Record<string, unknown>;
  const tradeCols = r.trades.length ? Object.keys(r.trades[0]) : [];
  const tradeLabels: Record<string, string> = { symbol: '证券', side: '方向', day: '日期', qty: '数量', price: '价格', fee: '费用', pnl: '盈亏', reason: '原因', holding_days: '持有天数', currency: '币种' };
  return (
    <Card
      size="small"
      title={`回测 #${r.id} 详情`}
      extra={
        <Space wrap>
          <DownloadLink href={`/api/sim/backtests/${r.id}/trades.csv`}>交易 CSV</DownloadLink>
          <DownloadLink href={`/api/sim/backtests/${r.id}/equity.csv`}>净值 CSV</DownloadLink>
          <DownloadLink href={`/api/sim/backtests/${r.id}/report.txt`}>中文复盘 .txt</DownloadLink>
        </Space>
      }
      data-testid="backtest-detail"
    >
      <Paragraph type="secondary">
        样本 {String(p.start)} 至 {String(p.end)}；滑点 {String(p.slippage_bps)} 基点；入场溢价上限 {String(p.entry_premium_bps)} 基点；初始资金 {fmtValue(p.capital)}。
      </Paragraph>
      <MetricsView m={r.result} />
      <div style={{ marginTop: 16 }}>
        <EquityCharts curve={r.equity_curve} />
      </div>
      <Card size="small" type="inner" title={`交易明细（${r.trades.length}）`} style={{ marginTop: 12 }}>
        <Table
          size="small"
          rowKey={(_, i) => String(i)}
          dataSource={r.trades}
          pagination={{ pageSize: 15, hideOnSinglePage: true }}
          scroll={{ x: true }}
          columns={tradeCols.map((k) => ({
            title: tradeLabels[k] ?? k,
            dataIndex: k,
            render: (v: unknown) => (k === 'side' ? (v === 'BUY' ? '买入' : v === 'SELL' ? '卖出' : String(v)) : k === 'price' ? fmtPrice(v as string) : v === '' || v === null ? '—' : String(v)),
          }))}
        />
      </Card>
    </Card>
  );
}

function BacktestSection() {
  const { run, busy } = useAction();
  const list = useApi<Items<BacktestSummary>>('/api/sim/backtests', { live: false });
  const [selected, setSelected] = useState<number | null>(null);
  const [form] = Form.useForm<BtForm>();

  const submit = async () => {
    const v = await form.validateFields();
    const capital: Record<string, string> = {};
    if (v.cap_MYR) capital.MYR = String(v.cap_MYR);
    if (v.cap_USD) capital.USD = String(v.cap_USD);
    const r = await run(
      () =>
        api.post<{ id: number }>('/api/sim/backtests', {
          strategy_key: v.strategy_key,
          start: v.range[0].format('YYYY-MM-DD'),
          end: v.range[1].format('YYYY-MM-DD'),
          capital,
          slippage_bps: v.slippage_bps,
          entry_premium_bps: v.entry_premium_bps,
          data_source: v.data_source,
        }),
      { success: (x) => `回测 #${x.id} 完成` },
    );
    if (r) {
      await list.reload();
      setSelected(r.id);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card size="small" title="运行回测">
        <Explain type="warning">
          回测用历史日K模拟撮合，结果会受数据、费用与撮合假设影响，<Text strong>不代表真实收益</Text>。「AI 策略」回测使用固定规则模拟的 AI 输出（不调用真实模型），而真实模型可能已知历史事件，历史回放不能作为无泄漏的收益证明。
        </Explain>
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            strategy_key: 'BASELINE_MA',
            range: [dayjs('2025-06-02'), dayjs('2026-05-29')],
            cap_MYR: '20000',
            cap_USD: '5000',
            slippage_bps: 10,
            entry_premium_bps: 30,
            data_source: 'DEMO_FIXTURE',
          }}
        >
          <Row gutter={12}>
            <Col xs={24} md={12}>
              <Form.Item name="strategy_key" label="策略">
                <Radio.Group>
                  <Radio value="BASELINE_MA">固定规则基准（MA20/MA60）</Radio>
                  <Radio value="AI_AUTONOMOUS">AI 自主决策（固定规则模拟的 AI 输出）</Radio>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="range" label="样本时间" rules={[{ required: true, message: '请选择样本时间' }]}>
                <RangePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="cap_MYR" label="初始资金 MYR">
                <InputNumber<string> stringMode min="0" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="cap_USD" label="初始资金 USD">
                <InputNumber<string> stringMode min="0" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="slippage_bps" label="滑点（基点）" tooltip="1 基点 = 0.01%">
                <InputNumber min={0} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="entry_premium_bps" label="入场溢价上限（基点）">
                <InputNumber min={0} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="data_source" label="数据">
                <Select options={[{ value: 'DEMO_FIXTURE', label: '演示数据' }, { value: 'HISTORICAL_REPLAY', label: '导入的历史数据' }]} />
              </Form.Item>
            </Col>
          </Row>
          <Text type="secondary">费用使用「日志与设置 → 费用」中当前生效的费用表（测试费用表并非官方费率）。</Text>
          <div style={{ marginTop: 8 }}>
            <Button type="primary" loading={busy} onClick={() => void submit()} data-testid="run-backtest">
              运行回测
            </Button>
          </div>
        </Form>
      </Card>
      <Card size="small" title="回测记录">
        <ErrorAlert error={list.error} />
        <Table<BacktestSummary>
          size="small"
          rowKey="id"
          dataSource={list.data?.items ?? []}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: true }}
          onRow={(r) => ({ onClick: () => setSelected(r.id), className: 'clickable-row' })}
          rowClassName={(r) => (r.id === selected ? 'ant-table-row-selected' : '')}
          locale={{ emptyText: '还没有回测记录' }}
          columns={[
            { title: '#', dataIndex: 'id' },
            { title: '标记', render: (_, r) => <Tag color="volcano">{r.result.label ?? '模拟回测'}</Tag> },
            { title: '样本', render: (_, r) => `${String(r.params.start)} ~ ${String(r.params.end)}` },
            {
              title: '净收益',
              render: (_, r) =>
                Object.entries(r.result.per_currency ?? {}).map(([c, v]) => (
                  <Tag key={c} color={signOf(v.net_return_pct) >= 0 ? 'green' : 'red'}>
                    {c} {fmtPct(v.net_return_pct, true)}
                  </Tag>
                )),
            },
            { title: '交易', render: (_, r) => r.result.round_trips ?? '—' },
            { title: '样本提示', render: (_, r) => <Text type="secondary">{r.result.sample_warning}</Text> },
            { title: '时间', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
          ]}
        />
      </Card>
      {selected ? <BacktestDetail key={selected} id={selected} /> : null}
    </Space>
  );
}

function ReportsSection() {
  const { ns } = useAppState();
  const reports = useApi<Items<ReviewRun>>(`/api/${ns}/reviews?kind=DAILY`);
  const all = reports.data?.items ?? [];
  const [shown, setShown] = useState(10);
  const items = all.slice(0, shown);
  return (
    <Card size="small" title={`每日复盘（${all.length}）`} data-testid="daily-reports">
      <Paragraph type="secondary">每个马来西亚交易日结束后自动生成；未平仓的浮动盈亏不计入已实现盈利；模拟、回放与实盘分开统计。</Paragraph>
      <ErrorAlert error={reports.error} />
      {items.length ? (
        <Collapse
          defaultActiveKey={all[0] ? [all[0].id] : []}
          items={items.map((r) => ({
            key: r.id,
            label: (
              <Space wrap>
                <Text strong>{fmtMytDate(r.period_start)}</Text>
                <Tag>{DATA_SOURCE_LABELS[r.data_source] ?? r.data_source}</Tag>
                <Tag color={r.status === 'DONE' ? 'green' : 'default'}>{r.status === 'DONE' ? '已完成' : r.status}</Tag>
              </Space>
            ),
            extra: <DownloadLink href={`/api/${ns}/reviews/${r.id}.txt`}>.txt</DownloadLink>,
            children: <pre className="report-text">{r.summary}</pre>,
          }))}
        />
      ) : (
        <Empty description="暂无日报" />
      )}
      {all.length > shown ? (
        <div style={{ textAlign: 'center', marginTop: 8 }}>
          <Button onClick={() => setShown(shown + 20)}>显示更早的日报（还有 {all.length - shown} 份）</Button>
        </div>
      ) : null}
    </Card>
  );
}

export function SimPage() {
  const { ns } = useAppState();
  return (
    <div>
      <PageTitle
        title={
          <Space>
            模拟、回测和复盘 <Tag color="volcano" className="sim-badge">模拟报告</Tag>
          </Space>
        }
        sub="本页所有结果都是模拟或复盘，不代表真实交易收益，也不构成投资建议。"
      />
      <Card size="small" title="导出数据（CSV，可用 Excel 打开）" style={{ marginBottom: 12 }}>
        <Space wrap size="large">
          <DownloadLink href={`/api/${ns}/export/decisions.csv`}>决策</DownloadLink>
          <DownloadLink href={`/api/${ns}/export/orders.csv`}>订单</DownloadLink>
          <DownloadLink href={`/api/${ns}/export/fills.csv`}>成交</DownloadLink>
          <DownloadLink href={`/api/${ns}/export/audit.csv`}>审计日志</DownloadLink>
        </Space>
        <div><Text type="secondary">当前导出的是「{ns === 'live' ? '实盘' : '模拟'}」数据；每行带「数据性质」标记。</Text></div>
      </Card>
      {ns === 'sim' ? (
        <BacktestSection />
      ) : (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message="回测属于模拟功能" description="请切换到「自动模拟」使用回测。这里显示实盘的每日复盘与导出。" />
      )}
      <div style={{ marginTop: 12 }}>
        <ReportsSection />
      </div>
    </div>
  );
}

