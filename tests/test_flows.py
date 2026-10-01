import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from bukowski.store import Store, Actor, DomainError
from bukowski.periods import TZ, period
from bukowski.ocr import parse_prices, TesseractReader

ADMIN = Actor(1,frozenset({100}))
FINANCE = Actor(2,frozenset({200}))
WORKER = Actor(3)

def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ)

class Flows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'farm.sqlite3')
        self.time = at('2026-10-01T08:00:00')
        self.s = Store(self.path,{100},{200},lambda:self.time)

    async def asyncTearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    async def member(self):
        rid = self.s.register(WORKER,'João','000123','00419')
        await self.s.approve(ADMIN,rid,self.grant)
        return rid

    async def grant(self, user):
        self.assertEqual(user,WORKER.id)

    def prices(self):
        return self.s.manual_table(ADMIN,{'alho':10,'chuchu':11,'inhame':9},'Publicação administrativa')

    async def sale(self):
        await self.member()
        self.prices()
        self.s.set_rate(ADMIN,'30')
        wid = self.s.withdraw(WORKER,'Alho',200,'interaction1')
        quote = self.s.quote(WORKER,wid)
        return self.s.sell(WORKER,quote['id']),wid,quote

    async def test_authorized_approval_preserves_numeric_text(self):
        rid = await self.member()
        r = self.s.one('SELECT * FROM registrations WHERE id=?',(rid,))
        self.assertEqual((r['status'],r['document'],r['mail'],r['decider']),('approved','000123','00419',1))

    async def test_unauthorized_approval_never_grants(self):
        rid = self.s.register(WORKER,'João','123','215')
        async def forbidden(user):
            self.fail('Cargo não deveria ser concedido')
        with self.assertRaises(DomainError):
            await self.s.approve(WORKER,rid,forbidden)

    async def test_role_failure_is_retryable(self):
        rid = self.s.register(WORKER,'João','123','215')
        async def fail(user):
            raise RuntimeError('Forbidden')
        with self.assertRaises(DomainError):
            await self.s.approve(ADMIN,rid,fail)
        self.assertEqual(self.s.one('SELECT status FROM registrations WHERE id=?',(rid,))['status'],'pending')
        await self.s.approve(ADMIN,rid,self.grant)

    async def test_simultaneous_old_approval_buttons(self):
        rid = self.s.register(WORKER,'João','123','215')
        calls = []
        async def grant(user):
            calls.append(user)
            await asyncio.sleep(.01)
        results = await asyncio.gather(self.s.approve(ADMIN,rid,grant),self.s.approve(ADMIN,rid,grant),return_exceptions=True)
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(isinstance(x,DomainError) for x in results),1)

    async def test_departure_during_approval_does_not_claim_success(self):
        rid = self.s.register(WORKER,'João','123','215')
        async def grant_then_leave(user):
            self.s.db.execute("UPDATE registrations SET status='departed' WHERE id=?",(rid,))
        with self.assertRaises(DomainError):
            await self.s.approve(ADMIN,rid,grant_then_leave)
        self.assertEqual(self.s.one('SELECT status FROM registrations WHERE id=?',(rid,))['status'],'departed')

    async def test_duplicate_member_and_document_conflicts(self):
        self.s.register(WORKER,'João','123','215')
        for actor,doc in [(WORKER,'999'),(Actor(4),'123')]:
            with self.assertRaises(DomainError):
                self.s.register(actor,'Outro',doc,'0001')

    async def test_refusal_and_correction_history(self):
        rid = self.s.register(WORKER,'João','123','215')
        await self.s.reject(ADMIN,rid,'Documento incorreto')
        self.s.correct_registration(ADMIN,rid,'João','00123','00419','Correção solicitada')
        events = self.s.rows('SELECT event,payload FROM audit')
        self.assertIn('cadastro_corrigido',[r['event'] for r in events])
        self.assertIn('"document": "123"',events[-1]['payload'])

    async def test_restart_persistence(self):
        payment,wid,q = await self.sale()
        self.s.db.close()
        self.s = Store(self.path,{100},{200},lambda:self.time)
        self.assertEqual(self.s.one('SELECT amount FROM payments WHERE id=?',(payment,))['amount'],6000)
        self.assertEqual(self.s.one('SELECT state FROM withdrawals WHERE id=?',(wid,))['state'],'sold')
        self.assertEqual(self.s.one('SELECT COUNT(*) n FROM migrations')['n'],1)

    async def test_missing_rate_blocks_withdrawal(self):
        await self.member(); self.prices()
        with self.assertRaises(DomainError):
            self.s.withdraw(WORKER,'alho',200,'x')

    async def test_sale_calculation_and_snapshot(self):
        await self.member(); self.prices(); self.s.set_rate(ADMIN,'30')
        wid = self.s.withdraw(WORKER,'alho',200,'x')
        self.s.set_rate(ADMIN,'50'); self.s.set_yield(ADMIN,'alho',20)
        q = self.s.quote(WORKER,wid)
        self.assertEqual((q['units'],q['total'],q['farm'],q['employee']), (2000,20000,6000,14000))
        self.assertEqual(q['rate'],'30')
        new = self.s.withdraw(WORKER,'alho',1,'y')
        self.assertEqual(self.s.quote(WORKER,new)['units'],20)

    async def test_explicit_half_up_rounding(self):
        await self.member()
        self.s.manual_table(ADMIN,{'alho':1},'teste')
        self.s.set_yield(ADMIN,'alho',1); self.s.set_rate(ADMIN,'50')
        wid = self.s.withdraw(WORKER,'alho',1,'x')
        q = self.s.quote(WORKER,wid)
        self.assertEqual((q['total'],q['farm'],q['employee']),(1,1,0))

    async def test_rate_bounds_and_no_float(self):
        for value in ('0','100','30,125'):
            self.s.set_rate(ADMIN,value)
        for value in ('-1','101','NaN','Infinity','abc','1.1234567'):
            with self.assertRaises(DomainError): self.s.set_rate(ADMIN,value)
        with self.assertRaises(DomainError): self.s.set_rate(WORKER,'30')

    async def test_sales_and_receipts_duplicate_protection(self):
        payment,wid,q = await self.sale()
        with self.assertRaises(DomainError): self.s.sell(WORKER,q['id'])
        self.s.report_payment(WORKER,payment,FINANCE.id)
        self.assertEqual(self.s.balance(FINANCE),0)
        with self.assertRaises(DomainError): self.s.receive(WORKER,payment,True)
        self.s.receive(FINANCE,payment,True)
        with self.assertRaises(DomainError): self.s.receive(FINANCE,payment,True)
        self.assertEqual(self.s.balance(FINANCE),6000)
        self.assertEqual(self.s.one('SELECT COUNT(*) n FROM ledger')['n'],1)

    async def test_concurrent_receipts_across_connections(self):
        payment,_,_ = await self.sale()
        self.s.report_payment(WORKER,payment,FINANCE.id)
        def attempt(_):
            connection = Store(self.path,{100},{200},lambda:self.time)
            try:
                connection.receive(FINANCE,payment,True)
                return 'received'
            except DomainError:
                return 'duplicate'
            finally:
                connection.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['duplicate','received'])
        self.assertEqual(self.s.balance(FINANCE),6000)

    async def test_rejection_then_delivery_again(self):
        payment,_,_ = await self.sale()
        self.s.report_payment(WORKER,payment,FINANCE.id)
        self.s.receive(FINANCE,payment,False,'Não recebido')
        self.s.report_payment(WORKER,payment,FINANCE.id)
        self.s.receive(FINANCE,payment,True)
        self.assertEqual(self.s.balance(FINANCE),6000)

    async def test_adjustment_reversal_once(self):
        lid = self.s.adjust(FINANCE,1000,'Entrada corrigida')
        self.s.adjust(FINANCE,0,'Estorno por erro',reverse=lid)
        self.assertEqual(self.s.balance(FINANCE),0)
        with self.assertRaises(DomainError): self.s.adjust(FINANCE,0,'Outra tentativa',reverse=lid)

    async def test_offline_restart_blocks_new_work_keeps_debt(self):
        payment,_,_ = await self.sale()
        self.time = at('2026-10-01T12:00:00')
        with self.assertRaises(DomainError): self.s.current_table()
        with self.assertRaises(DomainError): self.s.withdraw(WORKER,'alho',1,'new')
        self.s.report_payment(WORKER,payment,FINANCE.id)
        self.s.receive(FINANCE,payment,True)
        self.assertEqual(self.s.balance(FINANCE),6000)

    async def test_quote_changes_require_new_confirmation(self):
        await self.member(); self.prices(); self.s.set_rate(ADMIN,'30')
        wid = self.s.withdraw(WORKER,'alho',200,'x'); q = self.s.quote(WORKER,wid)
        self.time = at('2026-10-01T09:00:00')
        self.s.manual_table(ADMIN,{'alho':20},'Novo preço')
        with self.assertRaises(DomainError): self.s.sell(WORKER,q['id'])
        self.assertEqual(self.s.quote(WORKER,wid)['total'],40000)

    async def test_ocr_crossing_restart_expires(self):
        tid = self.s.begin_table(123,ADMIN,'attachment',self.time)
        self.time = at('2026-10-01T12:00:00')
        self.assertEqual(self.s.finish_table(tid,ADMIN,'Alho: $0.10',{'alho':10}),'expired')
        with self.assertRaises(DomainError): self.s.current_table()

    async def test_unauthorized_or_invalid_ocr_pending(self):
        tid = self.s.begin_table(123,WORKER,'attachment',self.time)
        self.assertEqual(self.s.finish_table(tid,WORKER,'Alho: $0.10',{'alho':10}),'pending')
        with self.assertRaises(DomainError): self.s.current_table()
        self.s.manual_table(ADMIN,{'alho':10},'Verificado manualmente',tid)
        self.assertEqual(self.s.current_table()[1],{'alho':10})
        tid2 = self.s.begin_table(124,ADMIN,'attachment',self.time)
        self.assertEqual(self.s.finish_table(tid2,ADMIN,'texto ilegível',{},'Inválido'),'pending')

    async def test_message_idempotence_and_publish_order(self):
        early = self.s.begin_table(123,ADMIN,'a',at('2026-10-01T07:00:00'))
        late = self.s.begin_table(124,ADMIN,'b',at('2026-10-01T07:30:00'))
        self.s.finish_table(late,ADMIN,'',{'alho':20})
        self.s.finish_table(early,ADMIN,'',{'alho':10})
        self.assertEqual(self.s.current_table()[1]['alho'],20)
        self.assertIsNone(self.s.begin_table(123,ADMIN,'a',self.time))

    async def test_ownership_and_cancellation(self):
        await self.member(); self.prices(); self.s.set_rate(ADMIN,'30')
        wid = self.s.withdraw(WORKER,'alho',1,'x')
        with self.assertRaises(DomainError): self.s.quote(Actor(44),wid)
        with self.assertRaises(DomainError): self.s.cancel_withdrawal(WORKER,wid,'Perda')
        self.s.cancel_withdrawal(ADMIN,wid,'Perda relatada pelo funcionário')
        with self.assertRaises(DomainError): self.s.quote(WORKER,wid)

class PeriodTests(unittest.TestCase):
    def test_boundaries(self):
        for hour in (6,12,18):
            value = f'2026-10-01T{hour:02}:00:00'
            self.assertEqual(period(at(value)),at(value).isoformat())
        self.assertEqual(period(at('2026-10-01T05:59:59')),at('2026-09-30T18:00:00').isoformat())
        self.assertEqual(period(at('2026-10-01T11:59:59')),at('2026-10-01T06:00:00').isoformat())
        self.assertEqual(period(at('2026-10-01T17:59:59')),at('2026-10-01T12:00:00').isoformat())

    def test_night_crosses_midnight(self):
        self.assertEqual(period(at('2026-10-01T23:59:59')),period(at('2026-10-02T00:00:00')))
        self.assertEqual(period(at('2026-10-02T05:59:59')),at('2026-10-01T18:00:00').isoformat())

    def test_utc_conversion_and_naive_rejected(self):
        self.assertEqual(period(datetime.fromisoformat('2026-10-01T09:00:00+00:00')),at('2026-10-01T06:00:00').isoformat())
        with self.assertRaises(ValueError): period(datetime(2026,10,1))

class OCRTests(unittest.TestCase):
    def test_reference_text_and_decimal_comma(self):
        self.assertEqual(parse_prices('Chuchu: $0.11\nAlho: $0,10\nInhame: $0.09'),{'chuchu':11,'alho':10,'inhame':9})

    def test_ambiguous_image_text_rejected_as_whole(self):
        for text in ('Alho: $0.10\nInhame: ???','Alho: $0.10\nAlho: $0.11','Alho: $0.00','Alho: $-0.10','Alho: $0.101','', 'Produto desconhecido sem preço'):
            with self.assertRaises(ValueError): parse_prices(text)

    def test_invalid_image(self):
        with self.assertRaises(Exception): TesseractReader().read(b'not an image')

    def test_real_reference_image(self):
        command = os.environ.get('TESSERACT_CMD','') or shutil.which('tesseract')
        if not command:
            self.skipTest('Tesseract não instalado; execute no Docker ou configure TESSERACT_CMD')
        data = Path('tests/fixtures/reference.png').read_bytes()
        raw,prices = TesseractReader(os.environ.get('OCR_LANG','eng'),command).read(data)
        self.assertEqual(prices,{'chuchu':11,'alho':10,'inhame':9},raw)

class DiscordSurface(unittest.IsolatedAsyncioTestCase):
    async def test_panel_installation_idempotence_and_recovery(self):
        from bukowski.bot import Bukowski
        from bukowski.config import Config
        class Message:
            def __init__(self,ident,text):
                self.id,self.content,self.author = ident,text,Mock(id=999)
                self.edit = AsyncMock()
        class Channel:
            def __init__(self,ident):
                self.id,self.messages = ident,[]
            async def send(self,text,**kwargs):
                msg = Message(len(self.messages)+1,text)
                self.messages.append(msg)
                return msg
            async def fetch_message(self,ident):
                return next(m for m in self.messages if m.id == ident)
            async def history(self,**kwargs):
                for m in self.messages:
                    yield m
        with tempfile.TemporaryDirectory() as tmp:
            channels = {1:Channel(1),2:Channel(2),3:Channel(3)}
            config = Config('unused',1,{'SERVICE_CHANNEL':1,'WORK_CHANNEL':2,'LOG_CHANNEL':3},2,frozenset({100}),frozenset({200}),str(Path(tmp)/'db.sqlite3'),'eng','')
            bot = Bukowski(config)
            bot._connection.user = Mock(id=999)
            bot.actor = AsyncMock(return_value=ADMIN)
            bot.validate_channels = Mock()
            interaction = Mock()
            interaction.guild.get_channel.side_effect = channels.get
            interaction.response.defer = AsyncMock()
            interaction.response.is_done.return_value = True
            interaction.followup.send = AsyncMock()
            await bot.install(interaction)
            await bot.install(interaction)
            self.assertEqual([len(c.messages) for c in channels.values()],[1,1,1])
            # Simulate crash after send, before panel IDs are persisted.
            bot.store.db.execute('DELETE FROM panels')
            await bot.install(interaction)
            self.assertEqual([len(c.messages) for c in channels.values()],[1,1,1])
            self.assertEqual(bot.store.one('SELECT COUNT(*) n FROM panels')['n'],3)
            await bot.close()

    async def test_commands_and_views_are_persistent(self):
        from bukowski.bot import Bukowski, Panel
        from bukowski.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            config = Config('unused',1,{},2,frozenset({100}),frozenset({200}),str(Path(tmp)/'db.sqlite3'),'eng','')
            bot = Bukowski(config)
            self.assertIn('taxa',[c.name for c in bot.tree.get_commands()])
            for kind,record in [('service',None),('work',None),('admin',None),('registration',1),('quote',1)]:
                self.assertTrue(Panel(bot,kind,record).is_persistent())
            await bot.close()

if __name__ == '__main__':
    unittest.main()
