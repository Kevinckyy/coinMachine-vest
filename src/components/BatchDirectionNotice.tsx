import {ShieldCheck} from 'lucide-react';

export default function BatchDirectionNotice({serverReady}: {serverReady: boolean}) {
  return <div className="batch-direction-notice" role="note" data-testid="strategy-batch-direction-rule">
    <ShieldCheck size={19}/><div><strong>同批同品种必须同向</strong>
      <p>同一批账号统一做多或做空，随机方向每批只抽取一次。考核号和 Live 号同批时，一方固定、另一方随机则统一固定方向；固定方向相反时整批暂停，禁止反向开仓。</p>
      {!serverReady && <p className="batch-direction-upgrade" role="alert">当前服务尚未启用同批同向校验，请重启本地服务后再启动策略。</p>}
    </div>
  </div>;
}
