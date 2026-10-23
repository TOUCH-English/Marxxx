import { ArrowLeftOutlined, DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import { Alert, App, Button, Card, Col, Descriptions, Input, Row, Segmented, Space, Switch, Table, Tag, Typography } from 'antd';
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api, qs } from '../api/client';
import type { InstrumentDetail, InstrumentRow, Items, WatchItem } from '../api/types';
import { ErrorAlert, Explain, KV, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { chartNumber, fmtMoney, fmtPrice } from '../utils/decimal';
import { fmtAge, fmtMyt } from '../utils/format';
import { DATA_SOURCE_LABELS, FEATURE_LABELS, MARKET_LABELS, SEC_TYPE_LABELS } from '../utils/labels';

const { Text, Paragraph } = Typography;

function useWatchActions() {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const { modal } = App.useApp();
  const add = async (symbol: string, in_whitelist: boolean, note = '') => {
    const r = await run(
      () => api.post<{ instrument: InstrumentRow | null; verify_note: string }>(`/api/${ns}/watchlist`, { symbol, in_whitelist, note }),
      { success: (x) => `已加入自选${in_whitelist ? '并加入白名单' : ''}。${x.verify_note}` },
    );
    if (r?.instrument) {
      modal.info({
        title: '请核对名称与证券类型',
        content: (
          <Descriptions
            size="small"
            column={1}
            items={[
              { key: 's', label: '代码', children: r.instrument.symbol },
              { key: 'n', label: '名称', children: r.instrument.name },
              { key: 't', label: '类型', children: SEC_TYPE_LABELS[r.instrument.sec_type] ?? r.instrument.sec_type },
              { key: 'm', label: '市场 / 币种', children: `${MARKET_LABELS[r.instrument.market] ?? r.instrument.market} / ${r.instrument.currency}` },
              { key: 'l', label: '每手', children: r.instrument.lot_size },
              { key: 'lev', label: '杠杆/反向', children: r.instrument.is_leveraged ? '是（不会被交易）' : '否' },
            ]}
          />
        ),
      });
    }
    return r;
  };
  const remove = (symbol: string) => run(() => api.del(`/api/${ns}/watchlist/${encodeURIComponent(symbol)}`), { success: `已移除 ${symbol}` });
  return { add, remove, busy };
}

export function InstrumentsPage() {
  const { ns } = useAppState();
  const nav = useNavigate();
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [market, setMarket] = useState<string>('');
  const [manual, setManual] = useState('');
  const search = useApi<Items<InstrumentRow> & { data_source: string; note: string }>(`/api/${ns}/instruments${qs({ q: query, market })}`, { live: false });
  const watch = useApi<Items<WatchItem>>(`/api/${ns}/watchlist`);
  const { add, remove, busy } = useWatchActions();
  const watched = new Set((watch.data?.items ?? []).map((w) => w.symbol));

  return (
    <div>
      <PageTitle title="自选股与股票详情" sub="搜索、添加并核对证券名称与类型；查看价格、买卖价差、成交量、行情延迟与数据来源。" />
      <Explain>
        交易范围有两种：<Text strong>授权股票池</Text>（程序按规则筛选，AI 在池内挑选，推荐）和<Text strong>手动白名单</Text>（下面勾选「白名单」的证券）。在启动向导中选择使用哪一种。
        {ns === 'sim' ? ' 演示证券均为虚构，不构成投资推荐。' : ''}
      </Explain>

      <Card size="small" title={`我的自选（${watch.data?.items.length ?? 0}）`} data-testid="watchlist-card">
        <ErrorAlert error={watch.error} />
        <Table<WatchItem>
          size="small"
          rowKey="id"
          dataSource={watch.data?.items ?? []}
          pagination={false}
          scroll={{ x: true }}
          locale={{ emptyText: '还没有自选证券，可在下方搜索添加' }}
          columns={[
            { title: '代码', dataIndex: 'symbol', render: (s: string) => <a onClick={() => nav(`/instruments/${encodeURIComponent(s)}`)}>{s}</a> },
            { title: '最新价', render: (_, w) => (w.quote?.last ? fmtPrice(w.quote.last) : <Text type="secondary">暂无报价</Text>) },
            {
              title: '买一 / 卖一',
              render: (_, w) => (w.quote?.bid && w.quote?.ask ? `${fmtPrice(w.quote.bid)} / ${fmtPrice(w.quote.ask)}` : <Text type="secondary">无买卖盘数据</Text>),
            },
            { title: '报价时间', render: (_, w) => (w.quote?.source_ts ? fmtMyt(w.quote.source_ts) : '—') },
            { title: '来源', render: (_, w) => (w.quote ? <Tag>{w.quote.source}{w.quote.is_realtime ? '·实时' : '·非实时'}</Tag> : '—') },
            {
              title: '加入白名单',
              render: (_, w) => (
                <Switch
                  size="small"
                  checked={w.in_whitelist}
                  loading={busy}
                  onChange={(v) => void add(w.symbol, v, w.note)}
                  checkedChildren="白名单"
                  unCheckedChildren="仅自选"
                />
              ),
            },
            { title: '备注', dataIndex: 'note' },
            { title: '', render: (_, w) => <Button size="small" icon={<DeleteOutlined />} onClick={() => void remove(w.symbol)}>移除</Button> },
          ]}
        />
      </Card>

      <Card size="small" title="搜索证券" style={{ marginTop: 12 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="代码或名称"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onPressEnter={() => setQuery(q.trim())}
            style={{ width: 220 }}
          />
          <Segmented value={market} onChange={(v) => setMarket(String(v))} options={[{ label: '全部', value: '' }, { label: '马股', value: 'MY' }, { label: '美股', value: 'US' }]} />
          <Button type="primary" onClick={() => setQuery(q.trim())}>搜索</Button>
        </Space>
        {search.data?.note ? <Alert type="info" showIcon message={search.data.note} style={{ marginBottom: 8 }} /> : null}
        <ErrorAlert error={search.error} />
        <Table<InstrumentRow>
          size="small"
          rowKey="symbol"
          loading={search.loading}
          dataSource={search.data?.items ?? []}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: true }}
          title={() => <Text type="secondary">数据来源：{search.data ? DATA_SOURCE_LABELS[search.data.data_source] ?? search.data.data_source : '—'}</Text>}
          columns={[
            { title: '代码', dataIndex: 'symbol', render: (s: string) => <a onClick={() => nav(`/instruments/${encodeURIComponent(s)}`)}>{s}</a> },
            { title: '名称', dataIndex: 'name' },
            { title: '市场', dataIndex: 'market', render: (m: string) => MARKET_LABELS[m] ?? m },
            { title: '类型', dataIndex: 'sec_type', render: (t: string) => SEC_TYPE_LABELS[t] ?? t },
            { title: '币种', dataIndex: 'currency' },
            { title: '每手', dataIndex: 'lot_size' },
            {
              title: '标记',
              render: (_, i) => (
                <Space size={2} wrap>
                  {i.is_demo ? <Tag color="gold">虚构演示</Tag> : null}
                  {i.is_leveraged ? <Tag color="red">杠杆/反向（不交易）</Tag> : null}
                </Space>
              ),
            },
            {
              title: '操作',
              render: (_, i) =>
                watched.has(i.symbol) ? (
                  <Tag color="blue">已在自选</Tag>
                ) : (
                  <Space size={4} wrap>
                    <Button size="small" icon={<PlusOutlined />} onClick={() => void add(i.symbol, false)}>自选</Button>
                    <Button size="small" disabled={i.is_leveraged} onClick={() => void add(i.symbol, true)}>自选+白名单</Button>
                  </Space>
                ),
            },
          ]}
        />
        <Space wrap style={{ marginTop: 12 }}>
          <Text type="secondary">直接输入代码添加：</Text>
          <Input placeholder="例如 US.AAPL 或 MY.1155" value={manual} onChange={(e) => setManual(e.target.value)} style={{ width: 220 }} />
          <Button disabled={!manual.trim()} onClick={async () => { if (await add(manual.trim().toUpperCase(), false)) setManual(''); }}>
            加入自选
          </Button>
        </Space>
      </Card>
    </div>
  );
}

function movingAverage(values: (number | null)[], n: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  let count = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v !== null) {
      sum += v;
      count += 1;
    }
    if (i >= n) {
      const old = values[i - n];
      if (old !== null) {
        sum -= old;
        count -= 1;
      }
    }
    out.push(i >= n - 1 && count === n ? +(sum / n).toFixed(4) : null);
  }
  return out;
}

export function InstrumentDetailPage() {
  const { symbol = '' } = useParams();
  const { ns } = useAppState();
  const nav = useNavigate();
  const [days, setDays] = useState(250);
  const d = useApi<InstrumentDetail>(`/api/${ns}/instruments/${encodeURIComponent(symbol)}${qs({ days })}`);
  const watch = useApi<Items<WatchItem>>(`/api/${ns}/watchlist`);
  const { add, busy } = useWatchActions();
  const w = watch.data?.items.find((x) => x.symbol === symbol);

  const chart = useMemo(() => {
    const bars = d.data?.bars ?? [];
    const closes = bars.map((b) => chartNumber(b.close));
    const ma20 = movingAverage(closes, 20);
    const ma60 = movingAverage(closes, 60);
    return bars.map((b, i) => ({ day: b.day, close: closes[i], ma20: ma20[i], ma60: ma60[i] }));
  }, [d.data]);

  const inst = d.data?.instrument;
  const qt = d.data?.quote;
  return (
    <div>
      <PageTitle
        title={
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={() => nav('/instruments')} />
            {symbol} {inst ? <Text type="secondary" style={{ fontSize: 16 }}>{inst.name}</Text> : null}
          </Space>
        }
        extra={
          w ? (
            <Space>
              <Tag color="blue">已在自选</Tag>
              <Switch checked={w.in_whitelist} loading={busy} onChange={(v) => void add(symbol, v, w.note)} checkedChildren="白名单" unCheckedChildren="仅自选" />
            </Space>
          ) : (
            <Space>
              <Button onClick={() => void add(symbol, false)} loading={busy}>加入自选</Button>
              <Button onClick={() => void add(symbol, true)} loading={busy} disabled={inst?.is_leveraged}>加入自选并加入白名单</Button>
            </Space>
          )
        }
      />
      <ErrorAlert error={d.error} />
      {d.data?.disclaimer ? <Alert type="warning" showIcon message={d.data.disclaimer} style={{ marginBottom: 12 }} /> : null}
      {d.data?.error ? <Alert type="error" showIcon message="行情获取失败" description={d.data.error} style={{ marginBottom: 12 }} /> : null}
      <Row gutter={[12, 12]}>
        <Col xs={24} lg={10}>
          <Card size="small" title="证券资料（请核对名称与类型）" className="card-fill" loading={!d.data && !d.error}>
            {inst ? (
              <Descriptions
                size="small"
                column={1}
                items={[
                  { key: 'n', label: '名称', children: inst.name },
                  { key: 't', label: '证券类型', children: SEC_TYPE_LABELS[inst.sec_type] ?? inst.sec_type },
                  { key: 'm', label: '市场 / 币种', children: `${MARKET_LABELS[inst.market] ?? inst.market} / ${inst.currency}` },
                  { key: 'l', label: '每手股数', children: inst.lot_size },
                  { key: 'lev', label: '杠杆/反向产品', children: inst.is_leveraged ? <Tag color="red">是（系统不会交易）</Tag> : '否' },
                  { key: 'demo', label: '虚构演示证券', children: inst.is_demo ? <Tag color="gold">是</Tag> : '否' },
                  { key: 'src', label: '数据来源', children: DATA_SOURCE_LABELS[d.data?.data_source ?? ''] ?? d.data?.data_source },
                ]}
              />
            ) : (
              <Alert type="warning" showIcon message="当前数据源中找不到这只证券，无法核对名称与类型" />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={14}>
          <Card size="small" title="报价" className="card-fill" loading={!d.data && !d.error}>
            {qt ? (
              <>
                {qt.suspended ? <Alert type="error" showIcon message="停牌" style={{ marginBottom: 8 }} /> : null}
                <Descriptions
                  size="small"
                  column={{ xs: 1, sm: 2 }}
                  items={[
                    { key: 'last', label: '最新价', children: <Text strong style={{ fontSize: 18 }}>{fmtPrice(qt.last)}</Text> },
                    { key: 'prev', label: '昨收', children: fmtPrice(qt.prev_close) },
                    { key: 'bid', label: '买一', children: qt.bid ? fmtPrice(qt.bid) : <Text type="secondary">无买卖盘数据</Text> },
                    { key: 'ask', label: '卖一', children: qt.ask ? fmtPrice(qt.ask) : <Text type="secondary">无买卖盘数据</Text> },
                    { key: 'spr', label: '买卖价差', children: qt.spread_bps !== null && qt.spread_bps !== undefined ? `${qt.spread_bps} 基点（${(qt.spread_bps / 100).toFixed(2)}%）` : <Text type="secondary">无买卖盘数据</Text> },
                    { key: 'vol', label: '成交量', children: qt.volume !== null ? fmtMoney(String(qt.volume), 0) : '暂无数据' },
                    { key: 'ts', label: '报价时间（马来西亚）', children: fmtMyt(qt.source_ts, true) },
                    { key: 'delay', label: '行情延迟', children: qt.delay_seconds !== null ? fmtAge(qt.delay_seconds) : '暂无数据' },
                    { key: 'rt', label: '实时', children: qt.is_realtime ? <Tag color="green">实时</Tag> : <Tag color="gold">非实时</Tag> },
                    { key: 'src', label: '来源', children: qt.source },
                  ]}
                />
                {qt.note ? <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>{qt.note}</Paragraph> : null}
              </>
            ) : (
              <Text type="secondary">暂无报价（不会伪造盘口）。</Text>
            )}
          </Card>
        </Col>
      </Row>
      <Card
        size="small"
        title="日K 收盘价与均线（MA20 / MA60 由页面根据返回的日K计算，仅供参考）"
        style={{ marginTop: 12 }}
        extra={<Segmented value={days} onChange={(v) => setDays(Number(v))} options={[{ label: '3个月', value: 70 }, { label: '1年', value: 250 }, { label: '2年', value: 500 }]} />}
      >
        {chart.length ? (
          <div className="chart-box">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="day" tick={{ fontSize: 11 }} minTickGap={40} />
                <YAxis domain={['auto', 'auto']} tick={{ fontSize: 11 }} width={56} />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="close" name="收盘价" stroke="#1677ff" dot={false} strokeWidth={1.6} isAnimationActive={false} />
                <Line type="monotone" dataKey="ma20" name="MA20" stroke="#fa8c16" dot={false} strokeWidth={1.2} isAnimationActive={false} connectNulls={false} />
                <Line type="monotone" dataKey="ma60" name="MA60" stroke="#722ed1" dot={false} strokeWidth={1.2} isAnimationActive={false} connectNulls={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <Text type="secondary">暂无日K数据</Text>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>截至业务时间 {fmtMyt(d.data?.as_of)}，共 {d.data?.bars.length ?? 0} 根已收盘日K。</Text>
      </Card>
      <Card size="small" title="技术特征（程序计算，AI 决策时可见）" style={{ marginTop: 12 }}>
        <KV data={d.data?.features} labels={FEATURE_LABELS} column={3} />
      </Card>
      {d.data?.last_snapshot ? (
        <Card size="small" title="系统最近保存的报价快照" style={{ marginTop: 12 }}>
          <Descriptions
            size="small"
            column={{ xs: 1, md: 3 }}
            items={[
              { key: 'l', label: '最新价', children: fmtPrice(d.data.last_snapshot.last) },
              { key: 'b', label: '买一/卖一', children: d.data.last_snapshot.bid && d.data.last_snapshot.ask ? `${fmtPrice(d.data.last_snapshot.bid)} / ${fmtPrice(d.data.last_snapshot.ask)}` : '无买卖盘数据' },
              { key: 't', label: '时间', children: fmtMyt(d.data.last_snapshot.source_ts, true) },
            ]}
          />
        </Card>
      ) : null}
    </div>
  );
}

