import { Alert, Button, Card, Input, Select, Space, Table, Tabs, Tag, Typography } from 'antd';
import { useState } from 'react';
import { qs } from '../api/client';
import type { FillRow, Intent, Items, PositionRow } from '../api/types';
import { DownloadLink, ErrorAlert, Explain, Money, OrderStatusTag, PageTitle, SideTag } from '../components/common';
import { FillsTable, TraceView, useDrawers } from '../components/Drawers';
import { useAppState } from '../state/app';
import { useApi } from '../state/hooks';
import { fmtPrice } from '../utils/decimal';
import { fmtMyt, shortId } from '../utils/format';
import { INTENT_REASON_LABELS, MODE_LABELS, ORDER_STATUS_LABELS } from '../utils/labels';
import { PendingApprovals } from './Decisions';

const { Text } = Typography;

function OrdersTab() {
  const { ns } = useAppState();
  const { openOrder } = useDrawers();
  const [status, setStatus] = useState<string[]>([]);
  const [symbol, setSymbol] = useState('');
  const list = useApi<Items<Intent>>(`/api/${ns}/orders${qs({ status: status.join(',') || undefined, symbol: symbol.trim().toUpperCase() || undefined })}`);
  const unknown = (list.data?.items ?? []).filter((o) => o.status === 'UNKNOWN').length;
  return (
    <>
      <Explain>
        订单状态分开显示：已建立、待确认、提交中、已提交（券商已接收）、部分成交、全部成交、撤单中、已撤销、被拒绝、状态未知。<Text strong>「已提交」不等于买入成功</Text>，以成交为准。
      </Explain>
      {unknown ? <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`有 ${unknown} 笔订单状态未知，需要人工核对（点击订单 → 人工核对）。核对前相关标的会被锁定。`} /> : null}
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          mode="multiple"
          allowClear
          placeholder="按状态筛选"
          style={{ minWidth: 260 }}
          value={status}
          onChange={setStatus}
          options={Object.entries(ORDER_STATUS_LABELS).map(([value, label]) => ({ value, label }))}
          data-testid="order-status-filter"
        />
        <Input allowClear placeholder="证券代码" value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ width: 160 }} />
        <DownloadLink href={`/api/${ns}/export/orders.csv`}>导出订单 CSV</DownloadLink>
      </Space>
      <ErrorAlert error={list.error} />
      <Table<Intent>
        size="small"
        rowKey="id"
        loading={list.loading && !list.data}
        dataSource={list.data?.items ?? []}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        scroll={{ x: 1100 }}
        onRow={(r) => ({ onClick: () => openOrder(r.id), className: 'clickable-row' })}
        locale={{ emptyText: '暂无订单' }}
        columns={[
          { title: '业务时间', dataIndex: 'as_of', render: (v: string | null, r) => fmtMyt(v || r.created_at_utc) },
          { title: '证券', dataIndex: 'symbol' },
          { title: '方向', dataIndex: 'side', render: (s: string) => <SideTag side={s} /> },
          { title: '原因', dataIndex: 'reason', render: (r: string) => INTENT_REASON_LABELS[r] ?? r },
          { title: '数量', dataIndex: 'qty' },
          { title: '限价', dataIndex: 'limit_price', render: (v: string | null) => fmtPrice(v) },
          { title: '状态', render: (_, r) => <Space size={2}><OrderStatusTag status={r.status} label={r.status_label} />{r.needs_manual_review ? <Tag color="magenta">需核对</Tag> : null}</Space> },
          { title: '已成交', render: (_, r) => `${r.filled_qty}/${r.qty}${r.avg_fill_price ? ' @ ' + fmtPrice(r.avg_fill_price) : ''}` },
          { title: '费用（实际/预估）', render: (_, r) => <span><Money v={r.fees} /> / <Money v={r.est_fees} /> <span className="ccy">{r.currency}</span></span> },
          { title: '券商订单号', dataIndex: 'broker_order_id', render: (v: string | null) => v || <Text type="secondary">—</Text> },
          { title: '模式', dataIndex: 'mode', render: (m: string) => MODE_LABELS[m] ?? m },
        ]}
      />
    </>
  );
}

function PositionsTab() {
  const { ns } = useAppState();
  const { openDecision } = useDrawers();
  const [status, setStatus] = useState('ALL');
  const list = useApi<Items<PositionRow>>(`/api/${ns}/positions?status=${status}`);
  return (
    <>
      <Explain>这里只列出本系统管理的持仓（不包括账户中的其他投资）。未平仓的浮动盈亏不计入已实现盈亏。</Explain>
      <Space style={{ marginBottom: 12 }}>
        <Select value={status} onChange={setStatus} style={{ width: 160 }} options={[{ value: 'ALL', label: '全部' }, { value: 'OPEN', label: '持有中' }, { value: 'CLOSED', label: '已平仓' }]} />
      </Space>
      <ErrorAlert error={list.error} />
      <Table<PositionRow>
        size="small"
        rowKey="id"
        dataSource={list.data?.items ?? []}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        scroll={{ x: 1200 }}
        locale={{ emptyText: '暂无持仓记录' }}
        columns={[
          { title: '证券', dataIndex: 'symbol' },
          { title: '状态', dataIndex: 'status', render: (s: string) => (s === 'OPEN' ? <Tag color="green">持有中</Tag> : <Tag>已平仓</Tag>) },
          { title: '数量（最高）', render: (_, p) => `${p.qty}（${p.max_qty}）` },
          { title: '成本价', dataIndex: 'avg_cost', render: (v: string) => fmtPrice(v) },
          { title: '已实现盈亏', render: (_, p) => <Money v={p.realized_pnl} ccy={p.currency} pnl /> },
          { title: '已付费用', render: (_, p) => <Money v={p.fees_paid} ccy={p.currency} /> },
          { title: '止损 / 止盈', render: (_, p) => `${fmtPrice(p.stop_price)} / ${fmtPrice(p.take_profit_price)}` },
          { title: '最长持有至', dataIndex: 'max_hold_until', render: (v: string | null) => fmtMyt(v) },
          { title: '开仓', dataIndex: 'opened_at', render: (v: string | null) => fmtMyt(v) },
          { title: '平仓', dataIndex: 'closed_at', render: (v: string | null) => (v ? fmtMyt(v) : '—') },
          { title: '保护规则版本', dataIndex: 'protection_version' },
          {
            title: '开仓决策',
            dataIndex: 'entry_decision_id',
            render: (v: string | null) => (v ? <a onClick={() => openDecision(v)}>{shortId(v)}</a> : '—'),
          },
        ]}
      />
    </>
  );
}

function FillsTab() {
  const { ns } = useAppState();
  const list = useApi<Items<FillRow>>(`/api/${ns}/fills`);
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <DownloadLink href={`/api/${ns}/export/fills.csv`}>导出成交 CSV</DownloadLink>
      </Space>
      <ErrorAlert error={list.error} />
      <FillsTable items={list.data?.items ?? []} />
    </>
  );
}

function TraceTab() {
  const [decisionId, setDecisionId] = useState('');
  const [intentId, setIntentId] = useState('');
  const [key, setKey] = useState<{ decision_id?: string; intent_id?: string } | null>(null);
  return (
    <>
      <Explain>输入决策 ID 或订单 ID，查看完整因果链：决策 → 风控 → 订单意图 → 券商事件 → 成交 → 持仓。也可以在任何列表中点击决策或订单直接打开。</Explain>
      <Space wrap style={{ marginBottom: 12 }}>
        <Input placeholder="决策 ID" value={decisionId} onChange={(e) => setDecisionId(e.target.value)} style={{ width: 300 }} />
        <Input placeholder="订单 ID" value={intentId} onChange={(e) => setIntentId(e.target.value)} style={{ width: 300 }} />
        <Button
          type="primary"
          disabled={!decisionId.trim() && !intentId.trim()}
          onClick={() => setKey({ decision_id: decisionId.trim() || undefined, intent_id: intentId.trim() || undefined })}
        >
          查询因果链
        </Button>
      </Space>
      {key ? <Card size="small"><TraceView key={`${key.decision_id}-${key.intent_id}`} k={key} /></Card> : null}
    </>
  );
}

export function OrdersPage() {
  return (
    <div>
      <PageTitle title="订单与持仓" sub="点击订单查看事件时间线、成交明细、费用和券商订单号；状态未知的订单在详情中进行人工核对。" />
      <PendingApprovals />
      <Tabs
        destroyOnHidden
        items={[
          { key: 'orders', label: '订单', children: <OrdersTab /> },
          { key: 'positions', label: '持仓', children: <PositionsTab /> },
          { key: 'fills', label: '成交', children: <FillsTab /> },
          { key: 'trace', label: '因果链查询', children: <TraceTab /> },
        ]}
      />
    </div>
  );
}
