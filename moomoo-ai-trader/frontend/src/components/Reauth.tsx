import { Alert, Form, Input, Modal, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { apiHandlers, api, errText } from '../api/client';

/**
 * 处理 428：敏感操作（实盘授权、放宽风控、手动清仓、批准策略升级等）需要最近 10 分钟内再次输入本机网页密码。
 * 注册到 api 客户端；验证成功后客户端会自动重试原请求一次。
 */
export function ReauthHost() {
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [pw, setPw] = useState('');
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  useEffect(() => {
    apiHandlers.requestReauth = () =>
      new Promise<boolean>((resolve) => {
        resolver.current = resolve;
        setErr(undefined);
        setPw('');
        setOpen(true);
      });
    return () => {
      apiHandlers.requestReauth = undefined;
    };
  }, []);

  const finish = (ok: boolean) => {
    setOpen(false);
    const r = resolver.current;
    resolver.current = null;
    r?.(ok);
  };

  const submit = async () => {
    if (!pw) {
      setErr('请输入本机网页密码');
      return;
    }
    setBusy(true);
    try {
      await api.reauth(pw);
      finish(true);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title="请再次输入本机网页密码"
      okText="确认"
      cancelText="取消操作"
      confirmLoading={busy}
      onOk={submit}
      onCancel={() => finish(false)}
      maskClosable={false}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary">
        这是一项敏感操作（例如实盘授权、放宽风控、手动清仓、批准策略升级）。为防止误操作，需要再次输入<strong>本机网页密码</strong>（10 分钟内有效）。
        这里不是 moomoo 密码，moomoo 密码和验证码只在官方 OpenD 窗口输入。
      </Typography.Paragraph>
      {err ? <Alert type="error" showIcon message={err} style={{ marginBottom: 12 }} /> : null}
      <Form layout="vertical" onFinish={submit}>
        <Form.Item label="本机网页密码" htmlFor="reauth-password">
          <Input.Password id="reauth-password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} onPressEnter={submit} autoComplete="current-password" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
