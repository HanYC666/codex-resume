#!/usr/bin/env python3
"""Monitor active Codex daemon threads and recover failed turns with per-thread retry backoff."""
import argparse, json, os, re, socket, sys, time

VERBOSE = False
def log(message):
    if VERBOSE:
        safe = re.sub(r"\b(?:sk|Bearer)[-_ ][A-Za-z0-9._-]{12,}", "[redacted]", str(message), flags=re.I)
        print(time.strftime("%H:%M:%S"), "DEBUG", safe, flush=True)

class RPCError(Exception): pass
class RPCRejected(RPCError): pass
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
            self.call('initialize', {'clientInfo': {'name':'codex-recovery','title':'Codex recovery','version':'0.4'}, 'capabilities': {'experimentalApi':True,'requestAttestation':False}})
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
                    raise RPCRejected(str(item['error']))
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
    return any(x in s for x in ('cybersecurity','safety check','approval required','permission denied'))

BACKOFF_SECONDS = 15 * 60

def error_identity(error):
    """Ignore per-request IDs while keeping distinct failure categories."""
    message = str(error.get('message', '') if isinstance(error, dict) else error).lower()
    if 'model_not_found' in message:
        model = re.search(r"['\"]([a-z0-9._-]+)['\"] model", message)
        return 'model_not_found:' + (model.group(1) if model else 'unknown')
    if 'no account is available for the upstream' in message:
        return 'compaction_upstream_unavailable'
    if any(x in message for x in ('usage limit', 'usage_limit', 'quota', 'rate limit')):
        return 'quota_or_rate_limit'
    status = re.search(r'(?:unexpected status|last status:)\s*(\d{3})', message)
    if status: return 'http_' + status.group(1)
    info = error.get('codexErrorInfo') if isinstance(error, dict) else None
    if info and info != 'other': return json.dumps(info, sort_keys=True)
    for x in ('stream disconnected', 'timed out', 'connection reset', 'connection closed'):
        if x in message: return x
    message = re.sub(r'(?:request id|request_id|cf-ray)[=: ]+\S+', '', message)
    return re.sub(r'\s+', ' ', message).strip()

class RecoveryState:
    def __init__(self):
        self.seen_failures = set()
        self.attempted_turns = set()
        self.consecutive = 0
        self.same_error = 0
        self.last_error = None
        self.last_failed_id = None
        self.last_attempt = None
        self.slow = False
        self.next_retry = None

    def completed_since_failure(self, turns):
        if not self.last_failed_id: return False
        ids = [t.get('id') for t in turns]
        if self.last_failed_id in ids:
            newer = turns[:ids.index(self.last_failed_id)]
        elif turns and turns[0].get('status') == 'completed':
            newer = turns[:1]
        else: return False
        return any(t.get('status') == 'completed' for t in newer)

    def reset(self):
        # Keep deduplication history, but a successful turn breaks the error streak.
        self.consecutive = self.same_error = 0
        self.last_error = self.last_failed_id = None
        self.slow = False
        self.next_retry = None

    def failure(self, turn, now):
        tid = turn['id']
        if tid in self.seen_failures: return False
        self.seen_failures.add(tid)
        self.consecutive += 1
        identity = error_identity(turn['error'])
        self.same_error = self.same_error + 1 if identity == self.last_error else 1
        self.last_error = identity
        self.last_failed_id = tid
        if not self.slow and (self.same_error >= 2 or self.consecutive >= 3):
            self.slow = True
            self.next_retry = (self.last_attempt if self.last_attempt is not None else now) + BACKOFF_SECONDS
            return True
        return False

    def ready(self, turn_id, now):
        if self.slow: return now >= self.next_retry
        return turn_id not in self.attempted_turns

    def record(self, turn_id, now):
        self.attempted_turns.add(turn_id)
        self.last_attempt = now
        if self.slow: self.next_retry = now + BACKOFF_SECONDS

class RunStats:
    """Process-local counters; accepted submissions are not assumed successful."""
    def __init__(self, history):
        self.history = history
        self.errors = set()
        self.baseline_failures = set()
        self.started = time.monotonic()

    def baseline(self, tid, turn_id, status):
        if status == 'failed': self.baseline_failures.add((tid,turn_id))

    def observe(self, tid, turns):
        if not turns: return
        latest=turns[0]
        key=(tid,latest.get('id'))
        if latest.get('status')=='failed' and key[1] and key not in self.baseline_failures:
            self.errors.add(key)
        for entry in self.history:
            if entry['thread_id']!=tid or entry.get('result')!='pending': continue
            candidate=None
            if entry.get('recovery_turn_id'):
                candidate=next((t for t in turns if t.get('id')==entry['recovery_turn_id']),None)
            else:
                ids=[t.get('id') for t in turns]
                if entry['turn_id'] in ids:
                    newer=turns[:ids.index(entry['turn_id'])]
                    if newer: candidate=newer[-1] # first subsequent turn, not an unrelated later success
            if candidate:
                status=candidate.get('status')
                if status=='completed': entry['result']='success'
                elif status=='failed': entry['result']='failed'
                elif status=='interrupted': entry['result']='interrupted'

    def record(self, entry):
        # A later retry must not let multiple pending attempts claim one success.
        for previous in self.history:
            if previous["thread_id"]==entry["thread_id"] and previous.get("result")=="pending":
                previous["result"]="unknown"
        self.history.append(entry)

    def print_summary(self):
        print('\nRun summary (%.0f seconds)' % (time.monotonic()-self.started),flush=True)
        print('Distinct errors observed: %d (failed turns; repeated polls count once)' % len(self.errors),flush=True)
        live_total=sum(e['outcome']!='dry-run' for e in self.history)
        print('Recovery commands attempted: %d' % live_total,flush=True)
        print('%-17s %8s %10s %8s %10s %11s %8s' % ('Recovery','Attempts','Successful','Failed','Unresolved','Interrupted','Dry-run'),flush=True)
        for action,label in (('Continue','Continue'),('resume goal','Goal resume')):
            entries=[e for e in self.history if e['action']==action]
            live=[e for e in entries if e['outcome']!='dry-run']
            counts=[len(live),sum(e.get('result')=='success' for e in live),
                    sum(e.get('result')=='failed' for e in live),
                    sum(e.get('result') in ('pending','unknown') for e in live),
                    sum(e.get('result')=='interrupted' for e in live),len(entries)-len(live)]
            print('%-17s %8d %10d %8d %10d %11d %8d' % (label,*counts),flush=True)
        print('Successful = observed a completed recovery turn; failed = next turn failed or submission rejected.',flush=True)
        print('Unresolved = still running, not yet observed, or submission outcome unknown. Dry-run does not send commands.',flush=True)

def pages(rpc, method, params):
    cursor=None; seen=set()
    while True:
        r=rpc.call(method,dict(params,cursor=cursor)); yield from r.get('data',[])
        cursor=r.get('nextCursor')
        if not cursor: return
        if cursor in seen: raise RPCError('repeated pagination cursor')
        seen.add(cursor)

def run(rpc, dry_run, interval, stats=None):
    stats=stats if stats is not None else RunStats([])
    tracked={}; states={}; history=stats.history # Process-local, discarded on exit.
    print('Monitoring active root threads from the shared Codex daemon. Ctrl-C to stop.', flush=True)
    try:
        while True:
            start=time.monotonic()
            log('Beginning poll; tracked=%d recovery_records=%d' % (len(tracked), len(history)))
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
                                stats.baseline(tid,*tracked[tid])
                                log('Thread %s baseline: turn=%s status=%s' % (tid, *tracked[tid]))
                        if tid not in tracked:
                            log('Thread %s: skipped, not observed working' % tid); continue
                        state=states.setdefault(tid, RecoveryState())
                        turns=rpc.call('thread/turns/list',{'threadId':tid,'limit':10,'sortDirection':'desc'}).get('data',[])
                        stats.observe(tid,turns)
                        if state.completed_since_failure(turns):
                            state.reset()
                            print('Thread %s: successful turn observed; normal retry rate restored' % title,flush=True)
                        if kind not in ('idle','systemError') or flags:
                            log('Thread %s: skipped, still active or waiting for input' % tid); continue
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
                        if blocked_error(turn.get('error')):
                            log('Thread %s: skipped, missing error or excluded error category' % tid); continue
                        now=time.monotonic()
                        if state.failure(turn, now):
                            print('Thread %s: entering 15-minute retry mode (%d consecutive errors; %d matching)' % (title,state.consecutive,state.same_error),flush=True)
                        if not state.ready(turn_id, now):
                            if state.slow:
                                log('Thread %s: backoff; next retry in %.1fs' % (tid,max(0,state.next_retry-now)))
                            else: log('Thread %s: skipped, already attempted this turn' % tid)
                            continue
                        # This is the latest failed turn of a thread observed working in this run.
                        try: goal=rpc.call('thread/goal/get',{'threadId':tid}).get('goal')
                        except RPCError as e:
                            if 'goals feature is disabled' in str(e).lower(): goal=None
                            else: raise
                        log('Thread %s goal status=%s' % (tid, goal.get('status') if goal else 'none'))
                        action='resume goal' if goal and goal.get('status')!='complete' else 'Continue'
                        print(('DRY RUN: ' if dry_run else '')+action+' for '+str(title)+' ('+tid+', turn '+turn_id+')',flush=True)
                        if not dry_run:
                            # Recheck just before mutation; leave user-input and active states alone.
                            fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                            if fresh.get('status',{}).get('type') not in ('idle','systemError'): print('Skipped: status changed',flush=True); continue
                            latest=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                            if not latest or latest[0].get('id')!=turn_id or latest[0].get('status')!='failed': print('Skipped: turn changed',flush=True); continue
                        # Record only after the preflight checks. Failures with ambiguous
                        # submission outcomes are retained as attempts, never sent twice rapidly.
                        state.record(turn_id, time.monotonic())
                        entry={'time':time.time(),'thread_id':tid,'turn_id':turn_id,
                               'action':action,'error':state.last_error,'slow':state.slow,
                               'outcome':'dry-run' if dry_run else 'pending',
                               'result':'dry-run' if dry_run else 'pending'}
                        stats.record(entry)
                        log('Recovery record #%d: %s' % (len(history), entry))
                        if not dry_run:
                            log('Thread %s: recheck passed; submitting %s' % (tid, action))
                            try:
                                if goal and goal.get('status')!='complete':
                                    rpc.call('thread/goal/set',{'threadId':tid,'status':'active','origin':'user'})
                                else:
                                    response=rpc.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':'Continue','text_elements':[]}]})
                                    entry['recovery_turn_id']=(response.get('turn') or {}).get('id')
                                entry['outcome']='accepted'
                            except RPCRejected:
                                entry['outcome']='rejected'
                                entry['result']='failed'
                                raise
                            except RPCError:
                                entry['outcome']='error-or-unknown'
                                entry['result']='unknown'
                                raise
                            log('Thread %s: recovery request accepted' % tid)
                    except RPCError as e: print('Thread',tid,'skipped:',e,flush=True)
                for missing in set(tracked)-set(ids): log('Thread %s: no longer loaded; removing from tracking' % missing)
                tracked={k:v for k,v in tracked.items() if k in ids}
            except RPCError as e:
                print('Polling failed:',e,flush=True)
                if isinstance(rpc,RPC) and rpc.ws is None: raise
            delay=max(0.1,interval-(time.monotonic()-start))
            deadlines=[st.next_retry for st in states.values() if st.slow and st.next_retry is not None and st.next_retry > time.monotonic()]
            if deadlines: delay=min(delay,max(0.1,min(deadlines)-time.monotonic()))
            log('Poll complete; sleeping %.2fs' % delay)
            time.sleep(delay)
    finally:
        stats.print_summary()

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
    stats=RunStats([])
    monitoring=False
    try:
        rpc=RPC(a.url or os.path.expanduser(a.socket),a.timeout)
        if a.check:
            ids=list(pages(rpc,'thread/loaded/list',{'limit':100}))
            print('Connection OK. Loaded threads (%d): %s' % (len(ids), ', '.join(ids) or 'none'))
        else:
            monitoring=True
            run(rpc,a.dry_run,a.interval,stats)
    except (RPCError,OSError,BrokenPipeError) as e: print('Error:',e,file=sys.stderr); return 1
    except KeyboardInterrupt:
        print('\nStopped.')
        if not monitoring: stats.print_summary()
    finally:
        if rpc: rpc.close()
    return 0
if __name__=='__main__': sys.exit(main())
