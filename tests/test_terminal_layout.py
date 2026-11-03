import io,unittest
from unittest.mock import patch
from rich.console import Console
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
import codex_multi_recovery as m
from terminal_ui import TerminalUI

class Output(DummyOutput):
 def get_size(self):return Size(rows=40,columns=150)

class LayoutTests(unittest.TestCase):
 def dashboard(self):
  d=m.Dashboard(5,enabled=False)
  for n in range(40):
   state=m.RecoveryState();state.model='gpt-6-sol';state.effort='high';state.goal_status='active'
   d.update_thread('%04d'%n,'Task %d'%n,{'type':'active'},[],state,m.RunStats([]))
  return d
 def test_one_physical_row_per_thread_and_panel_cap(self):
  d=self.dashboard();sink=io.StringIO();d.console=Console(file=sink,width=150,height=20,color_system=None)
  d.console.print(d.render());lines=sink.getvalue().splitlines()
  self.assertLessEqual(len(lines),20)
  data=[x for x in lines if '#00' in x]
  self.assertGreater(len(data),1)
  for row in data:
   for field in ('gpt-6-sol','high','Goal active','Working'):self.assertIn(field,row)
 def test_five_tracked_threads_stay_visible_when_states_change(self):
  d=m.Dashboard(5,enabled=False)
  names=('smallS','print panda website','Sleep Disabler','fourth thread','fifth thread')
  for name in names:
   d.update_thread(name,name,{'type':'active'},[],m.RecoveryState(),m.RunStats([]))
  with create_pipe_input() as inp,patch('prompt_toolkit.input.create_input',return_value=inp),patch('prompt_toolkit.output.create_output',return_value=Output()):
   ui=TerminalUI(d)
   with patch.object(ui.app.output,'get_size',return_value=Size(rows=16,columns=150)):
    self.assertEqual(ui.panel_height(),13)
    for status in ({'type':'active'},{'type':'systemError'}):
     d.update_thread('smallS','smallS',status,[],m.RecoveryState(),m.RunStats([]))
     d.finish_poll(1)
     frame=ui.status()
     for name in names:self.assertIn(name,frame)
     self.assertIn('5 threads',frame)
 def test_history_scroll_preserves_fixed_status_and_read_position(self):
  with create_pipe_input() as inp,patch('prompt_toolkit.input.create_input',return_value=inp),patch('prompt_toolkit.output.create_output',return_value=Output()):
   ui=TerminalUI(self.dashboard());ui.text=''.join('event %d\n'%n for n in range(100))
   self.assertLessEqual(ui.panel_height(),20)
   before=ui.status();self.assertIn('event 99',''.join(part[1] for part in ui.history()))
   ui.scroll(-20);older=''.join(part[1] for part in ui.history())
   self.assertNotIn('event 99',older)
   ui.text+='new event\n'
   self.assertEqual(''.join(part[1] for part in ui.history()),older)
   self.assertEqual(ui.status(),before)
   ui.scroll(1000);self.assertIsNone(ui.top)
