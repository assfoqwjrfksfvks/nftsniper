import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from nft_client import Gift, NFTClient, NFTFetchError, parse_gift_html, parse_rarity, _retry_after

FIXTURES = Path(__file__).with_name('fixtures')


def page(owner='<a href="https://t.me/Some_User">Some User</a>', model='Green Apple <mark>12‰</mark>', backdrop='Black <mark>0,5%</mark>'):
    return f'<table class="tgme_gift_table"><tr><th>Owner</th><td>{owner}</td></tr><tr><th>Model</th><td>{model}</td></tr><tr><th>Backdrop</th><td>{backdrop}</td></tr><tr><th>Symbol</th><td>Ice <mark>2%</mark></td></tr><tr><th>Quantity</th><td>12\u00a0345/50\u202f000 issued</td></tr></table>'


class ParseTests(unittest.TestCase):
    def test_percent_and_permille(self):
        self.assertEqual(parse_rarity('12‰'), 1.2)
        self.assertEqual(parse_rarity('0,5 %'), 0.5)
        self.assertEqual(parse_rarity('unknown'), 100)

    def test_nonfinite_retry_header_has_safe_default(self):
        for value in ['nan', 'inf', '-inf', 'broken']:
            self.assertEqual(_retry_after(value), 30)
        self.assertEqual(_retry_after('-5'), 0)

    def test_traits_and_owner(self):
        gift = parse_gift_html(page(), 'CandyCane', 42)
        self.assertIsInstance(gift, Gift)
        self.assertEqual(gift.owner_username, 'Some_User')
        self.assertEqual(gift.model, 'Green Apple')
        self.assertEqual(gift.model_rarity, 1.2)
        self.assertEqual(gift.backdrop, 'Black')
        self.assertEqual(gift.backdrop_rarity, 0.5)
        self.assertEqual(gift.symbol, 'Ice')
        self.assertEqual(gift.issued, 12345)

    def test_public_owners_only(self):
        for href in ['https://evil.t.me/Some_User', 'https://t.me.evil/Some_User',
                     'https://t.me/nft/CandyCane-1', 'https://t.me/share',
                     'https://t.me/+invite', 'https://t.me:444/Some_User',
                     'https://evil@t.me/Some_User', 'tg://user?id=1']:
            with self.subTest(href=href):
                self.assertIsNone(parse_gift_html(page(owner=f'<a href="{href}">owner</a>'), 'CandyCane', 1).owner_username)
        self.assertIsNone(parse_gift_html(page(owner='Hidden User'), 'CandyCane', 1).owner_username)
        self.assertEqual(parse_gift_html(page(owner='<a href="tg://resolve?domain=Some_User">x</a>'), 'CandyCane', 1).owner_username, 'Some_User')

    def test_unknown_layout_is_error(self):
        for html in ['<html>Gateway problem</html>', '<div class="tgme_gift">Changed layout</div>', '<table class="tgme_gift_table"><tr><th>Owner</th><td>x</td></tr></table>']:
            with self.subTest(html=html), self.assertRaises(NFTFetchError):
                parse_gift_html(html, 'CandyCane', 1)

    def test_live_captured_markup(self):
        gift = parse_gift_html((FIXTURES / 'JingleBells-91000.html').read_text(encoding='utf-8'), 'JingleBells', 91000)
        self.assertEqual(gift.model, 'Inner Glow')
        self.assertEqual(gift.model_rarity, 1.3)
        self.assertEqual(gift.backdrop, 'Dark Lilac')
        self.assertEqual(gift.issued, 69631)
        gift = parse_gift_html((FIXTURES / 'CandyCane-222000.html').read_text(encoding='utf-8'), 'CandyCane', 222000)
        self.assertEqual(gift.model, 'Lovely Kiss')
        self.assertEqual(gift.backdrop, 'Cyberpunk')
        self.assertIsNone(parse_gift_html((FIXTURES / 'JingleBells-99999999.html').read_text(encoding='utf-8'), 'JingleBells', 99999999))
        with self.assertRaises(NFTFetchError) as context:
            parse_gift_html((FIXTURES / 'JingleBells-136503.html').read_text(encoding='utf-8'), 'JingleBells', 136503)
        self.assertEqual(context.exception.retry_after, 60)


class Response:
    def __init__(self, status=200, body=None, headers=None, url='https://t.me/nft/CandyCane-1'):
        self.status, self.body, self.headers, self.url = status, body or page(), headers or {}, url
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def text(self): return self.body


class Session:
    def __init__(self, response): self.response=response; self.requests=[]
    def get(self, url, **kwargs): self.requests.append((url, kwargs)); return self.response


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_uses_verified_default_tls(self):
        session=Session(Response())
        gift=await NFTClient(session, request_interval=0).fetch('CandyCane',1)
        self.assertEqual(gift.number,1)
        self.assertNotIn('ssl',session.requests[0][1])

    async def test_missing_and_rate_limit_are_distinct(self):
        self.assertIsNone(await NFTClient(Session(Response(status=404)), request_interval=0).fetch('CandyCane',1))
        with self.assertRaises(NFTFetchError) as ctx:
            await NFTClient(Session(Response(status=429, headers={'Retry-After':'17'})), request_interval=0).fetch('CandyCane',1)
        self.assertEqual(ctx.exception.retry_after,17)

    async def test_pacing_is_shared_across_collections(self):
        client=NFTClient(Session(Response()),request_interval=.02)
        started=asyncio.get_running_loop().time()
        await asyncio.gather(client.fetch('CandyCane',1),client.fetch('JingleBells',1),client.fetch('LolPop',1))
        self.assertGreaterEqual(asyncio.get_running_loop().time()-started,.035)

    async def test_rate_limit_cooldown_is_shared_across_collections(self):
        session=Session(Response(status=429, headers={'Retry-After':'.04'}))
        client=NFTClient(session,request_interval=0)
        with self.assertRaises(NFTFetchError): await client.fetch('CandyCane',1)
        session.response=Response()
        started=asyncio.get_running_loop().time()
        await client.fetch('JingleBells',1)
        self.assertGreaterEqual(asyncio.get_running_loop().time()-started,.03)

    async def test_sleeping_pacer_observes_extended_deadline(self):
        class Delayed429(Response):
            async def __aenter__(self):
                await asyncio.sleep(.005)
                return self
        session=Session(Delayed429(status=429, headers={'Retry-After':'.06'}))
        client=NFTClient(session,request_interval=.02)
        started=asyncio.get_running_loop().time()
        task=asyncio.create_task(client.fetch('CandyCane',1))
        await asyncio.sleep(0)
        session.response=Response()
        second=asyncio.create_task(client.fetch('JingleBells',1))
        with self.assertRaises(NFTFetchError): await task
        await second
        self.assertGreaterEqual(asyncio.get_running_loop().time()-started,.055)

    async def test_ambiguous_shell_does_not_advance_scanning(self):
        body=(FIXTURES / 'JingleBells-136503.html').read_text(encoding='utf-8')
        client=NFTClient(Session(Response(body=body)),request_interval=0)
        before=asyncio.get_running_loop().time()
        with self.assertRaises(NFTFetchError): await client.fetch('JingleBells',136503)
        self.assertGreaterEqual(client._next_request, before + 59)

    async def test_placeholder_means_missing_only_with_healthy_anchor(self):
        body=(FIXTURES / 'JingleBells-136503.html').read_text(encoding='utf-8')
        class SequenceSession(Session):
            def __init__(self): self.responses=iter([Response(),Response(body=body),Response()]);self.requests=[]
            def get(self,url,**kwargs): self.requests.append((url,kwargs));return next(self.responses)
        session=SequenceSession()
        client=NFTClient(session,request_interval=0)
        self.assertIsNotNone(await client.fetch('JingleBells',91000))
        self.assertIsNone(await client.fetch('JingleBells',136503))
        self.assertEqual([url.rsplit('-',1)[-1] for url,_ in session.requests],['91000','136503','91000'])
        self.assertEqual(client.request_interval,0)

    async def test_throttled_anchor_stops_progress_and_adapts_rate(self):
        body=(FIXTURES / 'JingleBells-136503.html').read_text(encoding='utf-8')
        session=Session(Response())
        client=NFTClient(session,request_interval=0)
        await client.fetch('JingleBells',91000)
        session.response=Response(body=body)
        before=asyncio.get_running_loop().time()
        with self.assertRaises(NFTFetchError) as context:
            await client.fetch('JingleBells',136503)
        self.assertEqual(context.exception.retry_after,60)
        self.assertGreaterEqual(client._next_request,before+59)
        self.assertEqual(client.request_interval,1)
        self.assertEqual(len(session.requests),3)

    async def test_same_anchor_shell_is_always_retryable(self):
        body=(FIXTURES / 'JingleBells-136503.html').read_text(encoding='utf-8')
        session=Session(Response())
        client=NFTClient(session,request_interval=0)
        await client.fetch('JingleBells',91000)
        session.response=Response(body=body)
        with self.assertRaises(NFTFetchError): await client.fetch('JingleBells',91000)
        self.assertEqual(len(session.requests),2)

    async def test_adaptive_rate_has_five_second_cap(self):
        client=NFTClient(Session(Response()),request_interval=4.5)
        client._cooldown(adaptive=True)
        self.assertEqual(client.request_interval,5)
        client._cooldown(adaptive=True)
        self.assertEqual(client.request_interval,5)

    async def test_adaptive_rate_never_decreases_configured_slow_interval(self):
        for configured in [10,60]:
            with self.subTest(interval=configured):
                client=NFTClient(Session(Response()),request_interval=configured)
                client._cooldown(adaptive=True)
                self.assertEqual(client.request_interval,configured)

    async def test_forbidden_response_shares_cooldown(self):
        client=NFTClient(Session(Response(status=403)),request_interval=0)
        before=asyncio.get_running_loop().time()
        with self.assertRaises(NFTFetchError): await client.fetch('JingleBells',91000)
        self.assertGreaterEqual(client._next_request,before+59)

    async def test_timeout_is_error(self):
        session=Session(Response())
        session.get=lambda *args, **kwargs: (_ for _ in ()).throw(asyncio.TimeoutError())
        with self.assertRaises(NFTFetchError): await NFTClient(session,request_interval=0).fetch('CandyCane',1)


if __name__ == '__main__': unittest.main()
