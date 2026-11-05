import unittest
from datetime import datetime, timezone
from compact_history import parse_final

class TestParseFinal(unittest.TestCase):
    def setUp(self):
        self.event = {'date':'2026-11-01T20:00:00Z','status':{'type':{'completed':True}},
          'season':{'type':2},'competitions':[{'competitors':[
          {'homeAway':'home','score':'102','team':{'id':'1','displayName':'Home'}},
          {'homeAway':'away','score':'98','team':{'id':'2','displayName':'Away'}}]}]}
    def test_final(self):
        row=parse_final(self.event,'basketball_nba','123',datetime.now(timezone.utc))
        self.assertEqual(row[7:9],(102.0,98.0))
    def test_unfinished(self):
        self.event['status']['type']['completed']=False
        self.assertIsNone(parse_final(self.event,'basketball_nba','123',datetime.now(timezone.utc)))
    def test_missing_scores(self):
        del self.event['competitions'][0]['competitors'][0]['score']
        self.assertIsNone(parse_final(self.event,'basketball_nba','123',datetime.now(timezone.utc)))

if __name__=='__main__': unittest.main()
