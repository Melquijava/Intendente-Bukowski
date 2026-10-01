import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from PIL import Image, ImageDraw
from bukowski.config import Config
from bukowski.store import Store, Actor, DomainError
from bukowski.nicknames import nickname, validate_rules
from bukowski.branding import Brand, FOOTER
from bukowski.ocr import TesseractReader, ReadingError, parse_shop_text
from bukowski.bot import Bukowski

ADMIN = Actor(1,frozenset({100}))
RULES = ({'role_id':2,'prefix':'P','priority':10},{'role_id':4,'prefix':'G','priority':100})

class Nicknames(unittest.TestCase):
    def test_document_not_mail_and_role_priority(self):
        self.assertEqual(nickname('João Ribeiro','00215',{2},RULES),'P | João Ribeiro | 00215')
        self.assertEqual(nickname('Nay Bukowski','419',{2,4},RULES),'G | Nay Bukowski | 419')
        self.assertIsNone(nickname('Pessoa','123',{55},RULES))

    def test_only_name_truncated(self):
        result = nickname('Nome muito longo que excede o limite do Discord','000215',{2},RULES)
        self.assertLessEqual(len(result),32)
        self.assertTrue(result.startswith('P | '))
        self.assertTrue(result.endswith(' | 000215'))
        with self.assertRaises(DomainError):
            nickname('João','0'*40,{2},RULES)

    def test_configurable_format_and_validation(self):
        self.assertEqual(nickname('João','215',{2},RULES,'{prefix}: {name} / {document}'),'P: João / 215')
        with self.assertRaises(ValueError): validate_rules(RULES,'{prefix}: {name}')
        with self.assertRaises(ValueError): validate_rules([{'role_id':2,'prefix':'','priority':1}],'{prefix} | {name} | {document}')

    def test_legacy_environment_compatibility(self):
        keys = ['PUBLIC_CATEGORY','INFO_CHANNEL','ANNOUNCEMENTS_CHANNEL','RULES_CHANNEL','SERVICE_CHANNEL','PRICES_CHANNEL','WORK_CHANNEL','REQUESTS_CHANNEL','LOG_CHANNEL','ARRIVALS_CHANNEL']
        env = {k+'_ID':'10' for k in keys}
        env.update(DISCORD_TOKEN='test-not-real',GUILD_ID='1',P1_ROLE_ID='2',GERENTE_ROLE_ID='4',ADMIN_ROLE_IDS='100',FINANCE_ROLE_IDS='200')
        with patch.dict(os.environ,env,clear=True),patch('bukowski.config.load_dotenv'):
            config = Config.load()
        self.assertEqual(config.peao,2)
        self.assertEqual(config.role_rules()[1]['prefix'],'G')

class IdentityFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config('unused',1,{},2,frozenset({100}),frozenset({200}),str(Path(self.tmp.name)/'db.sqlite3'),'por+eng','',RULES)
        self.bot = Bukowski(self.config)
        self.member = SimpleNamespace(id=3,roles=[SimpleNamespace(id=2)],nick=None,top_role=5)
        async def edit(**kwargs): self.member.nick = kwargs['nick']
        self.member.edit = AsyncMock(side_effect=edit)
        self.guild = SimpleNamespace(owner_id=99,fetch_member=AsyncMock(return_value=self.member),me=SimpleNamespace(top_role=10,guild_permissions=SimpleNamespace(manage_nicknames=True)))
        self.bot.get_guild = Mock(return_value=self.guild)
        self.bot._connection.user = SimpleNamespace(id=999)
        self.rid = self.bot.store.register(Actor(3),'João Ribeiro','00215','00419')

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    async def approve(self):
        grant = AsyncMock()
        await self.bot.store.approve(ADMIN,self.rid,grant,lambda row:self.bot.sync_member(row,ADMIN,False))
        return grant

    async def test_approval_peao_and_nickname(self):
        grant = await self.approve()
        grant.assert_awaited_once_with(3)
        self.assertEqual(self.member.nick,'P | João Ribeiro | 00215')
        row = self.bot.store.one('SELECT * FROM registrations WHERE id=?',(self.rid,))
        self.assertEqual((row['status'],row['role_state'],row['nickname_state']),('approved','granted','synced'))

    async def test_promotion_demotion_and_no_redundant_edit(self):
        await self.approve()
        before = SimpleNamespace(roles=list(self.member.roles))
        self.member.roles.append(SimpleNamespace(id=4)); self.member.guild = SimpleNamespace(id=1)
        await self.bot.on_member_update(before,self.member)
        self.assertEqual(self.member.nick,'G | João Ribeiro | 00215')
        before.roles = list(self.member.roles)
        self.member.roles = [SimpleNamespace(id=2)]
        await self.bot.on_member_update(before,self.member)
        self.assertEqual(self.member.nick,'P | João Ribeiro | 00215')
        row = dict(self.bot.store.one('SELECT * FROM registrations WHERE id=?',(self.rid,)))
        await self.bot.sync_member(row,ADMIN)
        self.assertEqual(self.member.edit.await_count,3)

    async def test_partial_failure_then_safe_retry(self):
        self.guild.me.top_role = 4
        grant = AsyncMock()
        with self.assertRaisesRegex(DomainError,'Peão concedido'):
            await self.bot.store.approve(ADMIN,self.rid,grant,lambda row:self.bot.sync_member(row,ADMIN,False))
        row = self.bot.store.one('SELECT * FROM registrations WHERE id=?',(self.rid,))
        self.assertEqual((row['status'],row['role_state'],row['nickname_state']),('pending','granted','failed'))
        self.guild.me.top_role = 10
        await self.approve()
        self.assertEqual(self.member.nick,'P | João Ribeiro | 00215')

    async def test_correction_uses_registration_source(self):
        await self.approve()
        self.bot.store.correct_registration(ADMIN,self.rid,'Nay Bukowski','000419','215','Correção de dados')
        row = dict(self.bot.store.one('SELECT * FROM registrations WHERE id=?',(self.rid,)))
        await self.bot.sync_member(row,ADMIN)
        self.assertEqual(self.member.nick,'P | Nay Bukowski | 000419')

    async def test_owner_hierarchy_and_missing_permission(self):
        self.guild.owner_id = 3
        with self.assertRaises(DomainError): await self.approve()
        self.guild.owner_id = 99
        self.guild.me.guild_permissions.manage_nicknames = False
        with self.assertRaises(DomainError): await self.approve()

    async def test_request_original_updated_and_disabled(self):
        await self.approve()
        source = SimpleNamespace(id=12,channel=SimpleNamespace(id=13),embeds=[],edit=AsyncMock())
        await self.bot.update_request_messages(self.rid,source)
        args = source.edit.call_args.kwargs
        self.assertTrue(all(button.disabled for button in args['view'].children))
        self.assertIn('Aprovado',[f.value for f in args['embed'].fields])

    async def test_attachment_download_error_is_private_and_classified(self):
        self.config.channels = {'PRICES_CHANNEL':12,'LOG_CHANNEL':13}
        self.bot.validate_channels = Mock()
        self.bot.refresh_panels = AsyncMock()
        log_channel = SimpleNamespace(send=AsyncMock())
        self.bot.get_channel = Mock(return_value=log_channel)
        author = SimpleNamespace(id=3,bot=False)
        guild = SimpleNamespace(id=1,fetch_member=AsyncMock(return_value=SimpleNamespace(id=3,roles=[SimpleNamespace(id=100)])))
        attachment = SimpleNamespace(id=4,url='https://cdn.discordapp.com/test',filename='prices.png',content_type='image/png',size=100,read=AsyncMock(side_effect=OSError('private-detail-do-not-show')))
        message = SimpleNamespace(id=5,author=author,guild=guild,channel=SimpleNamespace(id=12),attachments=[attachment],created_at=self.bot.store.clock(),jump_url='https://discord.com/channels/1/12/5',reply=AsyncMock())
        await self.bot.process_prices(message)
        row = self.bot.store.one('SELECT * FROM tables WHERE message=?',('5',))
        self.assertEqual((row['state'],row['error_code']),('pending','attachment_download'))
        self.assertNotIn('private-detail',row['error'])
        self.assertNotIn('private-detail',str(message.reply.call_args))

class MigrationTests(unittest.TestCase):
    def test_upgrade_preserves_all_existing_business_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'legacy.sqlite3')
            conn = sqlite3.connect(path)
            conn.executescript(Path('bukowski/migrations/001_initial.sql').read_text(encoding='utf-8'))
            conn.executescript("""
            INSERT INTO registrations VALUES(1,3,'João','00215','00419','approved',1,'2026-10-01T08:00:00-03:00',NULL,'2026-10-01T07:00:00-03:00');
            INSERT INTO settings VALUES('rate','30');
            INSERT INTO tables VALUES(1,'123',1,'anexo','2026-10-01T08:00:00-03:00','2026-10-01T06:00:00-03:00','Alho: $0.10','valid','{"alho":10}',NULL);
            INSERT INTO withdrawals VALUES(1,3,'alho',200,10,'30','2026-10-01T08:00:00-03:00','sold','x');
            INSERT INTO quotes VALUES(1,1,1,10,20000,6000,'2026-10-01T08:00:00-03:00');
            INSERT INTO sales VALUES(1,1,1,10,20000,6000,14000,'2026-10-01T08:00:00-03:00');
            INSERT INTO payments VALUES(1,1,3,6000,'received',2,2,'2026-10-01T09:00:00-03:00','2026-10-01T08:30:00-03:00');
            INSERT INTO ledger VALUES(1,1,6000,2,2,'2026-10-01T09:00:00-03:00','Recebido',NULL);
            INSERT INTO audit VALUES(1,2,'recebimento_confirmado','{}','2026-10-01T09:00:00-03:00');
            INSERT INTO panels VALUES('work',10,20);
            """)
            names = ['registrations','settings','tables','withdrawals','quotes','sales','payments','ledger','audit','panels','notices']
            cols = {t:[r[1] for r in conn.execute(f'PRAGMA table_info({t})')] for t in names}
            before = {t:conn.execute(f'SELECT * FROM {t}').fetchall() for t in names}
            conn.commit(); conn.close()
            store = Store(path,{100},{200})
            try:
                for table in names:
                    after = [tuple(r) for r in store.db.execute(f'SELECT {",".join(cols[table])} FROM {table}')]
                    self.assertEqual(before[table],after,table)
                self.assertEqual(store.balance(Actor(2,frozenset({200}))),6000)
                self.assertEqual(store.one('SELECT MAX(version) v FROM migrations')['v'],2)
                backup = sqlite3.connect(path+'.pre-v2.backup.sqlite3')
                try:
                    self.assertEqual(backup.execute('SELECT MAX(version) FROM migrations').fetchone()[0],1)
                    self.assertEqual(backup.execute('SELECT SUM(amount) FROM ledger').fetchone()[0],6000)
                finally: backup.close()
            finally: store.db.close()

class BrandTests(unittest.TestCase):
    def test_discord_cdn_image_metadata_does_not_trigger_reupload(self):
        import discord
        config = Config('',1,{},2,frozenset({100}),frozenset({200}),'unused','eng','')
        brand = Brand(config)
        expected = brand.embed('Teste')
        expected.set_image(url='attachment://bukowski-service.png')
        data = expected.to_dict()
        data['image'] = {'url':'https://cdn.discordapp.com/attachments/1/2/bukowski-service.png?ex=123','proxy_url':'https://media.discordapp.net/image','width':2172,'height':724}
        actual = discord.Embed.from_dict(data)
        self.assertTrue(brand.same_panel(actual,expected))
        actual.description = 'Dados alterados'
        self.assertFalse(brand.same_panel(actual,expected))

    def test_panel_art_files_are_real_png_attachments(self):
        config = Config('',1,{},2,frozenset({100}),frozenset({200}),'unused','eng','')
        brand = Brand(config)
        for kind in ('service','work','admin'):
            files = brand.panel_files(kind)
            try:
                self.assertEqual(files[0].filename,f'bukowski-{kind}.png')
                self.assertEqual(files[0].fp.read(8),b'\x89PNG\r\n\x1a\n')
                self.assertLess(brand.panel_asset(kind).stat().st_size,8_000_000)
            finally:
                for file in files: file.close()

    def test_all_panels_and_financial_embed_obey_discord_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config('unused',1,{},2,frozenset({100}),frozenset({200}),str(Path(tmp)/'db'),'eng','')
            store = Store(config.database,{100},{200})
            try:
                brand = Brand(config)
                embeds = [brand.panel(k,store) for k in ('service','work','admin')]
                embeds.append(brand.quote({'withdrawal':1,'product':'alho','seeds':200,'yield':10,'units':2000,'price':10,'total':20000,'rate':'30','farm':6000,'employee':14000}))
                for embed in embeds:
                    self.assertEqual(embed.footer.text,FOOTER)
                    self.assertLessEqual(len(embed),6000)
                    self.assertLessEqual(len(embed.fields),25)
                    self.assertTrue(all(len(f.value)<=1024 for f in embed.fields))
                    if embed.title in ('AS PORTEIRAS DA BUKOWSKI','LIVRO DE LABUTA','MESA DO INTENDENTE'):
                        self.assertTrue(embed.image.url.startswith('attachment://bukowski-'))
                    else:
                        self.assertIsNone(embed.image.url)
                self.assertEqual([e.title for e in embeds[:3]],['AS PORTEIRAS DA BUKOWSKI','LIVRO DE LABUTA','MESA DO INTENDENTE'])
            finally: store.db.close()

class ReaderTests(unittest.TestCase):
    def test_reference_card_products_no_clock_and_small_resolution(self):
        command = os.environ.get('TESSERACT_CMD')
        if not command: self.skipTest('Configure TESSERACT_CMD')
        reader = TesseractReader('por+eng',command)
        with Image.open('tests/fixtures/reference_cards.png') as image:
            for width in (900,685):
                resized = image.resize((width,int(image.height*width/image.width)))
                buffer = io.BytesIO(); resized.save(buffer,format='PNG')
                _,prices = reader.read(buffer.getvalue())
                self.assertEqual(prices,{'chuchu':11,'alho':10,'inhame':9})

    def test_accessories_and_separate_lines_without_clock(self):
        text = 'EM ALTA\nChuchu\nLEGUMES\nVOCÊ TEM:\n$0.11\nVender\nAlho — $0,10\nInhame: $0.09 Vender'
        self.assertEqual(parse_shop_text(text),{'chuchu':11,'alho':10,'inhame':9})

    def test_unavailable_distinct_from_invalid_format(self):
        reader = TesseractReader('eng',str(Path('missing-ocr.exe').resolve()))
        with patch('bukowski.ocr.resolve_tesseract',return_value=(None,'unavailable')):
            with self.assertRaises(ReadingError) as result: reader.diagnose()
        self.assertEqual(result.exception.code,'ocr_unavailable')
        with self.assertRaises(ReadingError) as result: reader.read(b'not an image')
        self.assertEqual(result.exception.code,'unsupported_format')

    def test_stale_windows_path_uses_host_path(self):
        from bukowski.ocr_runtime import resolve_tesseract
        with patch('bukowski.ocr_runtime.shutil.which',side_effect=lambda value:'/usr/bin/tesseract' if value=='tesseract' else None),patch('bukowski.ocr_runtime.Path.is_file',return_value=False):
            command,source = resolve_tesseract(r'C:\outro-computador\tesseract.exe')
        self.assertEqual((command,source),('/usr/bin/tesseract','path'))

    def test_blank_config_resets_previous_global_command(self):
        import pytesseract
        reader = TesseractReader('eng','')
        with patch('bukowski.ocr.resolve_tesseract',return_value=('host-tesseract','path')):
            reader._activate()
        self.assertEqual(pytesseract.pytesseract.tesseract_cmd,'host-tesseract')

    def test_verduras_is_accessory_not_product(self):
        text = 'EM ALTA\nAlmeirão\nVERDURAS · VOCÊ TEM:\n$0.11\nMaxixe\nLEGUMES · VOCÊ TEM:\n$0.11\nRabanete\nVERDURAS · VOCÊ TEM:\n$0.11'
        self.assertEqual(parse_shop_text(text),{'almeirão':11,'maxixe':11,'rabanete':11})

    def test_invalid_config_falls_back_and_reads_real_image(self):
        command = os.environ.get('TESSERACT_CMD')
        if not command: self.skipTest('Configure TESSERACT_CMD')
        reader = TesseractReader('por+eng',r'Z:\caminho-inexistente\tesseract.exe')
        with patch('bukowski.ocr.resolve_tesseract',return_value=(command,'path')):
            _,prices = reader.read(Path('tests/fixtures/reference.png').read_bytes())
        self.assertEqual(prices,{'chuchu':11,'alho':10,'inhame':9})

    def test_cards_without_inventory_subtitle(self):
        command = os.environ.get('TESSERACT_CMD')
        if not command: self.skipTest('Configure TESSERACT_CMD')
        with Image.open('tests/fixtures/game_cards.png') as image:
            draw = ImageDraw.Draw(image)
            for index in range(3):
                y=45+index*80
                draw.rectangle((100,y+38,450,y+60),fill=(49,57,72))
            buffer = io.BytesIO(); image.save(buffer,format='PNG')
        _,prices = TesseractReader('por+eng',command).read(buffer.getvalue())
        self.assertEqual(prices,{'junco':11,'romã':11,'pitanga':9})

class VisionTests(unittest.TestCase):
    def test_no_key_never_simulates_a_result(self):
        from bukowski.vision import GeminiReader
        with self.assertRaises(ReadingError) as result:
            GeminiReader('','gemini-2.5-flash').read(Path('tests/fixtures/reference.png').read_bytes())
        self.assertEqual(result.exception.code,'ocr_unavailable')

    def test_real_transport_contract_and_incomplete_json_rejected(self):
        from bukowski.vision import GeminiReader
        data = Path('tests/fixtures/reference.png').read_bytes()
        reader = GeminiReader('test-key-not-real','gemini-2.5-flash')
        payload = {'candidates':[{'content':{'parts':[{'text':json.dumps({'complete':True,'rows':[{'product':'Alho','price':'0.10'}]})}]}}]}
        response = io.BytesIO(json.dumps(payload).encode())
        with patch('urllib.request.urlopen',return_value=response) as transport:
            _,prices = reader.read(data)
            request = transport.call_args.args[0]
            sent = json.loads(request.data)
            self.assertIn('inline_data',sent['contents'][0]['parts'][1])
            self.assertEqual(prices,{'alho':10})
        payload['candidates'][0]['content']['parts'][0]['text'] = json.dumps({'complete':False,'rows':[{'product':'Alho','price':'0.10'}]})
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(ReadingError) as result: reader.read(data)
            self.assertEqual(result.exception.code,'incomplete')
