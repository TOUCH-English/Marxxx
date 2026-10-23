import { Alert, Button, Card, Col, DatePicker, Descriptions, Empty, Input, Row, Select, Space, Spin, Switch, Tag, Typography } from 'antd';
import type { Dayjs } from 'dayjs';
import { useEffect, useState } from 'react';
import { api, qs } from '../api/client';
import type { Decision, DecisionFull, Intent, Items } from '../api/types';
import { ActionTag, ErrorAlert, Explain, OutcomeTag, PageTitle } from '../components/common';
import { ApproveButtons, conditionText, IntentTable, useDrawers } from '../components/Drawers';
import { useAppState } from '../state/app';
import { useApi } from '../state/hooks';
import { fmtMoney, fmtPrice } from '../utils/decimal';
import { fmtMyt } from '../utils/format';
import { DECISION_OUTCOME_LABELS, DECISION_SOURCE_LABELS, OPEN_ORDER_STATUSES } from '../utils/labels';

const { Text, Paragraph } = Typography;

const PAGE = 30;

/** 列表接口不含规则计算，按需取详情并缓存（只对买卖类决策）。 */
const calcCache = new Map<string, DecisionFull>();

function CalcSummary({ d }: { d: Decision }) {
  const { ns } = useAppState();
  const key = `${ns}:${d.id}`;
  const [full, setFull] = useState<DecisionFull | undefined>(calcCache.get(key));
  const [loading, setLoading] = useState(false);
  const relevant = d.action === 'BUY_PROPOSAL' || d.action === 'SELL_PROPOSAL';
  useEffect(() => {
    if (!relevant || full) return;
    let alive = true;
    setLoading(true);
    api
      .get<DecisionFull>(`/api/${ns}/decisions/${d.id}`)
      .then((r) => {
        if (!r.intents.some((i) => OPEN_ORDER_STATUSES.includes(i.status))) calcCache.set(key, r);
        if (alive) setFull(r);
      })
      .catch(() => undefined)
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [relevant, full, ns, d.id, key]);
  if (!relevant) return null;
  if (loading && !full) return <Spin size="small" />;
  const rc = full?.rule_calc;
  const intent = full?.intents?.[0];
  return (
    <Descriptions
      size="small"
      column={{ xs: 1, sm: 2, md: 4 }}
      items={[
        { key: 'q', label: '计算数量', children: rc?.qty !== undefined ? rc.qty : '—' },
        { key: 'p', label: '限价', children: rc?.limit_price ? fmtPrice(rc.limit_price) : '—' },
        {
          key: 'f',
          label: '预估费用',
          children: rc?.ok !== false && (rc?.est_fee_buy || rc?.est_fee_sell) ? `${d.action === 'BUY_PROPOSAL' ? '买 ' + fmtMoney(rc?.est_fee_buy) : '卖 ' + fmtMoney(rc?.est_fee_sell ?? rc?.est_fee_buy)} ${d.currency}` : '—',
        },
        {
          key: 'r',
          label: '执行结果',
          children: intent ? (
            <Space size={4} wrap>
              <Tag>{intent.status_label}</Tag>
              {intent.filled_qty ? <Text type="secondary">成交 {intent.filled_qty}{intent.avg_fill_price ? ' @ ' + fmtPrice(intent.avg_fill_price) : ''}</Text> : null}
            </Space>
          ) : rc && rc.ok === false ? (
            <Tag color="volcano">风控未通过</Tag>
          ) : (
            '未产生订单'
          ),
        },
      ]}
    />
  );
}

function DecisionCard({ d }: { d: Decision }) {
  const { openDecision } = useDrawers();
  const cond = conditionText(d as DecisionFull);
  const flags = d.proposal?.risk_flags ?? [];
  return (
    <Card
      size="small"
      hoverable
      onClick={() => openDecision(d.id)}
      title={
        <Space wrap>
          <Text strong>{d.symbol}</Text>
          <ActionTag action={d.action} />
          <OutcomeTag outcome={d.outcome} label={d.outcome_label} />
        </Space>
      }
      extra={<Text type="secondary" style={{ fontSize: 12 }}>数据时间 {fmtMyt(d.as_of)}</Text>}
      data-testid="decision-card"
    >
      <Paragraph style={{ marginBottom: 6 }} ellipsis={{ rows: 3 }}>
        <Text type="secondary">依据：</Text>
        {d.rationale || '—'}
      </Paragraph>
      <Paragraph style={{ marginBottom: 6 }}>
        <Text type="secondary">执行结果：</Text>
        {d.outcome_reason || '—'}
      </Paragraph>
      {d.action === 'BUY_PROPOSAL' || d.action === 'SELL_PROPOSAL' ? (
        <Paragraph style={{ marginBottom: 6 }}>
          <Text type="secondary">入场：</Text>
          {cond.entry}　<Text type="secondary">退出：</Text>
          {cond.exit}
        </Paragraph>
      ) : null}
      {flags.length ? (
        <Space wrap size={4} style={{ marginBottom: 6 }}>
          <Text type="secondary">风险：</Text>
          {flags.map((f) => (
            <Tag key={f} color="gold">{f}</Tag>
          ))}
        </Space>
      ) : null}
      <CalcSummary d={d} />
      <Space wrap size={4}>
        <Tag>{DECISION_SOURCE_LABELS[d.source] ?? d.source}</Tag>
        <Text type="secondary" style={{ fontSize: 12 }}>{d.strategy_version}</Text>
      </Space>
    </Card>
  );
}

export function PendingApprovals() {
  const { ns, overview } = useAppState();
  const { openOrder } = useDrawers();
  const pending = useApi<Items<Intent>>(ns === 'live' ? `/api/${ns}/orders?status=AWAITING_APPROVAL` : null);
  if (ns !== 'live') return null;
  const items = pending.data?.items ?? [];
  if (!items.length && overview?.portfolio?.mode !== 'LIVE_REVIEW') return null;
  return (
    <Card size="small" title={`待确认订单（仅「实盘逐笔确认」模式，${items.length}）`} style={{ marginBottom: 12 }}>
      <Explain>只有可选的 LIVE_REVIEW 模式需要逐笔确认；主流程 LIVE_AUTO 在授权范围内自动执行，不会出现在这里。</Explain>
      {items.length ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          <IntentTable items={items} onOpen={openOrder} />
          {items.map((i) => (
            <Space key={i.id} wrap>
              <Text>{i.symbol} {i.side === 'BUY' ? '买入' : '卖出'} {i.qty} @ {fmtPrice(i.limit_price)}</Text>
              <ApproveButtons id={i.id} onDone={() => void pending.reload()} />
            </Space>
          ))}
        </Space>
      ) : (
        <Text type="secondary">暂无待确认订单</Text>
      )}
    </Card>
  );
}

export function DecisionsPage() {
  const { ns } = useAppState();
  const [symbol, setSymbol] = useState('');
  const [outcome, setOutcome] = useState<string | undefined>();
  const [source, setSource] = useState<string | undefined>();
  const [day, setDay] = useState<Dayjs | null>(null);
  const [routine, setRoutine] = useState(false);
  const [pages, setPages] = useState(1);
  const path = `/api/${ns}/decisions${qs({
    symbol: symbol.trim().toUpperCase() || undefined,
    outcome,
    source,
    day: day ? day.format('YYYY-MM-DD') : undefined,
    include_routine: routine ? 'true' : undefined,
    limit: PAGE * pages,
  })}`;
  const list = useApi<Items<Decision>>(path);
  const items = list.data?.items ?? [];

  return (
    <div>
      <PageTitle
        title="AI 分析与决策"
        sub="每条决策都分开显示：事实（数据）、规则计算（程序风控）和模型判断。自动模式下合格决定直接执行，没有逐笔批准按钮。"
        extra={
          <>
            <a href={`/api/${ns}/export/decisions.csv`} download>导出决策 CSV</a>
          </>
        }
      />
      <PendingApprovals />
      <Card size="small" style={{ marginBottom: 12 }}>
        <Row gutter={[8, 8]} align="middle">
          <Col xs={12} md={5}>
            <Input allowClear placeholder="证券代码，如 MY.D002" value={symbol} onChange={(e) => { setSymbol(e.target.value); setPages(1); }} />
          </Col>
          <Col xs={12} md={5}>
            <Select
              allowClear
              placeholder="结果"
              style={{ width: '100%' }}
              value={outcome}
              onChange={(v) => { setOutcome(v); setPages(1); }}
              options={Object.entries(DECISION_OUTCOME_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Col>
          <Col xs={12} md={5}>
            <Select
              allowClear
              placeholder="来源"
              style={{ width: '100%' }}
              value={source}
              onChange={(v) => { setSource(v); setPages(1); }}
              options={['AI_MODEL', 'AI_FIXTURE', 'PROTECTION', 'BASELINE_MA'].map((value) => ({ value, label: DECISION_SOURCE_LABELS[value] }))}
            />
          </Col>
          <Col xs={12} md={4}>
            <DatePicker style={{ width: '100%' }} placeholder="日期（马来西亚）" value={day} onChange={(v) => { setDay(v); setPages(1); }} />
          </Col>
          <Col xs={24} md={5}>
            <Space>
              <Switch checked={routine} onChange={(v) => { setRoutine(v); setPages(1); }} />
              <Text>包含例行「无需交易」</Text>
            </Space>
          </Col>
        </Row>
      </Card>
      <ErrorAlert error={list.error} />
      {!routine && !outcome ? (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message="默认只显示买卖类决策与非例行结果；打开「包含例行无需交易」可查看 AI 每次扫描的全部结论。" />
      ) : null}
      {list.loading && !list.data ? <Card loading /> : null}
      {list.data && !items.length ? <Empty description="没有符合条件的决策（运行开始后，AI 会按计划自动扫描授权范围）" /> : null}
      <Row gutter={[12, 12]}>
        {items.map((d) => (
          <Col xs={24} xl={12} key={d.id}>
            <DecisionCard d={d} />
          </Col>
        ))}
      </Row>
      {items.length >= PAGE * pages ? (
        <div style={{ textAlign: 'center', marginTop: 12 }}>
          <Button onClick={() => setPages(pages + 1)} loading={list.loading}>加载更多</Button>
        </div>
      ) : null}
    </div>
  );
}
