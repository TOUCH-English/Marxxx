import { Alert, Button, Card, Col, Collapse, Descriptions, Empty, Input, Modal, Row, Space, Statistic, Table, Tag, Typography } from 'antd';
import { useState } from 'react';
import { api } from '../api/client';
import type { Candidate, LearningInfo } from '../api/types';
import { DownloadLink, ErrorAlert, Explain, KV, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { fmtMyt, fmtMytDate, fmtMytDateEnd, fmtValue } from '../utils/format';
import { CANDIDATE_STATUS, DATA_SOURCE_LABELS, EXPERIENCE_KIND_LABELS, MODE_LABELS } from '../utils/labels';
import { DeploymentsTable } from './Risk';

const { Text, Paragraph } = Typography;

const EVAL_PLAN_LABELS: Record<string, string> = {
  min_closed_trades: '最少已平仓交易数',
  min_window_days: '最短观察天数',
  max_window_days: '最长观察天数',
  accept_if: '通过门槛',
  metrics: '比较指标',
  note: '说明',
};

const VERDICT: Record<string, { label: string; color: string }> = {
  PASS: { label: '通过（可供审核）', color: 'green' },
  FAIL: { label: '未通过', color: 'red' },
  INSUFFICIENT: { label: '样本不足', color: 'orange' },
  CONFLICT: { label: '结果冲突', color: 'purple' },
};

function changeText(c: Candidate) {
  const ch = c.changes;
  if (ch && ch.path !== undefined) return `${ch.path}：${fmtValue(ch.from)} → ${fmtValue(ch.to)}`;
  return fmtValue(ch);
}

function ApproveModal({ c, onClose }: { c: Candidate | null; onClose: () => void }) {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [note, setNote] = useState('');
  return (
    <Modal
      open={!!c}
      title={`批准候选 #${c?.id ?? ''} 上线（策略升级）`}
      okText="批准并切换版本"
      cancelText="取消"
      confirmLoading={busy}
      onCancel={onClose}
      width={Math.min(680, window.innerWidth)}
      onOk={async () => {
        if (!c) return;
        const r = await run(() => api.post(`/api/${ns}/learning/candidates/${c.id}/approve`, { note }), { success: '已批准：新决策将使用新版本' });
        if (r) onClose();
      }}
    >
      {c ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="这是策略升级确认，不是逐笔交易确认"
            description="切换只影响之后的新决策；已有持仓保留原来的止损/止盈/持有期限保护。现行授权内的自动交易继续运行。需要再次输入密码。"
          />
          <Descriptions
            size="small"
            bordered
            column={1}
            items={[
              { key: 'ch', label: '改了什么（简明差异）', children: <Text strong>{changeText(c)}</Text> },
              { key: 'h', label: '假设', children: c.hypothesis },
              { key: 'b', label: '预计好处', children: c.expected_benefit || '—' },
              { key: 'cost', label: '可能代价', children: c.possible_cost || '—' },
              { key: 'e', label: '证据', children: fmtValue(c.evidence) },
              { key: 'r', label: '评估结论', children: c.status_reason || '—' },
            ]}
          />
          <Input.TextArea rows={2} placeholder="备注（可选）" value={note} onChange={(e) => setNote(e.target.value)} />
        </Space>
      ) : null}
    </Modal>
  );
}

export function LearningPage() {
  const { ns } = useAppState();
  const l = useApi<LearningInfo>(`/api/${ns}/learning`);
  const { run, busy } = useAction();
  const [approve, setApprove] = useState<Candidate | null>(null);
  const [rbOpen, setRbOpen] = useState(false);
  const [rbNote, setRbNote] = useState('');
  const d = l.data;
  const canRollback = !!d?.deployments.some((x) => x.action === 'ACTIVATE');

  return (
    <div>
      <PageTitle
        title="复盘与改进"
        sub="系统每天自动生成复盘、每周提出少量有明确假设的改进候选，并在隔离模拟中用候选生成之后的数据验证。候选不会自动上线。"
        extra={
          <Button danger disabled={!canRollback} onClick={() => setRbOpen(true)}>
            回退到上一版本
          </Button>
        }
      />
      <ErrorAlert error={l.error} />
      {d ? <Explain>{d.note}</Explain> : null}
      {d ? (
        <>
          <Row gutter={[12, 12]}>
            <Col xs={24} md={8}>
              <Card size="small" title="当前运行版本" className="card-fill">
                {d.active_version ? (
                  <>
                    <Statistic value={`${d.active_version.key} ${d.active_version.version}`} valueStyle={{ fontSize: 20 }} />
                    <Text type="secondary">{d.active_version.note}</Text>
                    <div><Text type="secondary">批准时间 {fmtMyt(d.active_version.approved_at)}</Text></div>
                  </>
                ) : (
                  <Text type="secondary">暂无数据</Text>
                )}
              </Card>
            </Col>
            <Col xs={24} md={8}>
              <Card size="small" title="经验记录（当前运行）" className="card-fill">
                <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                  {d.mode ? `${MODE_LABELS[d.mode] ?? d.mode} · ${DATA_SOURCE_LABELS[d.data_source ?? ''] ?? d.data_source}` : '尚未启动运行'}
                </Paragraph>
                {Object.keys(d.experience_counts).length ? (
                  Object.entries(d.experience_counts).map(([k, v]) => (
                    <Tag key={k} style={{ marginBottom: 4 }}>{EXPERIENCE_KIND_LABELS[k] ?? k} {v}</Tag>
                  ))
                ) : (
                  <Text type="secondary">暂无经验记录</Text>
                )}
                <div style={{ marginTop: 6 }}>
                  <Text type="secondary">已平仓交易分类：</Text>
                  {Object.keys(d.trade_classes).length
                    ? Object.entries(d.trade_classes).map(([k, v]) => <Tag key={k}>{k} {v}</Tag>)
                    : <Text type="secondary">暂无</Text>}
                </div>
              </Card>
            </Col>
            <Col xs={24} md={8}>
              <Card size="small" title="候选统计（含全部失败）" className="card-fill">
                <Row>
                  <Col span={12}><Statistic title="试过的候选" value={d.tried_total} /></Col>
                  <Col span={12}><Statistic title="失败 / 证据不足" value={d.failed_total} /></Col>
                </Row>
              </Card>
            </Col>
          </Row>

          <Card size="small" title={`改进候选（${d.candidates.length}）`} style={{ marginTop: 12 }}>
            <Table<Candidate>
              size="small"
              rowKey="id"
              dataSource={d.candidates}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              scroll={{ x: 1100 }}
              locale={{ emptyText: '暂无候选（每周改进分析需要足够的已平仓交易；数据不足时保持当前版本）' }}
              expandable={{
                expandedRowRender: (c) => (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <KV
                      data={{
                        预计好处: c.expected_benefit,
                        可能代价: c.possible_cost,
                        证据: fmtValue(c.evidence),
                        数据截止: fmtMyt(c.data_cutoff),
                        提出者: c.proposed_by,
                        基准版本: `#${c.base_strategy_version_id}`,
                        候选版本: c.candidate_strategy_version_id ? `#${c.candidate_strategy_version_id}` : '—',
                      }}
                    />
                    <Text strong>预先冻结的评价计划</Text>
                    <KV data={c.eval_plan} labels={EVAL_PLAN_LABELS} />
                  </Space>
                ),
              }}
              columns={[
                { title: '#', dataIndex: 'id', width: 50 },
                { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={CANDIDATE_STATUS[s]?.color}>{CANDIDATE_STATUS[s]?.label ?? s}</Tag> },
                { title: '改变（原 → 新）', render: (_, c) => <Text code>{changeText(c)}</Text> },
                { title: '假设', dataIndex: 'hypothesis' },
                { title: '状态原因', dataIndex: 'status_reason' },
                { title: '更新', dataIndex: 'updated_at_utc', render: (v: string) => fmtMyt(v) },
                {
                  title: '',
                  render: (_, c) =>
                    c.status === 'READY_FOR_REVIEW' ? (
                      <Button size="small" type="primary" onClick={() => setApprove(c)}>审核并批准</Button>
                    ) : null,
                },
              ]}
            />
          </Card>

          <Card size="small" title="比较评估记录" style={{ marginTop: 12 }}>
            <Table
              size="small"
              rowKey="id"
              dataSource={d.evaluations}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              scroll={{ x: true }}
              locale={{ emptyText: '暂无评估' }}
              columns={[
                { title: '#', dataIndex: 'id' },
                { title: '候选', dataIndex: 'candidate_id', render: (v: number) => `#${v}` },
                { title: '观察窗口', render: (_, e) => `${fmtMytDate(e.window_start)} ~ ${fmtMytDate(e.window_end)}` },
                { title: '结论', dataIndex: 'verdict', render: (v: string) => <Tag color={VERDICT[v]?.color}>{VERDICT[v]?.label ?? v}</Tag> },
                { title: '说明', dataIndex: 'notes' },
                { title: '时间', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
              ]}
            />
          </Card>

          <Card size="small" title="每周改进分析" style={{ marginTop: 12 }}>
            {d.weekly.length ? (
              <Collapse
                items={d.weekly.map((w) => ({
                  key: w.id,
                  label: `${fmtMytDate(w.period_start)} ~ ${fmtMytDateEnd(w.period_end)}`,
                  extra: <DownloadLink href={`/api/${ns}/reviews/${w.id}.txt`}>.txt</DownloadLink>,
                  children: <pre className="report-text">{w.summary}</pre>,
                }))}
              />
            ) : (
              <Empty description="暂无周度分析（每周自动运行；数据不足时输出「证据不足，保持当前版本」）" />
            )}
          </Card>

          <Card size="small" title="版本上线 / 回退历史" style={{ marginTop: 12 }}>
            <DeploymentsTable items={d.deployments} />
          </Card>
        </>
      ) : (
        <Card loading />
      )}

      <ApproveModal c={approve} onClose={() => setApprove(null)} />
      <Modal
        open={rbOpen}
        title="回退到上一版本？"
        okText="确认回退"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        confirmLoading={busy}
        onCancel={() => setRbOpen(false)}
        onOk={async () => {
          const r = await run(() => api.post(`/api/${ns}/learning/rollback`, { key: 'AI_AUTONOMOUS', note: rbNote }), { success: '已回退到上一版本' });
          if (r) setRbOpen(false);
        }}
      >
        <Paragraph>回退只把「AI 自主决策」策略切回上一次上线前的版本，影响之后的新决策。</Paragraph>
        <Alert type="warning" showIcon style={{ marginBottom: 8 }} message="回退不会清除亏损锁定、不会重新下单，也不会放宽已有持仓的保护。" />
        <Input.TextArea rows={2} placeholder="回退原因（可选）" value={rbNote} onChange={(e) => setRbNote(e.target.value)} />
      </Modal>
    </div>
  );
}
