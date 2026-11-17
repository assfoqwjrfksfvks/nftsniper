import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gift_api import GiftAPIClient, parse_unique_gift
from hunter import discover_latest, HunterEngine
from nft_client import NFTFetchError
from settings import Settings
from store import Store
from telegram_client import TelegramClient, TelegramError
from test_telegram_client import Session, Response


def data(n=1, **changes):
    d=dict(name=f'CandyCane-{n}',number=n,model={'name':'Rare','rarity_per_mille':13},
           backdrop={'name':'Black','rarity_per_mille':5},symbol={'name':'Star','rarity_per_mille':20})
    d.update(changes)
    return d


class APITests(unittest.IsolatedAsyncioTestCase):
    def client(self, body, status=200):
        return TelegramClient(Session(Response(body,status)), '12345:'+'x'*24, '12345')

    async def test_lookup_uses_official_method_and_name(self):
        c=self.client({'ok':True,'result':data()})
        self.assertEqual(await c.unique_gift('CandyCane-1'),data())
        url,kw=c.session.requests[0]
        self.assertTrue(url.endswith('/getUniqueGift'))
        self.assertEqual(kw['json'],{'name':'CandyCane-1'})

    async def test_only_known_not_found_is_absence(self):
        c=self.client({'ok':False,'error_code':400,'description':'Bad Request: gift not found'},400)
        self.assertIsNone(await c.unique_gift('CandyCane-1'))
        c=self.client({'ok':False,'error_code':400,'description':'Unexpected malformed request'},400)
        with self.assertRaises(TelegramError):await c.unique_gift('CandyCane-1')

    async def test_malformed_success_is_not_absence(self):
        c=self.client({'ok':True,'result':None})
        with self.assertRaises(TelegramError):await c.unique_gift('CandyCane-1')

    async def test_auth_and_rate_errors_do_not_become_missing(self):
        for code in [401,403,404,429]:
            c=self.client({'ok':False,'error_code':code,'description':'failure'},code)
            with self.assertRaises(TelegramError):await c.unique_gift('CandyCane-1')

    def test_metadata_rarity_validation_and_burned(self):
        g=parse_unique_gift(data(is_burned=True),'CandyCane',1)
        self.assertEqual(g.model_rarity,1.3)
        self.assertEqual(g.backdrop_rarity,.5)
        self.assertTrue(g.burned)
        self.assertIsNone(g.owner_username)
        for d in [data(number=2),data(name='Different-1'),data(model={'name':'x','rarity_per_mille':float('nan')})]:
            with self.assertRaises(NFTFetchError):parse_unique_gift(d,'CandyCane',1)
        g=parse_unique_gift(data(model={'name':'Crafted','rarity_per_mille':0,'rarity':'legendary'}),'CandyCane',1)
        self.assertEqual(g.model_rarity,100)

    async def test_frontier_search_never_reads_html(self):
        class Bot:
            async def unique_gift(self,name):
                n=int(name.rsplit('-',1)[-1])
                return data(n) if n<=1200 else None
        public=AsyncMock()
        source=GiftAPIClient(Bot(),public,candidate=lambda _:True,request_interval=0)
        real_sleep=asyncio.sleep
        async def fast(_):await real_sleep(0)
        with patch('gift_api.asyncio.sleep',new=fast):
            self.assertEqual(await discover_latest(source,'CandyCane',1),1200)
        public.fetch.assert_not_called()

    async def test_owner_page_failure_preserves_api_metadata(self):
        bot=AsyncMock();bot.unique_gift.return_value=data()
        public=AsyncMock();public.fetch.side_effect=NFTFetchError('empty HTML')
        source=GiftAPIClient(bot,public,candidate=lambda _:True,request_interval=0)
        g=await source.fetch('CandyCane',1)
        self.assertEqual(g.number,1)
        self.assertEqual(g.model,'Rare')
        self.assertIsNone(g.owner_username)

    async def test_owner_timeout_does_not_cancel_metadata_monitor(self):
        bot=AsyncMock();bot.unique_gift.return_value=data()
        public=AsyncMock();public.fetch.side_effect=asyncio.TimeoutError()
        source=GiftAPIClient(bot,public,candidate=lambda _:True,request_interval=0)
        self.assertEqual((await source.fetch('CandyCane',1)).number,1)

    async def test_matching_gift_reaches_durable_outbox(self):
        bot=AsyncMock();bot.unique_gift.return_value=data()
        public=AsyncMock();public.fetch.return_value=parse_unique_gift(data(),'CandyCane',1)
        from dataclasses import replace
        public.fetch.return_value=replace(public.fetch.return_value,owner_username='alice_test',owner_display='Alice')
        source=GiftAPIClient(bot,public,candidate=lambda _:True,request_interval=0)
        g=await source.fetch('CandyCane',1)
        with tempfile.TemporaryDirectory() as d:
            engine=object.__new__(HunterEngine)
            engine.cfg=Settings().validate();engine.store=Store(Path(d)/'state.sqlite3')
            engine._emit=lambda *a,**kw:None;engine.log=lambda _:None
            try:
                await engine._process(g)
                await engine._process(g)
                self.assertEqual(engine.store.stats()['pending'],1)
                row=engine.store.pending()[0]
                self.assertIn('alice_test',row['text'])
                self.assertIn('t.me/alice_test',str(row['reply_markup']))
            finally:engine.store.close()

    async def test_nonmatching_and_burned_gifts_do_not_fetch_owner(self):
        for burned,match in [(False,False),(True,True)]:
            bot=AsyncMock();bot.unique_gift.return_value=data(is_burned=burned)
            public=AsyncMock()
            source=GiftAPIClient(bot,public,candidate=lambda _:match,request_interval=0)
            self.assertIsNotNone(await source.fetch('CandyCane',1))
            public.fetch.assert_not_called()

    async def test_positive_cache_avoids_repeating_metadata_lookup(self):
        bot=AsyncMock();bot.unique_gift.return_value=data()
        source=GiftAPIClient(bot,AsyncMock(),candidate=lambda _:False,request_interval=0)
        await source.probe('CandyCane',1)
        await source.probe('CandyCane',1)
        bot.unique_gift.assert_awaited_once()

    async def test_lookup_rate_limit_propagates_and_defers_queries(self):
        bot=AsyncMock();bot.unique_gift.side_effect=TelegramError('limit',retry_after=7,status_code=429)
        source=GiftAPIClient(bot,AsyncMock(),candidate=lambda _:False,request_interval=0)
        with self.assertRaises(NFTFetchError) as c:await source.probe('CandyCane',1)
        self.assertEqual(c.exception.retry_after,7)
        import time
        self.assertGreater(source._next_request,time.monotonic()+6)
