import { CheckCircleFilled, ExclamationCircleFilled, InfoCircleFilled, PlayCircleOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Col, Empty, List, Progress, Row, Space, Tag, Typography } from 'antd';
import { useNavigate } from 'react-router-dom';
import type { Decision, Items, Overview, Scorecard } from '../api/types';
import { Money, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useApi } from '../state/hooks';
import { chartNumber, fmtPrice } from '../utils/decimal';
import { fmtMyt, fmtMytDate } from '../utils/format';

const { Text, Paragraph, Title } = Typography;

const NEED_TRADES = 30;

type Todo = { level: 'ok' | 'warn' | 'bad'; text: string };

/** 用户现在需要做什么：大多数时候「什么都不用做」。 */
function todos(o: Overview): Todo[] {
  const out: Todo[] = [];
  if (o.worker && !o.worker.alive) {
    out.push({ level: 'bad', text: '后台程序没有在运行：请确认 Mac 开着、没有睡眠，moomoo OpenD 已登录。' });
  }
  if (o.auth) {
    const days = (Date.parse(o.auth.expires_at) - Date.now()) / 86_400_000;
    if (!o.auth.ok) out.push({ level: 'bad', text: `运行授权已失效（${o.auth.reason}）：系统不会再买入新股票，已有持仓仍受保护。请续期授权。` });
    else if (days <= 3) out.push({ level: 'warn', text: `运行授权将在 ${fmtMyt(o.auth.expires_at)} 到期，请在这之前续期（可以请 Codex 帮忙）。` });
  }
  for (const lk of o.locks ?? []) {
    out.push({ level: 'warn', text: `系统触发了安全暂停：${lk.reason}` });
  }
  if (!out.length) out.push({ level: 'ok', text: '目前不需要您做任何事。系统会自动买卖，止损保护一直开着。' });
  return out;
}

function TodoIcon({ level }: { level: Todo['level'] }) {
  if (level === 'ok') return <CheckCircleFilled style={{ color: '#52c41a' }} />;
  if (level === 'warn') return <ExclamationCircleFilled style={{ color: '#faad14' }} />;
  return <ExclamationCircleFilled style={{ color: '#ff4d4f' }} />;
}

function pctOf(part: string | null | undefined, whole: string | null | undefined): string | null {
  const a = chartNumber(part);
  const b = chartNumber(whole);
  if (a === null || b === null || b === 0) return null;
  const p = (a / b) * 100;
  return `${p > 0 ? '+' : ''}${p.toFixed(2)}%`;
}

function plainAction(d: Decision): string {
  const who = d.source === 'PROTECTION' ? '程序自动保护' : d.source.startsWith('AI') ? 'AI' : '系统';
  const what = d.action === 'BUY_PROPOSAL' ? '买入' : d.action === 'SELL_PROPOSAL' ? '卖出' : '处理';
  return `${who} ${what} ${d.symbol.replace(/^US\./, '')}`;
}

export function SimplePage() {
  const { ns, overview: o, overviewError } = useAppState();
  const nav = useNavigate();
  const sc = useApi<Scorecard>(o?.portfolio ? `/api/${ns}/scorecard` : null, { live: false });
  const recent = useApi<Items<Decision>>(o?.portfolio ? `/api/${ns}/decisions?outcome=AUTO_EXECUTED&limit=8` : null);

  if (!o) {
    return (
      <>
        <PageTitle title="我的账户（简单版）" />
        {overviewError ? <Alert type="error" showIcon message="暂时读不到数据" description={overviewError} /> : <Card loading />}
      </>
    );
  }
  const p = o.portfolio;
  const isSim = ns === 'sim';

  if (!p) {
    return (
      <>
        <PageTitle title="我的账户（简单版）" />
        <Card>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text strong style={{ fontSize: 16 }}>还没有开始运行</Text>}
          >
            <Button type="primary" size="large" icon={<PlayCircleOutlined />} onClick={() => nav('/wizard')} data-testid="go-wizard">
              打开启动向导
            </Button>
          </Empty>
        </Card>
      </>
    );
  }

  const running = p.running && !p.emergency_stop && !(o.worker && !o.worker.alive);
  const headline = !running
    ? { color: '#cf1322', text: '系统现在没有在自动交易', sub: '请看下面「您需要做什么」。' }
    : p.pause_new_entries || !o.allow_new_entries.ok
      ? { color: '#d46b08', text: '系统在运行，但暂时不买新股票', sub: o.allow_new_entries.reasons[0] ?? '已暂停新开仓；已有持仓的止损保护继续运行。' }
      : { color: '#389e0d', text: '系统正在自动帮您操作', sub: '有合适机会时会自动买入，没有机会时不交易也是正常的。' };

  const usd = o.currencies.find((c) => c.currency === 'USD') ?? o.currencies[0];
  const n = sc.data?.numbers;
  const closed = Number(n?.closed_trades ?? 0);
  const todoList = todos(o);
  const noTrade = o.why_no_trade?.[0]?.reason;

  return (
    <div data-testid="simple-page">
      <PageTitle title="我的账户（简单版）" sub="用最简单的话告诉您：现在怎么样、赚了还是亏了、需不需要您做什么。" />

      <Card size="small">
        <Space align="start" size={16} wrap>
          <div>
            <Title level={3} style={{ color: headline.color, margin: 0 }} data-testid="simple-headline">{headline.text}</Title>
            <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>{headline.sub}</Paragraph>
          </div>
          <Tag color={isSim ? 'blue' : 'red'} style={{ fontSize: 14, padding: '2px 10px' }}>
            {isSim ? '模拟资金（不是真钱）' : '真实资金'}
          </Tag>
        </Space>
      </Card>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title="您的钱" className="card-fill" data-testid="simple-money">
            {usd ? (
              <>
                <Paragraph style={{ fontSize: 16, marginBottom: 4 }}>
                  开始时 <Money v={usd.allocation} ccy={usd.currency} />，现在 <Text strong style={{ fontSize: 20 }}><Money v={usd.equity} ccy={usd.currency} /></Text>
                </Paragraph>
                <Paragraph style={{ fontSize: 16, marginBottom: 4 }}>
                  目前盈亏{' '}
                  <Money v={usd.total_pnl} ccy={usd.currency} pnl />
                  {pctOf(usd.total_pnl, usd.allocation) ? <Text type="secondary">（{pctOf(usd.total_pnl, usd.allocation)}）</Text> : null}
                  <Text type="secondary">，已扣手续费 <Money v={usd.fees} ccy={usd.currency} /></Text>
                </Paragraph>
                {n?.spy_return_pct && n.spy_return_pct !== '—' ? (
                  <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                    对比：同一段时间如果把钱全部买大盘指数（SPY），是 {String(n.spy_return_pct)}。
                  </Paragraph>
                ) : null}
              </>
            ) : (
              <Text type="secondary">暂无数据</Text>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="您需要做什么" className="card-fill" data-testid="simple-todo">
            <List
              size="small"
              split={false}
              dataSource={todoList}
              renderItem={(t) => (
                <List.Item style={{ paddingLeft: 0 }}>
                  <Space align="start"><TodoIcon level={t.level} /><Text style={{ fontSize: 15 }}>{t.text}</Text></Space>
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="现在持有的股票" style={{ marginTop: 12 }} data-testid="simple-positions">
        {o.positions.length ? (
          <List
            size="small"
            dataSource={o.positions}
            renderItem={(pos) => (
              <List.Item style={{ paddingLeft: 0 }}>
                <div>
                  <Text strong style={{ fontSize: 15 }}>{pos.symbol.replace(/^US\./, '')}</Text>
                  {pos.name ? <Text type="secondary">（{pos.name}）</Text> : null}
                  <Text> {pos.qty} 股：</Text>
                  {pos.unrealized ? (
                    <Text>
                      目前盈亏 <Money v={pos.unrealized} ccy={pos.currency} pnl />
                    </Text>
                  ) : (
                    <Text type="secondary">暂时没有最新价</Text>
                  )}
                  <div>
                    <Text type="secondary">
                      {pos.stop_price ? `跌到 ${fmtPrice(pos.stop_price)} 会自动卖出（止损，控制亏损）` : '没有设止损'}
                      {pos.take_profit_price ? `；涨到 ${fmtPrice(pos.take_profit_price)} 会自动卖出（获利了结）` : ''}
                      {pos.max_hold_until ? `；最晚 ${fmtMytDate(pos.max_hold_until)} 卖出` : ''}。
                    </Text>
                  </div>
                </div>
              </List.Item>
            )}
          />
        ) : (
          <Text type="secondary">现在没有持股，全部是现金。</Text>
        )}
      </Card>

      <Card size="small" title="最近做了什么" style={{ marginTop: 12 }} data-testid="simple-recent">
        {recent.data?.items?.length ? (
          <List
            size="small"
            dataSource={recent.data.items}
            renderItem={(d) => (
              <List.Item style={{ paddingLeft: 0 }}>
                <div>
                  <Text type="secondary">{fmtMyt(d.as_of)}　</Text>
                  <Text strong>{plainAction(d)}</Text>
                  <div><Text type="secondary">原因：{(d.rationale || d.outcome_reason || '—').slice(0, 120)}</Text></div>
                </div>
              </List.Item>
            )}
          />
        ) : (
          <Text type="secondary">还没有成交过的交易。</Text>
        )}
        {noTrade ? (
          <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
            <InfoCircleFilled /> 今天没有买卖的主要原因：{noTrade}（没有合适机会时不交易是正常的）
          </Paragraph>
        ) : null}
      </Card>

      <Card size="small" title="测试进度：AI 到底会不会赚钱？" style={{ marginTop: 12 }} data-testid="simple-progress">
        <Paragraph style={{ marginBottom: 6 }}>
          已完成 <Text strong>{closed}</Text> / {NEED_TRADES} 笔交易（买入再卖出算 1 笔）。满 {NEED_TRADES} 笔后，系统自动判断及不及格。
        </Paragraph>
        <Progress percent={Math.min(100, Math.round((closed / NEED_TRADES) * 100))} showInfo={false} />
        {sc.data ? <Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0 }}>目前判断：{sc.data.verdict}</Paragraph> : null}
        <Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0 }}>
          不及格的话，不会用真钱；会建议改为定期买标普 500 指数基金。
        </Paragraph>
      </Card>

      <Paragraph type="secondary" style={{ marginTop: 16 }}>
        想看更多细节（订单、风控、AI 每次的分析），请打开左边菜单的「总览（专业版）」或其他页面。
      </Paragraph>
    </div>
  );
}
