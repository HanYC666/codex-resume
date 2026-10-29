import threading,unittest
from unittest.mock import patch,MagicMock
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.mouse_events import MouseEvent,MouseEventType,MouseButton
from prompt_toolkit.data_structures import Point
import codex_multi_recovery as m
from terminal_ui import TerminalUI,copy_text
class CopyTests(unittest.TestCase):
 def test_drag_selects_unicode_and_copies_on_release(self):
  with create_pipe_input() as inp,patch('prompt_toolkit.input.create_input',return_value=inp),patch('prompt_toolkit.output.create_output',return_value=DummyOutput()):
   ui=TerminalUI(m.Dashboard(1,enabled=False));ui.text='中文 ABC\nsecond line\n';ui.top=0
   copied=[];done=threading.Event()
   def clipboard(text,output):copied.append(text);done.set();return 'Copied selection'
   def event(kind,x):return MouseEvent(Point(x,0),kind,MouseButton.LEFT,frozenset())
   handler=ui.history()[0][2]
   with patch('terminal_ui.copy_text',side_effect=clipboard):
    handler(event(MouseEventType.MOUSE_DOWN,0));handler(event(MouseEventType.MOUSE_MOVE,4));handler(event(MouseEventType.MOUSE_UP,4))
    self.assertTrue(done.wait(2))
   self.assertEqual(copied,['中文']);self.assertEqual(ui.selected_text(),'中文')
   self.assertTrue(any(part[0]=='reverse' for part in ui.history()))
 def test_macos_clipboard_uses_pbcopy_without_shell(self):
  with patch('terminal_ui.sys.platform','darwin'),patch('terminal_ui.shutil.which',return_value='/usr/bin/pbcopy'),patch('terminal_ui.subprocess.run') as run:
   self.assertEqual(copy_text('中文',MagicMock()),'Copied selection')
   self.assertEqual(run.call_args.args[0],['pbcopy']);self.assertEqual(run.call_args.kwargs['input'],'中文'.encode())
 def test_terminal_clipboard_fallback_is_explicit(self):
  out=MagicMock()
  with patch('terminal_ui.shutil.which',return_value=None):result=copy_text('hello',out)
  self.assertIn('OSC52',result);out.write_raw.assert_called_once_with('\x1b]52;c;aGVsbG8=\x07')
