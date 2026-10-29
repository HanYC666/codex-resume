import codecs,fcntl,os,pty,select,struct,subprocess,sys,termios,time,unittest
try:import pyte
except ImportError:pyte=None

@unittest.skipIf(pyte is None,'Install requirements-dev.txt for rendered-screen checks')
class HistoryScreenTests(unittest.TestCase):
 def test_real_keys_mouse_and_late_renderer_failure(self):
  master,slave=pty.openpty();saved=termios.tcgetattr(slave)
  fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,150,0,0))
  code='''
import time
import codex_multi_recovery as m
from terminal_ui import TerminalUI
d=m.Dashboard(5,enabled=False);ui=TerminalUI(d)
ui.text=''.join('EVENT %04d raw history output\\n'%n for n in range(200))
@ui.app.key_bindings.add('b')
def append(event):ui.writer.write('NEW ARRIVAL\\n')
@ui.app.key_bindings.add('!')
def fail(event):
 def broken():raise RuntimeError('injected renderer failure')
 d.render=broken;ui.app.invalidate()
try:
 ui.start()
 while True:ui.check_health();time.sleep(.05)
except OSError:pass
finally:ui.stop()
'''
  p=subprocess.Popen([sys.executable,'-c',code],stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='xterm-256color'))
  os.close(slave);screen=pyte.Screen(150,30);stream=pyte.Stream(screen);decode=codecs.getincrementaldecoder('utf8')('replace');raw=[]
  def collect(seconds):
   end=time.monotonic()+seconds
   while time.monotonic()<end:
    if select.select([master],[],[],.03)[0]:
     try:data=os.read(master,65536)
     except OSError:return
     if not data:return
     raw.append(data);stream.feed(decode.decode(data))
     if b'\x1b[6n' in data:os.write(master,b'\x1b[1;1R')
  def key(data):os.write(master,data);collect(.3)
  def snapshot():return '\n'.join(screen.display)
  try:
   collect(.8);self.assertIn('EVENT 0199',snapshot());self.assertIn('Codex recovery',snapshot())
   key(b'\x1b[5~');self.assertIn('BROWSING',snapshot());self.assertNotIn('EVENT 0199',snapshot())
   key(b'\x1b[H');self.assertIn('EVENT 0000',snapshot())
   before=list(screen.display);key(b'b');self.assertEqual(screen.display[1:15],before[1:15])
   key(b'\x1b[F');self.assertIn('NEW ARRIVAL',snapshot());self.assertIn('LIVE history',snapshot())
   key(b'\x1b[<64;10;4M');self.assertIn('BROWSING',snapshot());self.assertNotIn('NEW ARRIVAL',snapshot())
   self.assertIn('Codex recovery',snapshot())
   key(b'!');collect(.4);self.assertEqual(p.wait(timeout=3),0)
   self.assertIn('Terminal UI failed: injected renderer failure',b''.join(raw).decode(errors='replace'))
   self.assertEqual(termios.tcgetattr(master),saved)
  finally:
   if p.poll() is None:p.kill();p.wait()
   os.close(master)
