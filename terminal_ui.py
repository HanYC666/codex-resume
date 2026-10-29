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
        @keys.add('c-end')
        def end(event): self.top=None;redraw()
        self.app=Application(layout=Layout(HSplit([self.history_window,self.status_window])),
            key_bindings=keys,full_screen=True,mouse_support=True,
            input=create_input(stdin=sys.stdin),output=create_output(stdout=self.original_out),
            min_redraw_interval=0,refresh_interval=.25)
        self.app.ttimeoutlen=.05;self.app.timeoutlen=.1
        ui=self
        class History(io.TextIOBase):
            def write(self,value):
                with ui.lock:ui.text+=str(value)
                ui.app.invalidate();return len(value)
            def flush(self):pass
            def isatty(self):return True
        self.writer=History()
    def panel_height(self):
        return max(1,min(20,self.app.output.get_size().rows-3,max(10,len(self.dashboard.rows)+7)))
    def history_height(self):return max(1,self.app.output.get_size().rows-self.panel_height())
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
        def run():
            try:self.app.run(pre_run=self.ready.set,handle_sigint=False)
            except Exception as exc:self.error=exc;self.ready.set()
        self.thread=threading.Thread(target=run,daemon=True)
        self.thread.start();self.ready.wait(timeout=2)
        if self.error:
            sys.stdout=self.original_out;sys.stderr=self.original_err
            raise OSError('Terminal UI startup failed: '+str(self.error))
    def stop(self):
        if self.app.is_running:self.app.loop.call_soon_threadsafe(self.app.exit)
        self.thread.join(timeout=3)
        sys.stdout=self.original_out;sys.stderr=self.original_err
        # Preserve the complete event log in normal terminal scrollback after exit.
        if self.text:self.original_out.write(self.text);self.original_out.flush()
