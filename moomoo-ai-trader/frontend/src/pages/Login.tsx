import { LockOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Checkbox, Form, Input, Typography } from 'antd';
import { useState } from 'react';
import { api, errText } from '../api/client';

const { Title, Paragraph, Text } = Typography;

function Notice() {
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message="这里只输入「本机网页密码」"
      description={
        <>
          这个密码只用来保护本机这个网页（防止别人或其他网页操作您的交易设置）。
          <Text strong>请不要在这里输入 moomoo 账号密码或验证码</Text>——它们只能在 moomoo 官方 OpenD 窗口中输入，本系统不会索取。
        </>
      }
    />
  );
}

export function SetupPage({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const onFinish = async (v: { password: string }) => {
    setBusy(true);
    setErr(undefined);
    try {
      await api.post('/api/auth/setup', { password: v.password });
      onDone();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-wrap">
      <Card className="auth-card">
        <Title level={3}>
          <SafetyCertificateOutlined /> 首次使用：设置本机网页密码
        </Title>
        <Paragraph type="secondary">欢迎使用 Moomoo AI 自动交易工作台。先设置一个至少 8 位的密码，之后打开本页面时需要输入。</Paragraph>
        <Notice />
        {err ? <Alert type="error" showIcon message={err} style={{ marginBottom: 12 }} /> : null}
        <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
          <Form.Item name="password" label="新密码（至少 8 位）" rules={[{ required: true, message: '请输入密码' }, { min: 8, message: '密码至少 8 位' }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="再次输入新密码"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入密码' },
              ({ getFieldValue }) => ({
                validator: (_, v) => (!v || getFieldValue('password') === v ? Promise.resolve() : Promise.reject(new Error('两次输入的密码不一致'))),
              }),
            ]}
          >
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>
            设置密码并进入
          </Button>
        </Form>
        <Paragraph type="secondary" style={{ marginTop: 16, fontSize: 12 }}>
          忘记密码时，可以停止系统后删除 data/secrets/web_password.json 再重新设置（不影响交易数据）。
        </Paragraph>
      </Card>
    </div>
  );
}

export function LoginPage({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const onFinish = async (v: { password: string; remember?: boolean }) => {
    setBusy(true);
    setErr(undefined);
    try {
      await api.post('/api/auth/login', { password: v.password, remember: !!v.remember });
      onDone();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-wrap">
      <Card className="auth-card">
        <Title level={3}>
          <LockOutlined /> 登录 Moomoo AI 自动交易工作台
        </Title>
        <Notice />
        {err ? <Alert type="error" showIcon message={err} style={{ marginBottom: 12 }} /> : null}
        <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
          <Form.Item name="password" label="本机网页密码" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} autoFocus autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="remember" valuePropName="checked" initialValue={false} style={{ marginBottom: 12 }}
            extra="只在您自己的 Mac 上勾选。30 天后、退出登录或更换密码后需要重新登录；放宽风控等重要操作仍会要求输入密码。">
            <Checkbox data-testid="remember-login">记住这台 Mac 30 天</Checkbox>
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>
            登录
          </Button>
        </Form>
        <Paragraph type="secondary" style={{ marginTop: 16, fontSize: 12 }}>
          已获授权的后台运行不依赖这个网页：即使退出登录或关闭浏览器，后台仍会按授权运行（包括持仓保护）。
        </Paragraph>
      </Card>
    </div>
  );
}
