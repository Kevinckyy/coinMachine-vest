"""HTTP API and optional production frontend, bound to loopback only."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .engine import Engine, ValidationError
from .backtest_service import BacktestService
from .strategies import catalogue

ROOT = Path(__file__).resolve().parent.parent


def create_server(engine, port=8765, host='127.0.0.1', static_dir=None,backtests=None):
    static_root = Path(static_dir or ROOT / 'dist').resolve()
    backtests=backtests or BacktestService(':memory:' if engine.db_path==':memory:' else Path(engine.db_path).parent/'backtests.sqlite')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'VestLocal/1.0'

        def log_message(self, fmt, *args):
            # Never log request paths, bodies, Authorization headers or bridge tokens.
            if args and str(args[1] if len(args)>1 else '').startswith('5'):
                sys.stderr.write('HTTP server error\n')

        def _origin(self):
            origin = self.headers.get('Origin', '')
            allowed = {f'http://{name}:{p}' for name in ('localhost', '127.0.0.1') for p in (5173,4173,self.server.server_port)}
            if origin in allowed:
                return origin
            if re.fullmatch(r'chrome-extension://[a-p]{32}', origin) and self.path.split('?')[0] in ('/api/bridge/accounts', '/api/bridge/poll', '/api/bridge/result', '/api/bridge/observe','/api/bridge/session','/api/bridge/preparation','/api/bridge/size','/api/bridge/inspect','/api/backtests/cost-observation'):
                return origin
            return None

        def _guard(self):
            try:
                parsed = urlparse('http://'+self.headers.get('Host', ''))
                valid_host = parsed.hostname in ('localhost','127.0.0.1') and parsed.port == self.server.server_port
            except ValueError:
                valid_host = False
            if not valid_host:
                raise ValidationError('无效 Host；服务仅供本机使用', 403)
            if self.headers.get('Origin') and self._origin() is None:
                raise ValidationError('来源不被允许', 403)
            if self.path.split('?')[0] in ('/api/bridge/accounts', '/api/bridge/poll', '/api/bridge/result', '/api/bridge/observe','/api/bridge/session','/api/bridge/preparation','/api/bridge/size','/api/bridge/inspect','/api/backtests/cost-observation'):
                token = self.headers.get('X-Bridge-Token', '')
                if not hmac.compare_digest(token, engine.bridge_token):
                    raise ValidationError('桥接配对码无效', 401)

        def _headers(self, status, content_type='application/json; charset=utf-8', length=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            if length is not None:
                self.send_header('Content-Length', str(length))
            origin = self._origin()
            if origin:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            if self.command=='OPTIONS':
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Bridge-Token')
            self.end_headers()

        def _json(self, body, status=200):
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self._headers(status, length=len(raw))
            self.wfile.write(raw)

        def _error(self, exc):
            # Windows can reset a closing socket with unread request bytes,
            # hiding a useful 401/403 from the plugin. Discard only a bounded
            # body, without parsing or authorizing it, before sending the error.
            if self.command=='POST' and not getattr(self,'_body_read',False):
                previous_timeout=self.connection.gettimeout()
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if 0<length<=65536:
                        self.connection.settimeout(.25)
                        self.rfile.read(length)
                except (OSError,ValueError):pass
                finally:self.connection.settimeout(previous_timeout)
            try:
                if isinstance(exc, ValidationError):
                    self._json({'error': str(exc)}, exc.status)
                elif isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
                    self._json({'error': '请求必须是有效的 JSON 对象'}, 400)
                else:
                    # No exception details or filesystem locations are exposed.
                    self._json({'error': '本地服务内部错误，请检查服务器终端'}, 500)
                    print(f'HTTP handler error: {type(exc).__name__}', file=sys.stderr)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _body(self):
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ValidationError('只接受 application/json 请求', 415)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValidationError('请求长度无效') from None
            if length < 0 or length > 65536:
                raise ValidationError('请求体不得超过 64 KiB', 413)
            raw=self.rfile.read(length)
            self._body_read=True
            body = json.loads(raw or b'{}')
            if not isinstance(body, dict):
                raise ValidationError('请求必须是 JSON 对象')
            return body

        def do_OPTIONS(self):
            try:
                # Preflights do not carry X-Bridge-Token. Check Host and origin
                # here; the subsequent actual request validates the token.
                parsed = urlparse('http://'+self.headers.get('Host',''))
                if parsed.hostname not in ('localhost','127.0.0.1') or parsed.port != self.server.server_port or not self._origin():
                    raise ValidationError('来源不被允许', 403)
                self._headers(204, length=0)
            except Exception as exc:
                self._error(exc)

        def do_GET(self):
            try:
                self._guard()
                parsed = urlparse(self.path)
                path = parsed.path
                if path=='/api/health':
                    return self._json({'ok': True, 'service': 'vest-local', 'version': '1.0.0'})
                if path=='/api/state':
                    return self._json(engine.state())
                if path=='/api/strategies':return self._json({'strategies':catalogue()})
                if path=='/api/backtests/meta':return self._json(backtests.metadata(engine.state()))
                if path=='/api/backtests/runs':return self._json({'runs':backtests.store.runs()})
                if path=='/api/backtests/history':
                    q=parse_qs(parsed.query)
                    try:
                        offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['20'])[0])
                        start=float(q['from'][0]) if 'from' in q else None;end=float(q['to'][0]) if 'to' in q else None
                    except ValueError:raise ValidationError('历史时间或分页参数无效') from None
                    return self._json(backtests.store.history(root=q.get('root',[''])[0],strategy_id=q.get('strategy_id',[''])[0],
                        start=start,end=end,time_basis=q.get('time_basis',['created'])[0],pattern=q.get('pattern',[''])[0],offset=offset,limit=limit))
                if re.fullmatch(r'/api/backtests/batches/[a-f0-9]{32}',path):
                    query=parse_qs(parsed.query)
                    try:offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['100'])[0])
                    except ValueError:raise ValidationError('多轮分页参数无效') from None
                    return self._json(backtests.store.batch(path.rsplit('/',1)[1],offset,limit,summary_only=query.get('summary_only')==['1']))
                if re.fullmatch(r'/api/backtests/jobs/[a-f0-9]{32}',path):return self._json(backtests.store.job(path.rsplit('/',1)[1]))
                if re.fullmatch(r'/api/backtests/runs/[a-f0-9]{32}',path):return self._json(backtests.store.run(path.rsplit('/',1)[1]))
                if re.fullmatch(r'/api/backtests/runs/[a-f0-9]{32}/details',path):
                    query=parse_qs(parsed.query)
                    try:offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['100'])[0])
                    except ValueError:raise ValidationError('明细分页参数无效') from None
                    return self._json(backtests.store.details(path.split('/')[4],query.get('kind',['trades'])[0],offset,limit,query.get('phase',[None])[0]))
                if path=='/api/backtests/candles':
                    q=parse_qs(parsed.query)
                    try:
                        interval=int(q.get('interval',['60'])[0]);start=float(q.get('from',['0'])[0]);end=float(q.get('to',[str(1e12)])[0])
                    except (ValueError,TypeError):raise ValidationError('K线预览参数无效') from None
                    return self._json({'candles':backtests.store.bars(q.get('symbol',[''])[0],interval,start,end)[-2000:]})
                if path=='/api/account-observation':
                    return self._json(engine.account_observation())
                if path=='/api/bridge/config':
                    return self._json({'token': engine.bridge_token, 'endpoint': f'http://127.0.0.1:{self.server.server_port}',
                                       'extension_path': 'extension', 'allowed_site': 'https://next.vestmarkets.com'})
                if path=='/api/bridge/accounts':
                    accounts = [{k:a[k] for k in ('id','name','mode','status','enabled')}
                                for a in engine.state()['accounts'] if a['mode']=='browser']
                    return self._json({'accounts':accounts})
                if path=='/api/history.csv':
                    query = parse_qs(parsed.query)
                    raw = engine.history_csv(query.get('account_id',[None])[0], query.get('days',[None])[0]).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="vest-pnl-history.csv"')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control','no-store')
                    if self._origin():
                        self.send_header('Access-Control-Allow-Origin',self._origin())
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path.startswith('/api/'):
                    raise ValidationError('接口不存在',404)
                target = (static_root / unquote(path).lstrip('/')).resolve()
                if not target.is_relative_to(static_root):
                    raise ValidationError('路径不被允许',403)
                if not target.is_file():
                    target = static_root/'index.html'
                if target.is_file():
                    raw = target.read_bytes()
                    self._headers(200, mimetypes.guess_type(target.name)[0] or 'application/octet-stream', len(raw))
                    self.wfile.write(raw)
                    return
                return self._json({'service':'vest-local','frontend':'运行 npm run dev，或 npm run build 后访问此地址。'})
            except Exception as exc:
                self._error(exc)

        def do_POST(self):
            try:
                self._guard()
                body = self._body()
                path = urlparse(self.path).path
                if path=='/api/accounts':
                    result = engine.add_account(body)
                elif path=='/api/backtests/sync':result=backtests.sync(body)
                elif path=='/api/backtests/run':result=backtests.backtest(body)
                elif path=='/api/backtests/estimate':result=backtests.estimate(body)
                elif re.fullmatch(r'/api/backtests/history/[a-f0-9]{32}/name',path):result=backtests.store.rename_history(path.split('/')[4],body.get('custom_name'))
                elif re.fullmatch(r'/api/backtests/jobs/[a-f0-9]{32}/cancel',path):result=backtests.cancel(path.split('/')[4])
                elif path=='/api/backtests/cost-observation':result=backtests.observe_costs(body)
                elif re.fullmatch(r'/api/accounts/[^/]+/toggle',path):
                    result = engine.toggle_account(path.split('/')[3],body.get('enabled'))
                elif re.fullmatch(r'/api/accounts/[^/]+/strategy-lock',path):
                    result = engine.strategy_lock_account(path.split('/')[3],body.get('locked'))
                elif re.fullmatch(r'/api/accounts/[^/]+/archive',path):
                    result = engine.archive_account(path.split('/')[3],body)
                elif path=='/api/orders':
                    result = engine.place_orders(body)
                elif path=='/api/positions/close-all':
                    result = engine.close_all()
                elif re.fullmatch(r'/api/positions/[^/]+/close',path):
                    result = engine.close_position(path.split('/')[3])
                elif path=='/api/strategy/start':
                    result = engine.start_strategy(body)
                elif path=='/api/strategy/config':
                    result = engine.save_strategy_config(body)
                elif path=='/api/strategy/stop':
                    result = engine.stop_strategy()
                elif path=='/api/settings':
                    result = engine.settings(body)
                elif path=='/api/bridge/inspect':
                    result = engine.bridge_inspect(body)
                elif path=='/api/bridge/poll':
                    result = engine.bridge_poll(body)
                elif path=='/api/bridge/result':
                    result = engine.bridge_result(body)
                elif path=='/api/bridge/size':
                    result = engine.bridge_size(body)
                elif path=='/api/bridge/reconcile':
                    result = engine.reconcile(body)
                elif path=='/api/bridge/observe':
                    result = engine.observe_accounts(body)
                elif path=='/api/bridge/session':
                    result = engine.bridge_session(body)
                elif path=='/api/bridge/preparation':
                    result = engine.bridge_preparation(body)
                else:
                    raise ValidationError('接口不存在',404)
                self._json(result)
            except Exception as exc:
                self._error(exc)

        def do_DELETE(self):
            try:
                self._guard()
                path = urlparse(self.path).path
                if not re.fullmatch(r'/api/accounts/[^/]+',path):
                    raise ValidationError('接口不存在',404)
                self._json(engine.delete_account(path.split('/')[3]))
            except Exception as exc:
                self._error(exc)

    class LocalHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        def server_close(self):
            super().server_close()
            backtests.close()

    return LocalHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Vest 本地交易管理服务（默认模拟交易）')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default=str(ROOT/'runtime'/'vest.sqlite'))
    args = parser.parse_args()
    engine = Engine(args.db)
    server = create_server(engine, port=args.port)
    engine.start()
    print(f'Vest local server: http://127.0.0.1:{args.port}', flush=True)
    print('Paper mode is simulated. Browser mode requires an explicitly paired and armed extension.', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()


if __name__ == '__main__':
    main()
