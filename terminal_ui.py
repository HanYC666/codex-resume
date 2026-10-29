"""Fixed bottom panel and independently scrollable history for POSIX terminals."""
import io, sys, threading, textwrap, base64, os, shutil, subprocess

def copy_text(text, output):
    """Prefer a desktop clipboard helper; OSC52 is a terminal fallback."""
    candidates=[['pbcopy']] if sys.platform=='darwin' else ([['wl-copy']] if os.environ.get('WAYLAND_DISPLAY') else [])+[['xclip','-selection','clipboard'],['xsel','--clipboard','--input']]
    for command in candidates:
        if shutil.which(command[0]):
            try:
                subprocess.run(command,input=text.encode('utf-8'),check=True,timeout=2,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                return 'Copied selection'
            except (OSError,subprocess.SubprocessError):pass
    output.write_raw('\x1b]52;c;'+base64.b64encode(text.encode('utf-8')).decode()+'\x07');output.flush()
    return 'Clipboard sent via OSC52; if unsupported, F2 for native copy'

class TerminalUI:
    def __init__(self, dashboard):
        from prompt_toolkit import Application
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.input import create_input
        from prompt_toolkit.output import create_output
        self.dashboard=dashboard; self.text=''; self.lock=threading.RLock(); self.top=None
        self.original_out=sys.stdout; self.original_err=sys.stderr
        self.ready=threading.Event();self.error=None
        self.controls=None; self.horizontal=0
        self.selection=None;self.dragging=False;self.notice='';self.mouse_enabled=True
        self.native_status=None;self.native_history=None
        self.line_cache=None
        self.history_window=Window(FormattedTextControl(self.history),wrap_lines=False)
        self.status_window=Window(FormattedTextControl(lambda:ANSI(self.status())),
                                  height=lambda:Dimension.exact(self.panel_height()),wrap_lines=False,
                                  get_horizontal_scroll=lambda window:self.horizontal)
        keys=KeyBindings()
        def redraw(): self.app.invalidate()
        for key,delta in [('up',-1),('down',1)]:
            keys.add(key)(lambda event,delta=delta:(dashboard.move(delta),redraw()))
        for key,delta in [('left',-8),('right',8)]:
            def pan(event,delta=delta):
                self.horizontal=max(0,min(max(0,120-self.app.output.get_size().columns),self.horizontal+delta));redraw()
            keys.add(key)(pan)
        @keys.add('?')
        def help(event): self.horizontal=0; dashboard.help_open=not dashboard.help_open; redraw()
        @keys.add('escape')
        def escape(event): dashboard.help_open=False;self.selection=None;self.dragging=False;self.notice='';redraw()
        for key,action in [('r','retry'),('R','retry'),('p','pause'),('P','pause'),('backspace','untrack'),('c-h','untrack'),('q','quit'),('c-c','quit')]:
            def command(event,action=action):
                if event.key_sequence[0].key=='c-c' and self.selected_text():
                    self.copy_selection();return
                if self.controls and (dashboard.selected or action=='quit'):
                    self.controls.commands.put((action,dashboard.selected));self.controls.wake.set()
                    dashboard.set_activity('Requested '+action);redraw()
            keys.add(key)(command)
        @keys.add('c')
        @keys.add('c-y')
        def copy(event):self.copy_selection()
        @keys.add('f2')
        def native(event):
            if self.mouse_enabled:
                self.native_status=self.status();self.native_history=self.history()
                self.mouse_enabled=False
            else:
                self.mouse_enabled=True;self.native_status=None;self.native_history=None
            redraw()
        @keys.add('pageup')
        def previous(event): self.scroll(-self.history_height())
        @keys.add('pagedown')
        def next(event): self.scroll(self.history_height())
        @keys.add('home')
        def beginning(event):self.top=0;redraw()
        @keys.add('s-up')
        def older_line(event):self.scroll(-3)
        @keys.add('s-down')
        def newer_line(event):self.scroll(3)
        @keys.add('end')
        @keys.add('c-end')
        def end(event): self.top=None;redraw()
        self.app=Application(layout=Layout(HSplit([Window(FormattedTextControl(self.history_label),height=1),self.history_window,self.status_window])),
            key_bindings=keys,full_screen=True,mouse_support=Condition(lambda:self.mouse_enabled),
            input=create_input(stdin=sys.stdin),output=create_output(stdout=self.original_out),
            min_redraw_interval=0,refresh_interval=.25)
        self.app.ttimeoutlen=.05;self.app.timeoutlen=.1
        ui=self
        class History(io.TextIOBase):
            def write(self,value):
                with ui.lock:ui.text+=str(value)
                if ui.error:ui.original_out.write(str(value));ui.original_out.flush()
                else:ui.app.invalidate()
                return len(value)
            def flush(self):pass
            def isatty(self):return True
        self.writer=History()
    def panel_height(self):
        return max(1,min(20,self.app.output.get_size().rows-3,max(10,len(self.dashboard.rows)+7)))
    def history_height(self):return max(1,self.app.output.get_size().rows-self.panel_height()-1)
    def history_label(self):
        if not self.mouse_enabled:return 'NATIVE SELECTION · drag and use Cmd+C / terminal Copy · F2 returns to live UI'
        total=len(self.lines());height=self.history_height()
        first=max(0,total-height) if self.top is None else self.top
        mode='LIVE' if self.top is None else 'BROWSING'
        return ('%s history %d–%d / %d · drag to copy · F2 native copy · PgUp/PgDn/Home/End' % (mode,first+1 if total else 0,min(total,first+height),total))+(' · '+self.notice if self.notice else '')
    def check_health(self):
        if self.error:raise OSError('Terminal UI stopped: '+str(self.error)+'; use --no-live -v for plain output')
        if hasattr(self,'thread') and not self.thread.is_alive():raise OSError('Terminal UI exited unexpectedly; use --no-live -v for plain output')
    def lines(self):
        width=max(1,self.app.output.get_size().columns)
        with self.lock:text=self.text
        if self.line_cache and self.line_cache[0] is text and self.line_cache[1]==width:return self.line_cache[2]
        result=[part for line in text.splitlines() for part in (textwrap.wrap(line,width,replace_whitespace=False,drop_whitespace=False) or [''])]
        self.line_cache=(text,width,result)
        return result
    def scroll(self,amount):
        lines=self.lines();last=max(0,len(lines)-self.history_height())
        self.top=max(0,min(last,(last if self.top is None else self.top)+amount))
        if self.top==last:self.top=None
        self.app.invalidate()
    def selected_text(self):
        if not self.selection:return ''
        start,end=sorted(self.selection);lines=self.lines()
        if start==end:return ''
        return '\n'.join(line[(start[1] if y==start[0] else 0):(end[1] if y==end[0] else len(line))]
                         for y,line in enumerate(lines) if start[0]<=y<=end[0])
    def copy_selection(self):
        text=self.selected_text()
        if not text:return
        def copy():
            self.notice=copy_text(text,self.app.output)
            self.app.invalidate()
        threading.Thread(target=copy,daemon=True).start()
    def history(self):
        if not self.mouse_enabled and self.native_history is not None:return self.native_history
        from prompt_toolkit.mouse_events import MouseEventType, MouseButton
        from prompt_toolkit.utils import get_cwidth
        lines=self.lines();height=self.history_height();last=max(0,len(lines)-height)
        start=last if self.top is None else min(self.top,last)
        def mouse(event):
            if event.event_type==MouseEventType.SCROLL_UP:self.scroll(-3)
            elif event.event_type==MouseEventType.SCROLL_DOWN:self.scroll(3)
            elif event.button==MouseButton.LEFT:
                row=min(max(0,start+event.position.y),max(0,len(lines)-1));column=0;cells=0
                for char in (lines[row] if lines else ''):
                    if cells+get_cwidth(char)>event.position.x:break
                    cells+=get_cwidth(char);column+=1
                point=(row,column)
                if event.event_type==MouseEventType.MOUSE_DOWN:
                    self.top=start;self.selection=(point,point);self.dragging=True
                elif self.dragging:
                    self.selection=(self.selection[0],point)
                    if event.event_type==MouseEventType.MOUSE_UP:
                        self.dragging=False;self.copy_selection()
                self.app.invalidate()
            else:return NotImplemented
        selected=sorted(self.selection) if self.selection else None
        fragments=[]
        for y,line in enumerate(lines[start:start+height],start):
            if fragments:fragments.append(('', '\n',mouse))
            for x,char in enumerate(line):
                style='reverse' if selected and selected[0]<=(y,x)<selected[1] else ''
                fragments.append((style,char,mouse))
            if not line:fragments.append(('', ' ',mouse))
        return fragments
    def status(self):
        if not self.mouse_enabled and self.native_status is not None:return self.native_status
        from rich.console import Console
        sink=io.StringIO();columns=self.app.output.get_size().columns
        self.horizontal=min(self.horizontal,max(0,120-columns))
        width=max(120,columns)
        console=Console(file=sink,width=width,height=self.panel_height(),force_terminal=True,color_system='standard',markup=False,highlight=False)
        self.dashboard.console=console
        console.print(self.dashboard.render())
        lines=sink.getvalue().splitlines()[:self.panel_height()]
        return '\n'.join(['']*max(0,self.panel_height()-len(lines))+lines)
    def start(self):
        sys.stdout=self.writer;sys.stderr=self.writer
        def failed(exc):
            self.error=exc;self.ready.set()
            self.original_err.write('\nTerminal UI failed: '+str(exc)+'; use --no-live -v for plain output\n')
            self.original_err.flush()
        def run():
            def setup():
                def on_error(loop,context):
                    exc=context.get('exception') or RuntimeError(context.get('message','UI event loop failed'))
                    failed(exc)
                    if self.app.is_running:self.app.exit(exception=exc)
                self.app.loop.set_exception_handler(on_error)
            try:self.app.run(pre_run=setup,set_exception_handler=False,handle_sigint=False)
            except Exception as exc:
                if not self.error:failed(exc)
        self.app.after_render+=lambda app:self.ready.set()
        self.thread=threading.Thread(target=run,daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=5):
            self.error=TimeoutError('no initial screen rendered')
            self.stop()
            raise OSError('Terminal UI did not render; use --no-live -v')
        if self.error:
            self.stop()
            raise OSError('Terminal UI startup failed: '+str(self.error))
    def stop(self):
        if self.app.is_running:self.app.loop.call_soon_threadsafe(self.app.exit)
        self.thread.join(timeout=3)
        sys.stdout=self.original_out;sys.stderr=self.original_err
        # Preserve the complete event log in normal terminal scrollback after exit.
        if self.text:self.original_out.write(self.text);self.original_out.flush()
