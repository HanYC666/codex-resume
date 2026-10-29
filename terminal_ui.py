"""Fixed bottom panel and independently scrollable history for POSIX terminals."""
import io, sys, threading, textwrap

class TerminalUI:
    def __init__(self, dashboard):
        from prompt_toolkit import Application
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.input import create_input
        from prompt_toolkit.output import create_output
        self.dashboard=dashboard; self.text=''; self.lock=threading.RLock(); self.top=None
        self.original_out=sys.stdout; self.original_err=sys.stderr
        self.ready=threading.Event();self.error=None
        self.controls=None; self.horizontal=0
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
        def escape(event): dashboard.help_open=False; redraw()
        for key,action in [('r','retry'),('R','retry'),('p','pause'),('P','pause'),('backspace','untrack'),('c-h','untrack'),('q','quit'),('c-c','quit')]:
            def command(event,action=action):
                if self.controls and (dashboard.selected or action=='quit'):
                    self.controls.commands.put((action,dashboard.selected));self.controls.wake.set()
                    dashboard.set_activity('Requested '+action);redraw()
            keys.add(key)(command)
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
            key_bindings=keys,full_screen=True,mouse_support=True,
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
        total=len(self.lines());height=self.history_height()
        first=max(0,total-height) if self.top is None else self.top
        mode='LIVE' if self.top is None else 'BROWSING'
        return '%s history %d–%d / %d · PgUp/PgDn or wheel · Home oldest · End latest' % (mode,first+1 if total else 0,min(total,first+height),total)
    def check_health(self):
        if self.error:raise OSError('Terminal UI stopped: '+str(self.error)+'; use --no-live -v for plain output')
        if hasattr(self,'thread') and not self.thread.is_alive():raise OSError('Terminal UI exited unexpectedly; use --no-live -v for plain output')
    def lines(self):
        width=max(1,self.app.output.get_size().columns)
        with self.lock:text=self.text
        return [part for line in text.splitlines() for part in (textwrap.wrap(line,width,replace_whitespace=False,drop_whitespace=False) or [''])]
    def scroll(self,amount):
        lines=self.lines();last=max(0,len(lines)-self.history_height())
        self.top=max(0,min(last,(last if self.top is None else self.top)+amount))
        if self.top==last:self.top=None
        self.app.invalidate()
    def history(self):
        from prompt_toolkit.mouse_events import MouseEventType
        lines=self.lines();height=self.history_height();last=max(0,len(lines)-height)
        start=last if self.top is None else min(self.top,last)
        def mouse(event):
            if event.event_type==MouseEventType.SCROLL_UP:self.scroll(-3)
            elif event.event_type==MouseEventType.SCROLL_DOWN:self.scroll(3)
            else:return NotImplemented
        return [('', '\n'.join(lines[start:start+height]),mouse)]
    def status(self):
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
