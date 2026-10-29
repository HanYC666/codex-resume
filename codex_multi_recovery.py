#!/usr/bin/env python3
"""Monitor active Codex daemon threads and recover failed turns with per-thread retry backoff."""
import argparse, json, os, re, socket, sys, threading, time, queue

VERBOSE = False

def plain(value):
    # Titles and provider errors are untrusted terminal text, not markup/escapes.
    value=re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    value=re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value=re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    return re.sub(r"\b(?:sk|Bearer)[-_ ][A-Za-z0-9._-]{12,}", "[redacted]", value, flags=re.I)

def event(*values, sep=' ', file=None, flush=True):
    print('['+time.strftime('%Y-%m-%d %H:%M:%S%z')+']',
          sep.join(plain(v) for v in values), file=file, flush=flush)

def log(message):
    if VERBOSE: event('DEBUG', message)

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
            self.call('initialize', {'clientInfo': {'name':'codex-recovery','title':'Codex recovery','version':'0.6'}, 'capabilities': {'experimentalApi':True,'requestAttestation':False}})
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
        callback=getattr(self,'on_activity',None)
        if callback: callback('RPC: '+method+' ['+str(n)+']')
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

MODEL_FALLBACKS = {'gpt-6.1-sol':'gpt-6-sol', 'gpt-6-astra':'gpt-6-sol'}

def configuration_update_error(error):
    """Provider-specific trigger; this error alone does not establish a usage cap."""
    text=json.dumps(error,ensure_ascii=False).lower()
    return 'configuration_update' in text and ('invalid input type' in text or 'invalid_input_type' in text)

def read_goal(rpc, tid):
    try: return rpc.call('thread/goal/get',{'threadId':tid}).get('goal')
    except RPCRejected as exc:
        if 'goals feature is disabled' in str(exc).lower(): return None
        raise

def stop_recovery(rpc, tid, state, goal, reason, dry_run):
    state.stopped=reason
    state.next_retry=None
    event(('DRY RUN: ' if dry_run else '')+'Thread '+tid+': automatic recovery stopped — '+reason)
    # The triggering turn is already failed. Pause its Goal to stop autonomous retries.
    if goal and goal.get('status')!='complete':
        if not dry_run:
            fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
            status=fresh.get('status') or {}
            if status.get('type') not in ('idle','systemError') or status.get('activeFlags'):
                return  # A manual resume is already working; leave it alone.
            current=read_goal(rpc,tid)
            if not current or current.get('status')=='complete':
                state.goal_status=current.get('status') if current else None
                return
            rpc.call('thread/goal/set',{'threadId':tid,'status':'paused','origin':'user'})
        state.goal_status='paused'


def pause_conversation(rpc, tid, state, dry_run):
    """Pause autonomous Goal scheduling before interrupting the current turn."""
    state.user_paused=True
    state.pause_error=None
    try:
        goal=read_goal(rpc,tid)
        if goal and goal.get('status')!='complete':
            if not dry_run: rpc.call('thread/goal/set',{'threadId':tid,'status':'paused','origin':'user'})
            state.goal_status='paused' if not dry_run else goal.get('status')
        turns=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
        if turns and turns[0].get('status')=='inProgress':
            if not dry_run: rpc.call('turn/interrupt',{'threadId':tid,'turnId':turns[0]['id']})
        event(('DRY RUN: would pause conversation ' if dry_run else 'Conversation paused: ')+tid)
    except RPCError as exc:
        state.pause_error=str(exc)
        event('Pause could not be confirmed for '+tid+': '+str(exc))
        raise


def resume_conversation(rpc, tid, state, stats, dry_run):
    fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
    status=fresh.get('status') or {}
    if status.get('type')=='active' or status.get('activeFlags'):
        state.user_paused=False;state.pause_error=None
        event('Resume skipped: conversation already active or awaiting input: '+tid)
        return
    goal=read_goal(rpc,tid)
    turns=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
    action='resume goal' if goal and goal.get('status')!='complete' else 'Continue'
    if action=='Continue' and (not turns or turns[0].get('status') not in ('interrupted','failed')):
        state.user_paused=False;state.pause_error=None
        event('No interrupted or failed turn to resume: '+tid)
        return
    entry={'time':time.time(),'thread_id':tid,'turn_id':turns[0].get('id') if turns else None,
           'action':action,'error':None,'slow':state.slow,'manual':True,
           'outcome':'dry-run' if dry_run else 'pending','result':'dry-run' if dry_run else 'pending'}
    stats.record(entry)
    state.record(entry['turn_id'],time.monotonic())
    try:
        if not dry_run:
            if action=='resume goal': rpc.call('thread/goal/set',{'threadId':tid,'status':'active','origin':'user'})
            else:
                result=rpc.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':'Continue','text_elements':[]}]})
                entry['recovery_turn_id']=(result.get('turn') or {}).get('id')
            entry['outcome']='accepted'
        state.user_paused=False;state.pause_error=None
        event(('DRY RUN: ' if dry_run else '')+action+' for paused conversation '+tid)
    except RPCError as exc:
        entry['outcome']='rejected' if isinstance(exc,RPCRejected) else 'error-or-unknown'
        entry['result']='failed' if isinstance(exc,RPCRejected) else 'unknown'
        state.pause_error=str(exc)
        raise


def fallback_effort(rpc, thread):
    effort=thread.get('reasoningEffort')
    if effort: return effort
    # An unset effort means the model default; resolve it rather than silently
    # accepting a possibly different default on the fallback model.
    for model in pages(rpc,'model/list',{'limit':100}):
        if thread.get('model') in (model.get('id'),model.get('model')):
            return model.get('defaultReasoningEffort')
    return None

def error_identity(error):
    """Ignore per-request IDs while keeping distinct failure categories."""
    if configuration_update_error(error): return 'invalid_configuration_update_input'
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

def productive_current_turn(turn):
    """Only the latest meaningful item of the current running turn is evidence."""
    if turn.get('status')!='inProgress' or turn.get('error'): return False
    items=turn.get('items') or []
    for item in reversed(items):
        kind=item.get('type')
        if kind in ('userMessage','reasoning','hookPrompt'): continue
        if item.get('error') or item.get('status') in ('failed','declined'): return False
        if kind=='agentMessage':
            text=str(item.get('text') or '').strip()
            try:
                value=json.loads(text.lstrip('■ ').strip())
                if isinstance(value,dict) and 'error' in value:return False
            except ValueError:pass
            if any(x in text.lower() for x in ('usage limit reached','stream disconnected before completion','invalid input type')):return False
            return bool(text)
        if kind in ('commandExecution','mcpToolCall','dynamicToolCall','fileChange'):
            return item.get('status')=='completed' and item.get('exitCode') in (None,0) and item.get('success') is not False
        return False
    return False

def turn_order(value):
    # Codex turn IDs are UUIDv7; their first 48 bits are a creation timestamp.
    import uuid
    try:
        parsed=uuid.UUID(str(value))
        return parsed.int>>80 if parsed.version==7 else None
    except ValueError:return None

class RecoveryState:
    def __init__(self):
        self.seen_failures = set()
        self.attempted_turns = set()
        self.consecutive = 0
        self.same_error = 0
        self.last_error = None
        self.last_failed_id = None
        self.failure_started = None
        self.last_attempt = None
        self.slow = False
        self.next_retry = None
        self.model = None
        self.effort = None
        self.goal_status = 'unknown'
        self.stopped = None
        self.user_paused = False
        self.pause_error = None
        self.fallback_target = None
        self.fallback_effort = None
        self.fallback_turn = None
        self.fallback_confirmed = False
        self.fallback_checks = 0
        self.fallback_recovery_sent = False

    def completed_since_failure(self, turns):
        if not self.last_failed_id: return False
        ids = [t.get('id') for t in turns]
        if self.last_failed_id in ids:
            newer = turns[:ids.index(self.last_failed_id)]
        else:
            anchor=turn_order(self.last_failed_id)
            newer=[t for t in turns if (anchor is not None and turn_order(t.get('id')) is not None and turn_order(t['id'])>anchor)
                   or (self.failure_started is not None and t.get('startedAt') is not None and t['startedAt']>self.failure_started)]
        return any(t.get('status') == 'completed' for t in newer)

    def reset(self):
        # Keep deduplication history, but a successful turn breaks the error streak.
        self.consecutive = self.same_error = 0
        self.last_error = self.last_failed_id = None
        self.failure_started = None
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
        self.failure_started = turn.get('startedAt')
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

def duration(seconds):
    seconds=max(0,int(seconds))
    hours, seconds=divmod(seconds,3600)
    minutes, seconds=divmod(seconds,60)
    return ('%dh %02dm' % (hours,minutes)) if hours else '%02d:%02d' % (minutes,seconds)

class Keyboard:
    """POSIX cbreak input; the monitor remains the only RPC writer."""
    def __init__(self, dashboard):
        self.dashboard=dashboard; self.commands=queue.Queue(); self.wake=threading.Event()
        self.done=threading.Event(); self.saved=None; self.thread=None
    def start(self):
        if getattr(self.dashboard,'terminal',None):
            self.dashboard.terminal.controls=self
            self.dashboard.interactive=True
            return
        if not self.dashboard.enabled or not sys.stdin.isatty(): return
        import termios, tty
        self.fd=sys.stdin.fileno(); self.saved=termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.thread=threading.Thread(target=self.read,daemon=True); self.thread.start()
        self.dashboard.interactive=True
    def read(self):
        import select
        buffer=b''
        while not self.done.is_set():
            if not select.select([self.fd],[],[],.1)[0]:
                if buffer==b'\x1b':
                    with self.dashboard.lock: self.dashboard.help_open=False
                    buffer=b''
                continue
            data=os.read(self.fd,128)
            if not data: return
            buffer+=data
            while buffer:
                if buffer in (b'\x1b',b'\x1b[',b'\x1bO'): break
                if buffer.startswith((b'\x1b[A',b'\x1bOA')):
                    self.dashboard.move(-1); buffer=buffer[3:]; continue
                if buffer.startswith((b'\x1b[B',b'\x1bOB')):
                    self.dashboard.move(1); buffer=buffer[3:]; continue
                key=buffer[:1];buffer=buffer[1:]
                if key==b'?':
                    with self.dashboard.lock: self.dashboard.help_open=not self.dashboard.help_open
                elif key==b'\x1b':
                    with self.dashboard.lock: self.dashboard.help_open=False
                elif key.lower() in (b'r',b'p') or key in (b'\x7f',b'\x08'):
                    with self.dashboard.lock: tid=self.dashboard.selected
                    if tid:
                        action='untrack' if key in (b'\x7f',b'\x08') else 'retry' if key.lower()==b'r' else 'pause'
                        self.commands.put((action,tid));self.wake.set()
                elif key.lower()==b'q': self.commands.put(('quit',None));self.wake.set()
    def close(self):
        self.done.set()
        if self.thread: self.thread.join(timeout=.3)
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd,termios.TCSADRAIN,self.saved)
        self.dashboard.interactive=False


class Dashboard:
    """A bottom-of-output live panel; immutable row snapshots cross threads."""
    def __init__(self, interval, dry_run=False, enabled=True, console=None):
        self.interval=interval
        self.dry_run=dry_run
        self.enabled=enabled and sys.stdout.isatty() and os.environ.get('TERM')!='dumb'
        self.console=console
        self.live=None
        self.terminal=None
        self.lock=threading.RLock()
        self.rows={}
        self.selected=None; self.help_open=False; self.interactive=False
        self.errors=self.attempts=self.successes=self.failures=0
        self.pending=self.plans=0
        self.last_poll=None
        self.next_poll=None
        self.activity='Starting monitor'
        self.polls=0
        self.started=time.monotonic()

    def move(self, delta):
        with self.lock:
            ids=[tid for tid,row in sorted(self.rows.items(),key=lambda item:(self.presentation(item[1])[2],item[1]['title'].casefold(),item[0]))]
            if not ids: self.selected=None; return
            index=ids.index(self.selected) if self.selected in ids else (-1 if delta>0 else len(ids))
            self.selected=ids[max(0,min(len(ids)-1,index+delta))]

    def start(self):
        if not self.enabled: return
        if sys.stdin.isatty():
            try: from terminal_ui import TerminalUI
            except ImportError: raise RPCError('Install dependencies: python -m pip install -r requirements.txt')
            self.terminal=TerminalUI(self)
            self.terminal.start()
            return
        from rich.console import Console
        from rich.live import Live
        self.console=self.console or Console(markup=False,highlight=False)
        self.live=Live(console=self.console,get_renderable=self.render,refresh_per_second=4,
                       transient=True,redirect_stdout=True,redirect_stderr=True)
        self.live.start()

    def stop(self):
        if self.terminal:
            self.terminal.stop();self.terminal=None
        if self.live is not None:
            self.live.stop();self.live=None

    def set_activity(self, value):
        with self.lock: self.activity=plain(value)

    def begin_poll(self):
        with self.lock:
            self.polls+=1
            self.activity='Polling threads'
            self.next_poll=None

    def update_thread(self, tid, title, status, turns, state, stats):
        entries=[e for e in stats.history if e['thread_id']==tid]
        live=[e for e in entries if e['outcome']!='dry-run']
        row={'title':plain(title), 'kind':status.get('type'),
             'flags':tuple(status.get('activeFlags') or []),
             'turn':turns[0].get('status') if turns else None,
             'attempts':len(live),'successes':sum(e.get('result')=='success' for e in live),
             'failures':sum(e.get('result')=='failed' for e in live),
             'unresolved':sum(e.get('result') in ('pending','unknown') for e in live),
             'plans':len(entries)-len(live),'slow':bool(state and state.slow),
             'attempted_latest':bool(state and turns and turns[0].get('id') in state.attempted_turns),
             'excluded':bool(turns and turns[0].get('status')=='failed' and blocked_error(turns[0].get('error'))),
             'deadline':state.next_retry if state else None,
             'model':state.model if state else None, 'effort':state.effort if state else None,
             'goal_status':state.goal_status if state else 'unknown',
             'user_paused':bool(state and state.user_paused),
             'pause_error':state.pause_error if state else None,
             'stopped':state.stopped if state else None,
             'switching':bool(state and state.fallback_target and not state.fallback_confirmed and not state.stopped and not self.dry_run)}
        with self.lock:
            self.rows[tid]=row
            self.errors=len(stats.errors)
            sent=[e for e in stats.history if e['outcome']!='dry-run']
            self.attempts=len(sent)
            self.successes=sum(e.get('result')=='success' for e in sent)
            self.failures=sum(e.get('result')=='failed' for e in sent)
            self.pending=sum(e.get('result') in ('pending','unknown') for e in sent)
            self.plans=len(stats.history)-len(sent)

    def finish_poll(self, loaded, delay, failed=False):
        with self.lock:
            if loaded is not None: self.rows={k:v for k,v in self.rows.items() if k in loaded}
            if not failed: self.last_poll=time.monotonic()
            self.next_poll=time.monotonic()+delay
            self.activity='Poll failed; waiting to retry' if failed else 'Waiting for next poll'

    @staticmethod
    def presentation(row):
        """State text, colour and stable display priority; colour is never the only cue."""
        if row.get('pause_error'): return 'Pause error', 'bold red', 0
        if row.get('user_paused'): return 'Paused', 'bold yellow', 0
        if row.get('stopped'): return 'Stopped', 'bold red', 0
        if row.get('switching'): return 'Switching', 'bold yellow', 1
        if row['flags']:
            label='Approval' if 'waitingOnApproval' in row['flags'] else 'Needs input'
            return label, 'bold yellow', 0
        if row['kind']=='active': return 'Working', 'bold cyan', 3
        if row['excluded']: return 'Attention', 'bold red', 0
        if row['kind']=='idle' and row['turn']=='completed': return 'Ready', 'green', 4
        if row['slow']: return 'Backoff', 'yellow', 2
        if row['kind']=='systemError' or row['turn']=='failed': return 'Error', 'bold red', 1
        return 'Paused', 'dim', 4

    @staticmethod
    def short_ids(ids):
        # UUID prefixes often share a timestamp; choose unique suffixes instead.
        result={}
        for tid in ids:
            size=min(4,len(tid))
            while size<len(tid) and any(other!=tid and other.endswith(tid[-size:]) for other in ids):
                size+=1
            result[tid]=tid[-size:]
        return result

    def render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        with self.lock:
            rows=list(self.rows.items());selected=self.selected
            errors,attempts,successes,failures=self.errors,self.attempts,self.successes,self.failures
        width,height=self.console.size if self.console else (100,20)
        height=min(20,max(3,height));inner=max(1,width-4)
        def line(text,style=''):
            return Text(text,style=style,no_wrap=True,overflow='ellipsis')
        if self.help_open:
            help=['Keyboard help · Selected: '+(selected or 'none'),
                  '↑/↓ select · R retry now · P pause/resume Codex',
                  'Backspace untrack · ?/Esc close help / clear selection',
                  'Drag to copy · C or Ctrl+Y copy selection',
                  'F2 native selection: use Cmd+C / terminal Copy',
                  'PgUp/PgDn/wheel history · ←/→ pan columns',
                  'Home/End oldest/latest · Q exit',
                  'Ctrl+C copies selection; otherwise exits']
            return Panel(Group(*(line(x) for x in help[:max(1,height-2)])),border_style='cyan')
        rows.sort(key=lambda item:(self.presentation(item[1])[2],item[1]['title'].casefold(),item[0]))
        ids=self.short_ids([tid for tid,_ in rows]);capacity=max(1,height-8)
        pages=max(1,(len(rows)+capacity-1)//capacity)
        page=int(max(0,time.monotonic()-self.started)//8)%pages
        if selected in ids:page=next(i for i,(tid,_) in enumerate(rows) if tid==selected)//capacity
        active=sum(r['kind']=='active' and not r['flags'] for _,r in rows)
        ready=sum(self.presentation(r)[0]=='Ready' for _,r in rows)
        header=line('Active %d · Paused %d · Ready %d | Sent %d · Done %d · Failed %d · Pending %d' %
                    (active,len(rows)-active-ready,ready,attempts,successes,failures,self.pending),'bold cyan')
        table=Table(box=None,padding=(0,1),collapse_padding=True,expand=True,header_style='dim')
        for label,ratio in [('THREAD',2),('MODEL',2),('DEPTH',1),('MODE',1),('STATE',1),('TRY/OK/FAIL/WAIT',1),('RETRY/NEXT',2)]:
            fixed={'MODEL':14,'DEPTH':6,'MODE':13,'STATE':12,'TRY/OK/FAIL/WAIT':17,'RETRY/NEXT':18}
            table.add_column(label,ratio=ratio if label=='THREAD' or width<110 else None,width=fixed.get(label) if width>=110 else None,no_wrap=True,overflow='ellipsis')
        now=time.monotonic()
        for tid,row in rows[page*capacity:(page+1)*capacity]:
            status,style,_=self.presentation(row)
            goal=row.get('goal_status')
            mode='Prompt' if goal in (None,'complete') else 'Mode ?' if goal=='unknown' else 'Goal '+str(goal)
            retry='15m/'+duration(row['deadline']-now) if row['slow'] and row['deadline'] is not None else 'Now / Next poll' if row['turn']=='failed' else 'Immediate / —'
            if row['kind']=='active' or row['flags']:retry=('15m' if row['slow'] else 'Immediate')+' / —'
            elif row.get('attempted_latest') and not row['slow']:retry='Awaiting new turn'
            if row.get('stopped'):retry='Auto off'
            if row.get('user_paused'):retry='Paused by you'
            title=('› ' if tid==selected else '')+'#'+ids[tid]+' '+(row['title'] or 'Untitled')
            table.add_row(line(title,'bold cyan' if tid==selected else 'bold'),line(row.get('model') or 'Model ?'),
                line(row.get('effort') or 'default'),line(mode,'magenta'),line(status,style),
                line('%d/%d/%d/%d'%(row['plans'] if self.dry_run else row['attempts'],row['successes'],row['failures'],row['unresolved'])),line(retry,'yellow' if row['slow'] else 'dim'))
        lines=[header,table if rows else line('Waiting for a working thread')]
        if self.dry_run:lines.append(line('DRY RUN · nothing sent · %d plans'%self.plans,'yellow'))
        pager='Page %d/%d · rotates 8s'%(page+1,pages) if pages>1 else '%d threads'%len(rows)
        lines.append(line(pager+' · Errors %d · Poll %gs'%(errors,self.interval),'dim'))
        lines.append(line((selected or '↑/↓ select')+' · ? help · PgUp/PgDn history','dim'))
        return Panel(Group(*lines),title='Codex recovery'+(' · DRY RUN' if self.dry_run else ''),
                     border_style='cyan',subtitle='Ctrl+C summary')

def pages(rpc, method, params):
    cursor=None; seen=set()
    while True:
        r=rpc.call(method,dict(params,cursor=cursor)); yield from r.get('data',[])
        cursor=r.get('nextCursor')
        if not cursor: return
        if cursor in seen: raise RPCError('repeated pagination cursor')
        seen.add(cursor)

def run(rpc, dry_run, interval, stats=None, dashboard=None):
    stats=stats if stats is not None else RunStats([])
    dashboard=dashboard or Dashboard(interval,dry_run,enabled=False)
    keyboard=Keyboard(dashboard); suppressed=set(); forced=set()
    tracked={}; states={}; history=stats.history # Process-local, discarded on exit.
    try:
        dashboard.start()
        event('Monitoring active root threads from the shared Codex daemon. Ctrl-C to stop.', flush=True)
        keyboard.start()
        if isinstance(rpc,RPC): rpc.on_activity=dashboard.set_activity
        while True:
            if dashboard.terminal:dashboard.terminal.check_health()
            start=time.monotonic()
            keyboard.wake.clear()
            while not keyboard.commands.empty():
                command,chosen=keyboard.commands.get_nowait()
                if command=='quit': raise KeyboardInterrupt
                if chosen not in tracked: continue
                if command=='untrack':
                    suppressed.add(chosen);tracked.pop(chosen,None);forced.discard(chosen)
                    with dashboard.lock:
                        dashboard.rows.pop(chosen,None)
                        if dashboard.selected==chosen: dashboard.selected=None
                    event('Untracked:',chosen)
                elif command in ('pause','retry'):
                    st=states[chosen]
                    try:
                        if st.user_paused and not st.pause_error:
                            resume_conversation(rpc,chosen,st,stats,dry_run)
                        elif command=='pause': pause_conversation(rpc,chosen,st,dry_run)
                        else: forced.add(chosen)
                    except RPCError as exc: event('Conversation control failed:',chosen,exc)
            dashboard.begin_poll()
            ids=None
            poll_failed=False
            log('Beginning poll; tracked=%d recovery_records=%d' % (len(tracked), len(history)))
            try:
                ids=list(pages(rpc,'thread/loaded/list',{'limit':100}))
                log('Loaded threads (%d): %s' % (len(ids), ', '.join(ids) or 'none'))
                for tid in ids:
                    if tid in suppressed: continue
                    manual=tid in forced; forced.discard(tid)
                    thread=None; state=None; turns=[]; title=tid; status={}; goal=None
                    try:
                        thread=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                        if thread.get('parentThreadId'):
                            log('Thread %s: skipped subagent, parent=%s' % (tid, thread['parentThreadId']))
                            continue
                        status=thread.get('status') or {}; kind=status.get('type'); flags=status.get('activeFlags') or []
                        title=thread.get('name') or thread.get('preview') or tid
                        log('Thread %s: status=%s flags=%s tracked=%s' % (tid, kind, flags, tid in tracked))
                        if kind=='active' and not flags:
                            if tid not in tracked: event('Tracking:',title,tid,flush=True)
                            if tid not in tracked:
                                latest = rpc.call('thread/turns/list', {'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                                # Only a currently running turn, or a later turn, may be recovered.
                                tracked[tid] = (latest[0].get('id'), latest[0].get('status')) if latest else (None, None)
                                stats.baseline(tid,*tracked[tid])
                                log('Thread %s baseline: turn=%s status=%s' % (tid, *tracked[tid]))
                        if tid not in tracked:
                            log('Thread %s: skipped, not observed working' % tid); continue
                        state=states.setdefault(tid, RecoveryState())
                        state.model=thread.get('model')
                        state.effort=thread.get('reasoningEffort')
                        goal=read_goal(rpc,tid)
                        state.goal_status=goal.get('status') if goal else None
                        turns=rpc.call('thread/turns/list',{'threadId':tid,'limit':10,'sortDirection':'desc'}).get('data',[])
                        previous_errors=len(stats.errors)
                        stats.observe(tid,turns)
                        if len(stats.errors)>previous_errors:
                            event('Thread %s: failed turn detected (%s)' % (title,error_identity(turns[0].get('error'))))
                        if state.user_paused and not manual: continue
                        if state.stopped and not manual:
                            log('Thread %s: automatic recovery disabled: %s' % (tid,state.stopped)); continue
                        if state.fallback_target and not state.fallback_confirmed and not dry_run and not manual:
                            if state.model==state.fallback_target and state.effort==state.fallback_effort:
                                state.fallback_confirmed=True
                                event('Thread %s: fallback confirmed: %s / %s' % (title,state.model,state.effort))
                            else:
                                state.fallback_checks+=1
                                if state.fallback_checks>=3:
                                    stop_recovery(rpc,tid,state,goal,'model switch could not be confirmed',dry_run)
                                continue
                        progressed=False
                        if state.slow and kind=='active' and not flags and turns and turns[0].get('id')!=state.last_failed_id and not state.user_paused:
                            current=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc','itemsView':'full'}).get('data',[])
                            progressed=bool(current and current[0].get('id')==turns[0].get('id') and productive_current_turn(current[0]))
                        if progressed:
                            state.reset()
                            event('Thread %s: productive activity in current turn; normal retry rate restored (turn outcome still pending)' % title)
                        elif state.completed_since_failure(turns):
                            state.reset()
                            event('Thread %s: successful turn observed; normal retry rate restored' % title,flush=True)
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
                        special=configuration_update_error(turn.get('error'))
                        if not manual and special and state.fallback_confirmed and turn_id!=state.fallback_turn:
                            stop_recovery(rpc,tid,state,goal,'configuration_update error repeated after fallback; no retries',dry_run)
                            continue
                        if not manual and special and not state.fallback_target:
                            target=MODEL_FALLBACKS.get(state.model)
                            effort=fallback_effort(rpc,thread) if target else None
                            if not target or not effort:
                                stop_recovery(rpc,tid,state,goal,'no configured fallback or reasoning level unavailable',dry_run)
                                continue
                            fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                            latest=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                            fresh_status=fresh.get('status') or {}
                            if fresh_status.get('type') not in ('idle','systemError') or fresh_status.get('activeFlags'): continue
                            if not latest or latest[0].get('id')!=turn_id or latest[0].get('status')!='failed': continue
                            if fresh.get('model')!=state.model or fresh.get('reasoningEffort')!=state.effort: continue
                            state.fallback_target=target; state.fallback_effort=effort; state.fallback_turn=turn_id
                            event(('DRY RUN: ' if dry_run else '')+'Thread '+str(title)+': fallback '+state.model+' → '+target+' / '+effort)
                            if dry_run:
                                # Record the plan without pretending the server switched models.
                                state.fallback_recovery_sent=True
                            else:
                                try: rpc.call('thread/settings/update',{'threadId':tid,'model':target,'effort':effort})
                                except RPCError:
                                    state.stopped='model switch rejected or outcome unknown'
                                    raise
                                continue
                        if state.failure(turn, now):
                            event('Thread %s: entering 15-minute retry mode (%d consecutive errors; %d matching)' % (title,state.consecutive,state.same_error),flush=True)
                        switching_recovery=bool(not manual and special and state.fallback_confirmed and not state.fallback_recovery_sent)
                        if not manual and not switching_recovery and not state.ready(turn_id, now):
                            if state.slow:
                                log('Thread %s: backoff; next retry in %.1fs' % (tid,max(0,state.next_retry-now)))
                            else: log('Thread %s: skipped, already attempted this turn' % tid)
                            continue
                        log('Thread %s goal status=%s' % (tid, goal.get('status') if goal else 'none'))
                        action='resume goal' if goal and goal.get('status')!='complete' else 'Continue'
                        event(('DRY RUN: ' if dry_run else '')+action+' for '+str(title)+' ('+tid+', turn '+turn_id+')',flush=True)
                        if not dry_run:
                            # Recheck just before mutation; leave user-input and active states alone.
                            fresh=rpc.call('thread/read',{'threadId':tid,'includeTurns':False})['thread']
                            if fresh.get('status',{}).get('activeFlags') or fresh.get('status',{}).get('type') not in ('idle','systemError'): event('Skipped: status changed',flush=True); continue
                            if switching_recovery and (fresh.get('model')!=state.fallback_target or fresh.get('reasoningEffort')!=state.fallback_effort):
                                state.stopped='settings changed before fallback recovery'
                                event('Thread %s: recovery stopped; model/reasoning changed before submission' % title)
                                continue
                            latest=rpc.call('thread/turns/list',{'threadId':tid,'limit':1,'sortDirection':'desc'}).get('data',[])
                            if not latest or latest[0].get('id')!=turn_id or latest[0].get('status')!='failed': event('Skipped: turn changed',flush=True); continue
                        # Record only after the preflight checks. Failures with ambiguous
                        # submission outcomes are retained as attempts, never sent twice rapidly.
                        state.record(turn_id, time.monotonic())
                        if switching_recovery: state.fallback_recovery_sent=True
                        entry={'time':time.time(),'thread_id':tid,'turn_id':turn_id,
                               'action':action,'error':state.last_error,'slow':state.slow,
                               'model':state.model,'effort':state.effort,'fallback':switching_recovery,'manual':manual,
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
                    except RPCError as e: event('Thread',tid,'skipped:',e,flush=True)
                    finally:
                        if thread is not None and tid in tracked:
                            dashboard.update_thread(tid,title,status,turns,state,stats)
                for missing in set(tracked)-set(ids): log('Thread %s: no longer loaded; removing from tracking' % missing)
                tracked={k:v for k,v in tracked.items() if k in ids}
            except RPCError as e:
                poll_failed=True
                event('Polling failed:',e,flush=True)
                if isinstance(rpc,RPC) and rpc.ws is None: raise
            delay=max(0.1,interval-(time.monotonic()-start))
            deadlines=[st.next_retry for st in states.values() if st.slow and st.next_retry is not None and st.next_retry > time.monotonic()]
            if deadlines: delay=min(delay,max(0.1,min(deadlines)-time.monotonic()))
            dashboard.finish_poll(ids,delay,poll_failed)
            log('Poll complete; sleeping %.2fs' % delay)
            if keyboard.thread or dashboard.terminal: keyboard.wake.wait(delay)
            else: time.sleep(delay)
    finally:
        if isinstance(rpc,RPC): rpc.on_activity=None
        keyboard.close()
        dashboard.stop()
        stats.print_summary()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    home=os.environ.get('CODEX_HOME',os.path.expanduser('~/.codex'))
    p.add_argument('--socket',default=os.path.join(home,'app-server-control','app-server-control.sock'),help='shared daemon Unix socket path')
    p.add_argument('--url',help='optional local ws:// endpoint instead of the Unix socket')
    p.add_argument('--timeout',type=float,default=30,help='connection and RPC timeout in seconds')
    p.add_argument('--check',action='store_true',help='initialize, list loaded thread IDs, and exit without recovery')
    p.add_argument('--interval',type=float,default=1)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--no-live',action='store_true',help='disable the live panel; retain timestamped event logs')
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
            event('Connection OK. Loaded threads (%d): %s' % (len(ids), ', '.join(ids) or 'none'))
        else:
            monitoring=True
            run(rpc,a.dry_run,a.interval,stats,Dashboard(a.interval,a.dry_run,enabled=not a.no_live))
    except (RPCError,OSError,BrokenPipeError) as e: event('Error:',e,file=sys.stderr); return 1
    except KeyboardInterrupt:
        event('Stopped.')
        if not monitoring: stats.print_summary()
    finally:
        if rpc: rpc.close()
    return 0
if __name__=='__main__': sys.exit(main())
