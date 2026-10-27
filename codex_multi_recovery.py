#!/usr/bin/env python3
"""Monitor active Codex daemon threads and retry stopped failed turns once."""
import argparse, json, os, re, socket, sys, time

VERBOSE = False
def log(message):
    if VERBOSE:
        safe = re.sub(r"\b(?:sk|Bearer)[-_ ][A-Za-z0-9._-]{12,}", "[redacted]", str(message), flags=re.I)
        print(time.strftime("%H:%M:%S"), "DEBUG", safe, flush=True)

class RPCError(Exception): pass
class RPC:
    """JSON-RPC over WebSocket on the existing daemon's Unix socket."""
    def __init__(self, path, timeout=30):
        try:
            import websocket
        except ImportError:
            raise RPCError('Missing dependency. Run: python3 -m pip install websocket-client')
        self.ws = None
        self.seq = 0
        self.timeout = timeout
        log('Connecting directly to daemon socket: ' + path)
        from urllib.parse import urlparse
        is_url=path.startswith("ws://")
        if is_url:
            endpoint=urlparse(path)
            if endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise RPCError("Only local WebSocket endpoints are supported")
            raw=socket.create_connection((endpoint.hostname,endpoint.port),timeout)
        else:
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(timeout)
        try:
            if not is_url: raw.connect(path)
            self.ws = websocket.create_connection(path if is_url else 'ws://localhost/', socket=raw,
                                                  timeout=timeout, enable_multithread=True, suppress_origin=True)
            log('WebSocket handshake accepted')
            self.call('initialize', {'clientInfo': {'name':'codex-recovery','title':'Codex recovery','version':'0.2'}, 'capabilities': {'experimentalApi':True,'requestAttestation':False}})
            self.send({'method':'initialized','params':{}})
            log('Initialization complete')
        except Exception as e:
            raw.close()
            self.close()
            raise RPCError("Connection/initialization failed: " + str(e)) from e
    def send(self, value):
        if self.ws is None: raise RPCError("Connection is closed")
        try: self.ws.send(json.dumps(value))
        except Exception as e:
            self.close()
            raise RPCError("Send failed: " + str(e)) from e
    def call(self, method, params, timeout=None):
        import websocket
        self.seq += 1
        n = self.seq
        log('RPC request id=%s method=%s thread=%s' % (n, method, params.get('threadId', '-')))
        self.send({'id':n,'method':method,'params':params})
        end = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = end-time.monotonic()
            if remaining <= 0:
                self.close()
                raise RPCError(method + ' timed out; connection closed to prevent ambiguous repeated actions')
            self.ws.settimeout(remaining)
            try:
                message = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                self.close()
                raise RPCError(method + ' timed out; connection closed to prevent ambiguous repeated actions')
            except websocket.WebSocketException as e:
                self.close()
                raise RPCError('Connection failed: ' + str(e)) from e
            if not message:
                self.close()
                raise RPCError('daemon closed the connection')
            try: item = json.loads(message)
            except ValueError: raise RPCError('daemon sent invalid JSON')
            if 'method' in item:
                log('RPC event: %s%s' % (item['method'], ' (server request left unanswered)' if 'id' in item else ''))
            if item.get('id') == n and ('result' in item or 'error' in item):
                if 'error' in item:
                    log('RPC error: %s' % item['error'])
                    raise RPCError(str(item['error']))
                log('RPC response id=%s method=%s received' % (n, method))
                return item['result']
    def close(self):
        if self.ws is not None:
            self.ws.close(timeout=1)
            self.ws = None

def blocked_error(error):
    if not error: return True
    if isinstance(error, dict) and error.get('misalignment'): return True
    s = json.dumps(error).lower()
    return any(x in s for x in ('cybersecurity','safety check','approval required','permission denied','usage limit','usage_limit','quota exceeded','budget limit'))

def pages(rpc, method, params):
    cursor=None; seen=set()
    while True:
        r=rpc.call(method,dict(params,cursor=cursor)); yield from r.get('data',[])
        cursor=r.get('nextCursor')
        if not cursor: return
        if cursor in seen: raise RPCError('repeated pagination cursor')
        seen.add(cursor)

def run(rpc, dry_run, interval):
    tracked={}; attempted=set()
    print('Monitoring active root threads from the shared Codex daemon. Ctrl-C to stop.', flush=True)
    while True:
        start=time.monotonic()
        log('Beginning poll; tracked=%d attempted_turns=%d' % (len(tracked), len(attempted)))
        try:
            ids=list(pages(rpc,'thread/loaded/list',{'limit':100}))
            log('Loaded threads (%d): %s' % (len(ids), ', '.join(ids) or 'none'))
            for tid in ids:
                try:
                    thread=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                    if thread.get('parentThreadId'):
                        log('Thread %s: skipped subagent, parent=%s' % (tid, thread['parentThreadId']))
                        continue
                    status=thread.get('status') or {}; kind=status.get('type'); flags=status.get('activeFlags') or []
                    title=thread.get('name') or thread.get('preview') or tid
                    log('Thread %s: status=%s flags=%s tracked=%s' % (tid, kind, flags, tid in tracked))
                    if kind=='active' and not flags:
                        if tid not in tracked: print('Tracking:',title,tid,flush=True)
                        if tid not in tracked:
                            latest = rpc.call('thread/turns/list', {'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                            # Only a currently running turn, or a later turn, may be recovered.
                            tracked[tid] = (latest[0].get('id'), latest[0].get('status')) if latest else (None, None)
                            log('Thread %s baseline: turn=%s status=%s' % (tid, *tracked[tid]))
                    if tid not in tracked:
                        log('Thread %s: skipped, not observed working' % tid); continue
                    if kind not in ('idle','systemError'):
                        log('Thread %s: skipped, still active or waiting for input' % tid); continue
                    turns=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                    if not turns:
                        log('Thread %s: no turn returned' % tid); continue
                    turn=turns[0]; turn_id=turn.get('id'); key=(tid,turn_id)
                    baseline_id, baseline_status = tracked[tid]
                    log('Thread %s latest turn=%s status=%s error=%s' % (tid, turn_id, turn.get('status'), turn.get('error')))
                    if turn_id == baseline_id and baseline_status != 'inProgress':
                        log('Thread %s: skipped baseline turn' % tid); continue
                    if turn.get('status')!='failed':
                        log('Thread %s: skipped, latest turn not failed' % tid); continue
                    if not turn_id:
                        log('Thread %s: skipped, missing turn ID' % tid); continue
                    if key in attempted:
                        log('Thread %s: skipped, already attempted this turn' % tid); continue
                    if blocked_error(turn.get('error')):
                        log('Thread %s: skipped, missing error or excluded error category' % tid); continue
                    # This is the latest failed turn of a thread observed working in this run.
                    try: goal=rpc.call('thread/goal/get',{'threadId':tid}).get('goal')
                    except RPCError as e:
                        if 'goals feature is disabled' in str(e).lower(): goal=None
                        else: raise
                    log('Thread %s goal status=%s' % (tid, goal.get('status') if goal else 'none'))
                    action='resume goal' if goal and goal.get('status')!='complete' else 'Continue'
                    print(('DRY RUN: ' if dry_run else '')+action+' for '+str(title)+' ('+tid+', turn '+turn_id+')',flush=True)
                    attempted.add(key) # never duplicate on ambiguous timeout
                    log('Thread %s: recorded attempt for turn %s' % (tid, turn_id))
                    if not dry_run:
                        # Recheck just before mutation; leave user-input and active states alone.
                        fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                        if fresh.get('status',{}).get('type') not in ('idle','systemError'): print('Skipped: status changed',flush=True); continue
                        latest=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                        if not latest or latest[0].get('id')!=turn_id or latest[0].get('status')!='failed': print('Skipped: turn changed',flush=True); continue
                        log('Thread %s: recheck passed; submitting %s' % (tid, action))
                        if goal and goal.get('status')!='complete':
                            rpc.call('thread/goal/set',{'threadId':tid,'status':'active','origin':'user'})
                        else:
                            rpc.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':'Continue','text_elements':[]}]})
                        log('Thread %s: recovery request accepted' % tid)
                except RPCError as e: print('Thread',tid,'skipped:',e,flush=True)
            for missing in set(tracked)-set(ids): log('Thread %s: no longer loaded; removing from tracking' % missing)
            tracked={k:v for k,v in tracked.items() if k in ids}
        except RPCError as e:
            print('Polling failed:',e,flush=True)
            if isinstance(rpc,RPC) and rpc.ws is None: raise
        delay=max(0.1,interval-(time.monotonic()-start))
        log('Poll complete; sleeping %.2fs' % delay)
        time.sleep(delay)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    home=os.environ.get('CODEX_HOME',os.path.expanduser('~/.codex'))
    p.add_argument('--socket',default=os.path.join(home,'app-server-control','app-server-control.sock'),help='shared daemon Unix socket path')
    p.add_argument('--url',help='optional local ws:// endpoint instead of the Unix socket')
    p.add_argument('--timeout',type=float,default=30,help='connection and RPC timeout in seconds')
    p.add_argument('--check',action='store_true',help='initialize, list loaded thread IDs, and exit without recovery')
    p.add_argument('--interval',type=float,default=5)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('-v','--verbose',action='store_true',help='show detailed polling, decisions, and RPC activity')
    a=p.parse_args()
    global VERBOSE
    VERBOSE=a.verbose
    log('Options: dry_run=%s interval=%s socket=%s' % (a.dry_run,a.interval,a.socket))
    if a.interval<=0 or a.timeout<=0: p.error('--interval and --timeout must be positive')
    rpc=None
    try:
        rpc=RPC(a.url or os.path.expanduser(a.socket),a.timeout)
        if a.check:
            ids=list(pages(rpc,'thread/loaded/list',{'limit':100}))
            print('Connection OK. Loaded threads (%d): %s' % (len(ids), ', '.join(ids) or 'none'))
        else: run(rpc,a.dry_run,a.interval)
    except (RPCError,OSError,BrokenPipeError) as e: print('Error:',e,file=sys.stderr); return 1
    except KeyboardInterrupt: print('\nStopped.')
    finally:
        if rpc: rpc.close()
    return 0
if __name__=='__main__': sys.exit(main())
