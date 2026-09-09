import asyncio
import os
import tempfile
import unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from sheet_watcher import StableFileDebouncer, watch_file

class DebouncerTests(unittest.TestCase):
 def test_unchanged_does_not_sync(self):
  s=StableFileDebouncer(1); self.assertIsNone(s.observe((1,1),0)); self.assertIsNone(s.observe((1,1),2))
 def test_rapid_changes_collapse(self):
  s=StableFileDebouncer(1); s.observe((1,1),0); s.observe((2,2),.1); s.observe((3,3),.5); self.assertIsNone(s.observe((3,3),1.4)); self.assertEqual((3,3),s.observe((3,3),1.5)); s.mark_success((3,3)); self.assertIsNone(s.observe((3,3),3))

class WatcherTests(unittest.IsolatedAsyncioTestCase):
 async def test_change_once_and_retry_failure(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"x.xlsx"; p.write_bytes(b"a"); calls=[]; stop=asyncio.Event()
   async def sync():
    calls.append(len(calls)); return len(calls)>1
   task=asyncio.create_task(watch_file(p,sync,poll_seconds=.01,debounce_seconds=.02,retry_seconds=.03,stop_event=stop))
   await asyncio.sleep(.03); p.write_bytes(b"broken"); os.utime(p,None); await asyncio.sleep(.12); stop.set(); await task
   self.assertEqual(2,len(calls))
 async def test_rapid_writes_are_one_sync(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"x.xlsx"; p.write_bytes(b"a"); calls=[]; stop=asyncio.Event()
   async def sync(): calls.append(1); return True
   task=asyncio.create_task(watch_file(p,sync,poll_seconds=.01,debounce_seconds=.04,retry_seconds=.03,stop_event=stop))
   await asyncio.sleep(.03)
   for value in (b"bb",b"ccc",b"dddd"):
    p.write_bytes(value); os.utime(p,None); await asyncio.sleep(.01)
   await asyncio.sleep(.08); stop.set(); await task; self.assertEqual(1,len(calls))

if __name__=="__main__": unittest.main()
