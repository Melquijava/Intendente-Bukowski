import asyncio
import json
import logging
import random
import re
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
import discord
from discord import app_commands
from discord.ext import tasks
from .config import Config
from .store import Store, Actor, DomainError
from .periods import now, period
from .ocr import TesseractReader, parse_prices, ReadingError
from .branding import Brand, money, STATES, SYNC_STATES
from .nicknames import nickname

log = logging.getLogger(__name__)

async def answer(interaction, text, **kwargs):
    text = text[:1950]
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, **kwargs)

class Form(discord.ui.Modal):
    def __init__(self, bot, title, fields, action):
        super().__init__(title=title)
        self.bot, self.action = bot, action
        for label, long in fields:
            self.add_item(discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph if long else discord.TextStyle.short, max_length=1500 if long else 100))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            actor = await self.bot.actor(interaction)
            await self.action(interaction, actor, [str(x.value).strip() for x in self.children])
        except (DomainError, ValueError, InvalidOperation) as error:
            await answer(interaction, str(error))
        except Exception:
            log.exception('Falha no formulário')
            await answer(interaction, 'Operação não concluída. Consulte a gerência antes de tentar novamente.')

    async def on_error(self, interaction, error):
        log.error('Erro no formulário', exc_info=error)
        await answer(interaction, 'Falha no formulário. Consulte a gerência.')

class Panel(discord.ui.View):
    def __init__(self, bot, kind, record=None):
        super().__init__(timeout=None)
        self.bot, self.kind, self.record = bot, kind, record
        actions = {
            'service': [('Apresentar documentos','register'),('Consultar meu cadastro','status')],
            'work': [('Retirar sementes','withdraw'),('Registrar venda','quote'),('Minhas retiradas','open'),('Meus repasses','pending'),('Informar entrega','report')],
            'admin': [('Taxa da fazenda','rate'),('Rendimento','yield'),('Corrigir tabela','table'),('Consultar pendências','finance'),('Confirmar ou rejeitar entrega','receive')],
            'registration': [('Aprovar','approve'),('Recusar','reject')],
            'quote': [('Confirmar venda calculada','sell')],
        }[kind]
        for index,(label, action) in enumerate(actions):
            style = discord.ButtonStyle.success if action in ('register','approve','withdraw','sell') else discord.ButtonStyle.danger if action == 'reject' else discord.ButtonStyle.secondary
            button = discord.ui.Button(label=label, custom_id=f'bukowski:{kind}:{record or 0}:{action}', style=style,row=0 if index < 3 else 1)
            async def callback(interaction, action=action):
                try:
                    await self.bot.action(interaction, action, self.record)
                except (DomainError, ValueError, InvalidOperation) as error:
                    await answer(interaction,str(error))
                except Exception:
                    log.exception('Falha no botão %s',action)
                    await answer(interaction,'Operação não concluída. Consulte a gerência antes de tentar novamente.')
            button.callback = callback
            self.add_item(button)
        if kind == 'admin':
            menu = discord.ui.Select(placeholder='Outros registros e revisões',custom_id='bukowski:admin:operations',row=2,options=[
                discord.SelectOption(label='Cadastros e apelidos pendentes',value='registrations'),
                discord.SelectOption(label='Tentar completar contratação',value='retry_registration'),
                discord.SelectOption(label='Sincronizar apelido',value='sync_nickname'),
                discord.SelectOption(label='Cancelar retirada com motivo',value='cancel_withdrawal'),
                discord.SelectOption(label='Ajustar caixa com motivo',value='adjust_cash'),
                discord.SelectOption(label='Estornar lançamento',value='reverse_cash'),
                discord.SelectOption(label='Consultar auditoria',value='audit_records')])
            async def selected(interaction):
                try:
                    await self.bot.action(interaction,menu.values[0])
                except (DomainError,ValueError,InvalidOperation) as error:
                    await answer(interaction,str(error))
                except Exception:
                    log.exception('Falha no menu administrativo')
                    await answer(interaction,'Operação não concluída. Consulte a gerência.')
            menu.callback = selected
            self.add_item(menu)

class Tree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if interaction.guild_id != self.client.config.guild:
            await answer(interaction,'Este bot atende somente ao servidor configurado.')
            return False
        return True

class Bukowski(discord.Client):
    def __init__(self, config, reader=None):
        intents = discord.Intents.none()
        intents.guilds = intents.members = intents.guild_messages = intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,users=False))
        self.config = config
        self.store = Store(config.database,config.admins,config.finance)
        self.brand = Brand(config)
        if config.ocr_backend == 'gemini':
            from .vision import GeminiReader
            self.reader = reader or GeminiReader(config.vision_key,config.vision_model)
        else:
            self.reader = reader or TesseractReader(config.ocr_lang,config.tesseract)
        self.tree = Tree(self)
        self.install_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.ocr_slots = asyncio.Semaphore(2)
        self.nickname_locks = {}
        self.recovered_processing = False
        self.commands()

    async def actor(self, interaction):
        if interaction.guild_id != self.config.guild:
            raise DomainError('Servidor não autorizado.')
        self.validate_channels(interaction.guild)
        member = await interaction.guild.fetch_member(interaction.user.id)
        return Actor(member.id,frozenset(r.id for r in member.roles))

    async def setup_hook(self):
        for kind in ('service','work','admin'):
            self.add_view(Panel(self,kind))
        for r in self.store.rows("SELECT id FROM registrations WHERE status='pending'"):
            self.add_view(Panel(self,'registration',r['id']))
        for r in self.store.rows("SELECT q.id FROM quotes q JOIN withdrawals w ON w.id=q.withdrawal WHERE w.state='open'"):
            self.add_view(Panel(self,'quote',r['id']))
        guild = discord.Object(id=self.config.guild)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.restart_watch.start()

    async def on_ready(self):
        log.info('Intendente conectado; servidor %s', self.config.guild)
        try:
            self.validate_channels(self.get_guild(self.config.guild))
        except DomainError:
            log.exception('Configuração insegura: serviço será encerrado')
            await self.close()
            return
        async with self.scan_lock:
            # Recovery: replay current-period images; message uniqueness prevents double work.
            if not self.recovered_processing:
                for row in self.store.rows("SELECT id FROM tables WHERE state='processing'"):
                    self.store.db.execute("UPDATE tables SET state='pending',error_code='ocr_service',error='Processamento interrompido; valide manualmente' WHERE id=?",(row['id'],))
                self.recovered_processing = True
            channel = self.get_channel(self.config.channels['PRICES_CHANNEL'])
            if channel:
                try:
                    async for message in channel.history(limit=None,after=datetime.fromisoformat(period(now())),oldest_first=True):
                        await self.on_message(message)
                except discord.HTTPException:
                    log.exception('Não foi possível recuperar imagens do período')
        await self.refresh_panels()
        requests = self.get_channel(self.config.channels['REQUESTS_CHANNEL'])
        if requests:
            try:
                async for message in requests.history(limit=None):
                    if message.author.id != self.user.id or not message.embeds:
                        continue
                    match = re.fullmatch(r'(?:Solicitação #|DOCUMENTOS À MESA • #)(\d+)',message.embeds[0].title or '')
                    if match and self.store.one('SELECT id FROM registrations WHERE id=?',(int(match[1]),)):
                        self.store.db.execute('INSERT OR IGNORE INTO request_messages VALUES(?,?,?)',(int(match[1]),requests.id,message.id))
                        await self.update_request_messages(int(match[1]),message)
            except discord.HTTPException:
                log.warning('Solicitações antigas não puderam ser reconciliadas; use /solicitacao para pendentes')
        for row in self.store.rows("SELECT * FROM registrations WHERE status='approved'"):
            try:
                await self.sync_member(dict(row),Actor(self.user.id))
            except (DomainError,discord.HTTPException):
                log.warning('Apelido pendente no cadastro #%s; consulte /cadastros',row['id'])

    async def sync_member(self, row, actor, record=True):
        lock = self.nickname_locks.setdefault(row['user'],asyncio.Lock())
        async with lock:
            guild = self.get_guild(self.config.guild)
            try:
                member = await guild.fetch_member(row['user'])
                current = self.store.one('SELECT * FROM registrations WHERE id=?',(row['id'],))
                if current:
                    row = dict(current)
                target = nickname(row['name'],row['document'],{r.id for r in member.roles},self.config.role_rules(),self.config.nickname_format)
                if target is None:
                    if record:
                        self.store.identity_result(actor,row['id'],'nickname','unmapped','Nenhum cargo possui prefixo definido; apelido preservado.')
                    return None
                if member.nick != target:
                    bot_member = guild.me
                    if not bot_member.guild_permissions.manage_nicknames or member.id == guild.owner_id or member.top_role >= bot_member.top_role:
                        raise DomainError('Não posso alterar esse apelido. Confira Gerenciar Apelidos e a posição do bot acima do membro; o dono do servidor não pode ter o apelido alterado pelo bot.')
                    await member.edit(nick=target,reason=f'Sincronização do cadastro #{row["id"]}; responsável {actor.id}')
                if record:
                    latest = self.store.one('SELECT * FROM registrations WHERE id=?',(row['id'],))
                    if latest and (latest['name'],latest['document']) != (row['name'],row['document']):
                        raise DomainError('Cadastro mudou durante o ajuste do apelido; tente novamente.')
                    self.store.identity_result(actor,row['id'],'nickname','synced')
                return target
            except discord.Forbidden:
                error = DomainError('Discord recusou o apelido. Confira Gerenciar Apelidos e hierarquia.')
                if record:
                    self.store.identity_result(actor,row['id'],'nickname','failed',str(error))
                raise error
            except (DomainError,discord.HTTPException) as error:
                if record:
                    self.store.identity_result(actor,row['id'],'nickname','failed',str(error) if isinstance(error,DomainError) else 'Falha na API Discord; tente novamente.')
                raise

    async def on_member_update(self, before, after):
        if after.guild.id != self.config.guild or {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        row = self.store.one("SELECT * FROM registrations WHERE user=? AND status='approved'",(after.id,))
        if row:
            try:
                await self.sync_member(dict(row),Actor(self.user.id))
            except (DomainError,discord.HTTPException):
                await self.update_request_messages(row['id'])
                log.warning('Falha de sincronização de apelido; cadastro #%s',row['id'])

    async def update_request_messages(self, registration, source=None):
        row = self.store.one('SELECT * FROM registrations WHERE id=?',(registration,))
        if source is not None:
            self.store.db.execute('INSERT OR IGNORE INTO request_messages VALUES(?,?,?)',(registration,source.channel.id,source.id))
        for saved in self.store.rows('SELECT * FROM request_messages WHERE registration=?',(registration,)):
            try:
                message = source if source and source.id == saved['message'] else await self.get_channel(saved['channel']).fetch_message(saved['message'])
                avatar = message.embeds[0].thumbnail.url if message.embeds and message.embeds[0].thumbnail else None
                view = Panel(self,'registration',registration)
                if row['status'] != 'pending':
                    for button in view.children:
                        button.disabled = True
                await message.edit(embed=self.brand.registration(dict(row),avatar),view=view)
            except (discord.HTTPException,AttributeError):
                log.warning('Não foi possível atualizar solicitação #%s',registration)

    async def refresh_panels(self):
        async with self.install_lock:
            for row in self.store.rows('SELECT * FROM panels'):
                try:
                    channel = self.get_channel(row['channel'])
                    message = await channel.fetch_message(row['message'])
                    embed = self.brand.panel(row['kind'],self.store)
                    path = self.brand.panel_asset(row['kind'])
                    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path else ''
                    known_digest = self.store.get_setting('panel_art:'+row['kind']) or ''
                    needs_art = digest != known_digest or (path and not any(a.filename == f"bukowski-{row['kind']}.png" for a in message.attachments))
                    if not message.embeds or not self.brand.same_panel(message.embeds[0],embed) or message.content or needs_art:
                        attachments = self.brand.panel_files(row['kind']) if needs_art else list(message.attachments)
                        try:
                            await message.edit(content=None,embed=embed,view=Panel(self,row['kind']),attachments=attachments)
                        finally:
                            for attachment in attachments:
                                if isinstance(attachment,discord.File):
                                    attachment.close()
                        self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('panel_art:'+row['kind'],digest))
                except (discord.HTTPException,AttributeError):
                    log.warning('Painel %s indisponível; /instalar permite recuperá-lo',row['kind'])

    async def notify(self, user_id, text):
        try:
            user = await self.fetch_user(user_id)
            await user.send(text)
        except discord.HTTPException:
            channel = self.get_channel(self.config.channels['SERVICE_CHANNEL'])
            try:
                await channel.send(f'<@{user_id}> Há uma atualização no seu cadastro. Use “Consultar meu cadastro” no painel.',allowed_mentions=discord.AllowedMentions(users=True))
            except (discord.HTTPException, AttributeError):
                log.warning('Notificação indisponível; cadastro consultável no painel')

    async def publish_request(self, registration):
        r = self.store.one('SELECT * FROM registrations WHERE id=?',(registration,))
        user = await self.fetch_user(r['user'])
        embed = self.brand.registration(dict(r),user.display_avatar.url)
        message = await self.get_channel(self.config.channels['REQUESTS_CHANNEL']).send(embed=embed,view=Panel(self,'registration',registration))
        self.store.db.execute('INSERT OR IGNORE INTO request_messages VALUES(?,?,?)',(registration,message.channel.id,message.id))
        await self.refresh_panels()

    async def form(self, interaction, title, labels, action):
        await interaction.response.send_modal(Form(self,title,labels,action))

    async def action(self, i, action, record=None):
        a = await self.actor(i)
        s = self.store
        if action == 'register':
            async def submit(i,a,v):
                registration = s.register(a,*v)
                try:
                    await self.publish_request(registration)
                except discord.HTTPException:
                    await answer(i,f'Solicitação #{registration} salva. Falha ao publicar; a gerência pode consultar /cadastros e republicar /solicitacao.')
                    return
                await answer(i,f'Solicitação #{registration} registrada. Aguarde revisão da gerência.')
            await self.form(i,'Livro de registro',[('Nome do personagem',False),('ID/documento no jogo',False),('Pombo/correio',False)],submit)
        elif action == 'status':
            r = s.one('SELECT * FROM registrations WHERE user=? ORDER BY id DESC LIMIT 1',(a.id,))
            await answer(i, f"Cadastro #{r['id']}: {STATES.get(r['status'],r['status'])}.\nPeão: {SYNC_STATES.get(r['role_state'],r['role_state'])}. Apelido: {SYNC_STATES.get(r['nickname_state'],r['nickname_state'])}.\n{r['sync_error'] or r['reason'] or ''}" if r else 'Nenhum cadastro registrado.')
        elif action == 'approve':
            s.admin(a)
            if not i.response.is_done():
                await i.response.defer(ephemeral=True)
            async def grant(user):
                member = await i.guild.fetch_member(user)
                role = i.guild.get_role(self.config.peao)
                if role is None:
                    raise DomainError('Cargo Peão não encontrado.')
                if role not in member.roles:
                    if not i.guild.me.guild_permissions.manage_roles or role >= i.guild.me.top_role:
                        raise DomainError('Confira Gerenciar Cargos e posição do bot acima de Peão.')
                    await member.add_roles(role,reason=f'Cadastro #{record} aprovado por {a.id}')
            try:
                user = await s.approve(a,record,grant,lambda row:self.sync_member(row,a,record=False))
            finally:
                await self.update_request_messages(record,getattr(i,'message',None))
                await self.refresh_panels()
            await self.notify(user,'Seu registro na Fazenda Bukowski foi aprovado. Cargo Peão e apelido atualizados.')
            await answer(i,'Registro aprovado. Peão concedido e apelido sincronizado.')
        elif action == 'reject':
            s.admin(a)
            source = getattr(i,'message',None)
            async def submit(i,a,v):
                user = await s.reject(a,record,v[0])
                await self.update_request_messages(record,source)
                await self.refresh_panels()
                await self.notify(user,f'Seu registro foi recusado. Motivo: {v[0]}')
                await answer(i,'Solicitação recusada com motivo registrado.')
            await self.form(i,'Recusar registro',[('Motivo',True)],submit)
        elif action == 'withdraw':
            s.approved(a)
            async def submit(i,a,v):
                wid = s.withdraw(a,v[0],int(v[1]),i.id)
                await answer(i,f'Retirada #{wid} registrada. Consulte “Retiradas abertas”.')
            await self.form(i,'Retirada de sementes',[('Produto da tabela vigente',False),('Quantidade inteira de sementes',False)],submit)
        elif action == 'open':
            s.approved(a)
            rows = s.rows("SELECT * FROM withdrawals WHERE user=? AND state='open' ORDER BY id",(a.id,))
            await answer(i,'\n'.join(f"#{r['id']} — {r['product']}: {r['seeds']} sementes × {r['yield']}; taxa {r['rate']}%" for r in rows) or 'Nenhuma retirada aberta.')
        elif action == 'quote':
            s.approved(a)
            async def submit(i,a,v):
                q = s.quote(a,int(v[0]))
                await answer(i,'Confira o acerto antes de confirmar.',embed=self.brand.quote(q),view=Panel(self,'quote',q['id']))
            await self.form(i,'Calcular venda',[('ID da retirada aberta',False)],submit)
        elif action == 'sell':
            payment = s.sell(a,record)
            await self.refresh_panels()
            await answer(i,f'Venda calculada registrada. Repasse #{payment} pendente; informe a entrega após entregar o dinheiro.',embed=self.payment_embed(payment))
        elif action == 'pending':
            await answer(i,self.pending_text(a.id))
        elif action == 'report':
            async def submit(i,a,v):
                receiver = await i.guild.fetch_member(int(v[1]))
                if not self.config.finance.intersection(r.id for r in receiver.roles):
                    raise DomainError('Quem recebeu precisa ter cargo financeiro autorizado.')
                s.report_payment(a,int(v[0]),receiver.id)
                await self.refresh_panels()
                await answer(i,'Entrega informada. O caixa só será creditado após confirmação da gerência.',embed=self.payment_embed(int(v[0])))
                try:
                    await self.get_channel(self.config.channels['LOG_CHANNEL']).send(embed=self.payment_embed(int(v[0])))
                except discord.HTTPException:
                    log.exception('Aviso de entrega falhou; pendência permanece consultável')
            await self.form(i,'Informar entrega',[('ID do repasse',False),('ID Discord de quem recebeu',False)],submit)
        elif action == 'rate':
            s.admin(a)
            async def submit(i,a,v):
                s.set_rate(a,v[0]); await self.refresh_panels(); await answer(i,'Taxa configurada para novas retiradas.')
            await self.form(i,'Taxa da fazenda',[('Percentual de 0 a 100',False)],submit)
        elif action == 'yield':
            s.admin(a)
            async def submit(i,a,v):
                s.set_yield(a,v[0],int(v[1])); await answer(i,'Rendimento configurado para novas retiradas.')
            await self.form(i,'Rendimento',[('Produto',False),('Unidades por semente',False)],submit)
        elif action == 'table':
            s.admin(a)
            async def submit(i,a,v):
                tid = s.manual_table(a,parse_prices(v[0]),v[1],int(v[2]) if v[2] else None)
                await self.refresh_panels()
                await answer(i,f'Tabela #{tid} validada com histórico.')
            form = Form(self,'Tabela manual', [('Produto: $0.10 (uma linha por produto)',True),('Motivo da correção/publicação',True),('ID pendente; vazio para nova tabela',False)],submit)
            form.children[2].required = False
            await i.response.send_modal(form)
        elif action == 'finance':
            s.cashier(a)
            await answer(i,f'Saldo recebido: {money(s.balance(a))}\n'+self.pending_text())
        elif action == 'receive':
            s.cashier(a)
            async def submit(i,a,v):
                if v[1].casefold() not in ('confirmar','rejeitar'):
                    raise DomainError('Digite confirmar ou rejeitar.')
                s.receive(a,int(v[0]),v[1].casefold()=='confirmar',v[2])
                await self.refresh_panels()
                await answer(i,'Decisão financeira registrada.',embed=self.payment_embed(int(v[0])))
            await self.form(i,'Recebimento',[('ID do repasse',False),('confirmar ou rejeitar',False),('Motivo (obrigatório para rejeitar)',True)],submit)
        elif action == 'registrations':
            s.admin(a)
            rows = s.rows("SELECT * FROM registrations WHERE status='pending' OR nickname_state='failed' ORDER BY id")
            await answer(i,'\n'.join(f"#{r['id']} — {r['name']} | documento {r['document']} | pombo {r['mail']}\n{r['sync_error'] or 'Aguardando decisão'}" for r in rows) or 'Nenhum cadastro ou apelido pendente.')
        elif action in ('retry_registration','sync_nickname'):
            s.admin(a)
            async def submit(i,a,v):
                rid = int(v[0])
                if action == 'retry_registration':
                    await self.action(i,'approve',rid)
                    return
                row = s.one("SELECT * FROM registrations WHERE id=? AND status='approved'",(rid,))
                if not row:
                    raise DomainError('Cadastro aprovado não encontrado.')
                target = await self.sync_member(dict(row),a)
                await self.update_request_messages(rid)
                await answer(i,f'Apelido sincronizado: {target}' if target else 'Cargo sem prefixo; apelido preservado.')
            await self.form(i,'Revisar identidade',[('ID do cadastro',False)],submit)
        elif action == 'cancel_withdrawal':
            s.admin(a)
            async def submit(i,a,v):
                s.cancel_withdrawal(a,int(v[0]),v[1])
                await answer(i,'Retirada cancelada com histórico.')
            await self.form(i,'Revisar retirada',[('ID da retirada',False),('Motivo',True)],submit)
        elif action in ('adjust_cash','reverse_cash'):
            s.cashier(a)
            async def submit(i,a,v):
                if action == 'reverse_cash':
                    lid = s.adjust(a,0,v[1],reverse=int(v[0]))
                else:
                    amount = Decimal(v[0].replace(',','.'))
                    if not amount.is_finite() or amount != amount.quantize(Decimal('.01')):
                        raise DomainError('Informe valor com até duas casas decimais.')
                    lid = s.adjust(a,int(amount*100),v[1])
                await self.refresh_panels()
                await answer(i,f'Lançamento #{lid} registrado com histórico.')
            await self.form(i,'Revisar caixa',[('ID do lançamento' if action=='reverse_cash' else 'Valor assinado (ex.: -10,00)',False),('Motivo',True)],submit)
        elif action == 'audit_records':
            s.admin(a)
            await answer(i,'\n'.join(f"#{r['id']} | {r['created']} | {r['actor']} | {r['event']} | {r['payload']}" for r in s.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 8')) or 'Sem eventos.')

    def pending_text(self, user=None):
        rows = self.store.rows("SELECT * FROM payments WHERE state!='received'" + (' AND user=?' if user else '') + ' ORDER BY id',(user,) if user else ())
        return '\n'.join(f"#{r['id']} — funcionário {r['user']}: {money(r['amount'])}; {'Pendente' if r['state']=='pending' else STATES.get(r['state'],r['state'])}" for r in rows) or 'Nenhum repasse pendente.'

    def payment_embed(self, payment):
        row = self.store.one('SELECT p.*,s.withdrawal,s.price,s.total,s.farm,s.employee,w.product,w.seeds,w.yield,w.rate FROM payments p JOIN sales s ON s.id=p.sale JOIN withdrawals w ON w.id=s.withdrawal WHERE p.id=?',(payment,))
        q = dict(row); q['units'] = q['seeds']*q['yield']
        embed = self.brand.quote(q)
        embed.title = f'REPASSE NO LIVRO • #{payment}'
        embed.description = 'Pendente' if row['state']=='pending' else STATES.get(row['state'],row['state'])
        embed.remove_field(9)
        self.brand.field(embed,'Funcionário',f"<@{row['user']}>",True)
        if row['receiver']:
            self.brand.field(embed,'Entrega informada a',f"<@{row['receiver']}>",True)
        if row['confirmed_by']:
            self.brand.field(embed,'Confirmado por',f"<@{row['confirmed_by']}> • {row['confirmed_at']}")
        self.brand.field(embed,'Caixa','Crédito confirmado.' if row['state']=='received' else 'Ainda não houve crédito no caixa.')
        return embed

    async def on_message(self, message):
        await self.process_prices(message)

    async def process_prices(self, message, retry=None):
        if message.author.bot or not message.guild or message.guild.id != self.config.guild or message.channel.id != self.config.channels['PRICES_CHANNEL']:
            return
        attachments = [a for a in message.attachments if (a.content_type or '').startswith('image/') or a.filename.lower().endswith(('.png','.jpg','.jpeg','.webp'))]
        if not attachments:
            if retry is not None:
                raise DomainError('A mensagem não possui mais a imagem original.')
            return
        try:
            self.validate_channels(message.guild)
        except DomainError:
            log.exception('Leitura bloqueada por configuração insegura')
            if retry is not None:
                raise
            return
        member = await message.guild.fetch_member(message.author.id)
        actor = Actor(member.id,frozenset(r.id for r in member.roles))
        tid = retry if retry is not None else self.store.begin_table(message.id,actor,json.dumps([{'id':a.id,'url':a.url,'filename':a.filename} for a in attachments]),message.created_at)
        if tid is None:
            return
        raw, prices, error = '', {}, None
        error_code, partial, diagnostic = None,{},''
        try:
            if len(attachments) != 1 or attachments[0].size > 10_000_000:
                raise ReadingError('unsupported_format','Envie uma única imagem de até 10 MB por mensagem.')
            async with self.ocr_slots:
                try:
                    data = await attachments[0].read()
                except (discord.HTTPException,OSError,asyncio.TimeoutError):
                    raise ReadingError('attachment_download','Não foi possível baixar o anexo. Envie a imagem novamente.')
                raw, prices = await asyncio.to_thread(self.reader.read,data)
        except ReadingError as exc:
            error = str(exc)[:1000]
            raw = getattr(exc,'raw_text','')
            error_code,partial,diagnostic = exc.code,exc.partial,exc.diagnostic
            prices = partial
        except Exception as exc:
            error_code = 'ocr_service'
            error = 'Falha inesperada no serviço de leitura. Tente novamente ou solicite revisão.'
            diagnostic = type(exc).__name__  # No keys, request URLs or arbitrary exception bodies.
        state = self.store.finish_table(tid,actor,raw,prices,error)
        self.store.db.execute('UPDATE tables SET error_code=? WHERE id=?',(error_code,tid))
        row = dict(self.store.one('SELECT * FROM tables WHERE id=?',(tid,)))
        try:
            if state == 'valid':
                embed = self.brand.prices(row,prices,message.jump_url)
            else:
                detail = error or ('O período da publicação encerrou; publique uma nova imagem.' if state == 'expired' else 'Leitura concluída, mas o autor não possui cargo autorizado para ativar a tabela.')
                embed = self.brand.reading_issue(tid,error_code,detail)
            await message.reply(embed=embed,mention_author=False)
            if state == 'pending':
                admin_embed = self.brand.reading_issue(tid,error_code,error or 'Autor sem cargo administrativo.',partial or prices)
                self.brand.field(admin_embed,'Diagnóstico privado',f'Categoria: {error_code or "author_unauthorized"}\n{diagnostic}\n{raw[:700]}')
                self.brand.field(admin_embed,'Mensagem original',message.jump_url)
                await self.get_channel(self.config.channels['LOG_CHANNEL']).send(embed=admin_embed)
        except discord.HTTPException:
            log.exception('Resposta da tabela falhou; extração foi persistida')
        await self.refresh_panels()

    @tasks.loop(seconds=30)
    async def restart_watch(self):
        await self.refresh_panels()
        p = period(now())
        if self.store.one('SELECT period FROM notices WHERE period=?',(p,)):
            return
        channel = self.get_channel(self.config.channels['ANNOUNCEMENTS_CHANNEL'])
        if channel is None:
            return
        marker = f'Período {p}'
        # Reconcile a send interrupted before persisting its message ID.
        try:
            async for msg in channel.history(limit=None,after=datetime.fromisoformat(p)):
                if msg.author.id == self.user.id and marker in msg.content:
                    self.store.db.execute("INSERT OR IGNORE INTO notices VALUES(?,'sent',?)",(p,msg.id))
                    return
            try:
                self.store.current_table()
                status = 'Tabela vigente disponível em /precos.'
            except DomainError:
                status = 'Aguardando nova tabela em alta.'
            msg = await channel.send(f'{marker}\n{status} Reinício: preços anteriores não valem para novos registros; dívidas e vendas registradas permanecem.')
            self.store.db.execute("INSERT OR IGNORE INTO notices VALUES(?,'sent',?)",(p,msg.id))
        except discord.HTTPException:
            log.exception('Aviso do período indisponível; será tentado novamente')

    @restart_watch.before_loop
    async def before_watch(self):
        await self.wait_until_ready()

    async def on_member_join(self, member):
        if member.guild.id != self.config.guild:
            return
        phrases = ['Um novo forasteiro cruzou as porteiras da Fazenda Bukowski.','Há passos novos na estrada. Identifique-se no atendimento, forasteiro.','As porteiras se abriram para mais um viajante. Respeite esta terra e sua gente.']
        embed = self.brand.arrival(member,random.choice(phrases))
        await self.get_channel(self.config.channels['ARRIVALS_CHANNEL']).send(embed=embed)

    async def on_member_remove(self, member):
        if member.guild.id != self.config.guild:
            return
        with self.store.tx():
            self.store.db.execute("UPDATE registrations SET status='departed' WHERE user=? AND status IN ('approved','pending')",(member.id,))
            self.store.audit(Actor(member.id),'membro_saiu',{'type':'saída'})
        phrases = ['Um nome foi riscado do livro da Fazenda Bukowski. Sua estrada agora segue longe destas porteiras.','Mais um deixou nosso círculo. A fazenda segue, e o livro guarda sua passagem.','As porteiras se fecharam para este forasteiro. Seu vínculo com a fazenda chegou ao fim.']
        # Conservatively reports departure: no inference of kick/ban from leaving.
        embed = self.brand.arrival(member,random.choice(phrases),leaving=True)
        await self.get_channel(self.config.channels['ARRIVALS_CHANNEL']).send(embed=embed)

    def validate_channels(self, guild):
        if guild is None:
            raise DomainError('Servidor configurado não encontrado.')
        for key, ident in self.config.channels.items():
            channel = guild.get_channel(ident)
            expected = discord.CategoryChannel if key == 'PUBLIC_CATEGORY' else discord.TextChannel
            if not isinstance(channel,expected):
                raise DomainError(f'{key}: canal/categoria não encontrado no servidor configurado.')
        for key in ('REQUESTS_CHANNEL','LOG_CHANNEL','WORK_CHANNEL'):
            ch = guild.get_channel(self.config.channels[key])
            if ch.permissions_for(guild.default_role).view_channel:
                raise DomainError(f'{key}: negue Ver canal a @everyone antes de iniciar.')
        role = guild.get_role(self.config.peao)
        if not role or role.is_default() or role.managed:
            raise DomainError('Peão deve ser um cargo comum válido.')
        if role.permissions.administrator:
            raise DomainError('Peão não deve possuir permissão Administrador.')
        for key in ('REQUESTS_CHANNEL','LOG_CHANNEL'):
            if guild.get_channel(self.config.channels[key]).permissions_for(role).view_channel:
                raise DomainError(f'{key}: Peão não pode visualizar informações administrativas.')

    async def install(self, i):
        actor = await self.actor(i)
        self.store.admin(actor)
        await i.response.defer(ephemeral=True)
        # Fail closed for personal data and member-only work. Never mutate channels.
        guild = i.guild
        self.validate_channels(guild)
        async with self.install_lock:
            for kind,key,text in [('service','SERVICE_CHANNEL','Livro da Fazenda Bukowski — solicite registro ou consulte seu cadastro.'),('work','WORK_CHANNEL','Painel de trabalho — retiradas, vendas calculadas e repasses.'),('admin','LOG_CHANNEL','Gerência — taxas, rendimentos, tabelas e financeiro. Comandos adicionais: /cadastro_corrigir, /retirada_cancelar, /ajuste, /estorno, /auditoria.')]:
                channel = guild.get_channel(self.config.channels[key])
                embed = self.brand.panel(kind,self.store)
                row = self.store.one('SELECT * FROM panels WHERE kind=?',(kind,))
                msg = None
                if row:
                    if row['channel'] != channel.id:
                        raise DomainError('Canal do painel mudou. Revise a configuração; painéis não serão movidos automaticamente.')
                    try:
                        msg = await channel.fetch_message(row['message'])
                    except discord.NotFound:
                        pass
                if msg is None:
                    async for old in channel.history(limit=None):
                        if old.author.id == self.user.id and (old.content == text or (old.embeds and old.embeds[0].title == embed.title)):
                            msg = old
                            break
                if msg:
                    files = self.brand.panel_files(kind)
                    try:
                        await msg.edit(content=None,embed=embed,view=Panel(self,kind),attachments=files)
                    finally:
                        for file in files:
                            file.close()
                else:
                    files = self.brand.panel_files(kind)
                    try:
                        msg = await channel.send(embed=embed,view=Panel(self,kind),files=files)
                    finally:
                        for file in files:
                            file.close()
                self.store.db.execute('INSERT OR REPLACE INTO panels VALUES(?,?,?)',(kind,channel.id,msg.id))
                path = self.brand.panel_asset(kind)
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path else ''
                self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('panel_art:'+kind,digest))
            self.store.audit(actor,'paineis_instalados',{})
        await answer(i,'Painéis instalados/atualizados sem duplicação. Nenhum canal foi alterado.')

    def commands(self):
        tree, s = self.tree, self.store

        @tree.error
        async def command_error(i,error):
            root = getattr(error,'original',error)
            if isinstance(root,(DomainError,ValueError,InvalidOperation)):
                await answer(i,str(root))
            else:
                log.error('Falha no comando',exc_info=error)
                await answer(i,'Operação não concluída. Verifique os registros do serviço.')

        @tree.command(name='instalar',description='Instala ou atualiza painéis nos canais configurados')
        async def install(i:discord.Interaction):
            await self.install(i)

        taxa = app_commands.Group(name='taxa',description='Taxa destinada à fazenda')
        @taxa.command(name='definir',description='Define taxa para novas retiradas')
        async def rate_set(i:discord.Interaction,percentual:str):
            s.set_rate(await self.actor(i),percentual)
            await self.refresh_panels()
            await answer(i,'Taxa definida para novas retiradas.')
        @taxa.command(name='consultar',description='Consulta a taxa configurada')
        async def rate_get(i:discord.Interaction):
            await self.actor(i)
            rate = s.get_setting('rate')
            await answer(i,f'Taxa da fazenda: {rate}%' if rate is not None else 'Taxa da fazenda não configurada; retiradas bloqueadas.')
        tree.add_command(taxa)

        rendimento = app_commands.Group(name='rendimento',description='Produção por semente')
        @rendimento.command(name='definir',description='Define rendimento para novas retiradas')
        async def yield_set(i:discord.Interaction,produto:str,unidades_por_semente:int):
            s.set_yield(await self.actor(i),produto,unidades_por_semente)
            await answer(i,'Rendimento definido.')
        tree.add_command(rendimento)

        tabela = app_commands.Group(name='tabela',description='Correção auditável de preços')
        @tabela.command(name='manual',description='Abre formulário de publicação ou correção')
        async def table_manual(i:discord.Interaction):
            await self.action(i,'table')
        @tabela.command(name='reler',description='Tenta ler novamente uma imagem pendente do período atual')
        async def table_retry(i:discord.Interaction,tabela_id:int):
            a = await self.actor(i)
            s.admin(a)
            row = s.one('SELECT * FROM tables WHERE id=?',(tabela_id,))
            if not row or row['state'] != 'pending' or row['period'] != period(now()) or not row['message']:
                raise DomainError('Somente imagem pendente do período atual pode ser relida.')
            await i.response.defer(ephemeral=True)
            message = await self.get_channel(self.config.channels['PRICES_CHANNEL']).fetch_message(int(row['message']))
            with s.tx():
                changed = s.db.execute("UPDATE tables SET state='processing' WHERE id=? AND state='pending'",(tabela_id,))
                if not changed.rowcount:
                    raise DomainError('Tabela já está em processamento ou foi decidida.')
                s.audit(a,'tabela_releitura_solicitada',{'id':tabela_id,'before':{'raw':row['raw'],'error':row['error'],'prices':row['prices'],'state':row['state']}})
            try:
                await self.process_prices(message,retry=tabela_id)
            except Exception:
                s.db.execute("UPDATE tables SET state='pending',error='Releitura interrompida' WHERE id=? AND state='processing'",(tabela_id,))
                raise
            result = s.one('SELECT state,error FROM tables WHERE id=?',(tabela_id,))
            await answer(i,f"Releitura concluída: {result['state']}. {result['error'] or ''}")
        tree.add_command(tabela)

        @tree.command(name='precos',description='Consulta preços do período atual')
        async def prices(i:discord.Interaction):
            await self.actor(i)
            t,prices = s.current_table()
            await answer(i,f"Tabela #{t['id']} — período {t['period']}\n"+'\n'.join(f'{p}: {money(c)}' for p,c in prices.items()))

        @tree.command(name='tabelas',description='Lista tabelas recentes para revisão')
        async def tables(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} — {r['state']} — {r['published']} — {r['error_code'] or '—'} — {r['error'] or ''}" for r in s.rows('SELECT * FROM tables ORDER BY id DESC LIMIT 15')) or 'Nenhuma tabela.')

        @tree.command(name='ocr_diagnostico',description='Verifica executável e idiomas do OCR; resposta privada')
        async def ocr_diagnose(i:discord.Interaction):
            s.admin(await self.actor(i))
            await i.response.defer(ephemeral=True)
            result = await asyncio.to_thread(self.reader.diagnose)
            await answer(i,json.dumps(result,ensure_ascii=False))

        @tree.command(name='cadastro_tentar',description='Tenta completar cargo Peão e apelido de cadastro pendente')
        async def retry_registration(i:discord.Interaction,cadastro:int):
            await self.action(i,'approve',cadastro)

        @tree.command(name='apelido_sincronizar',description='Tenta novamente sincronizar apelido com cadastro e cargos atuais')
        async def nickname_sync(i:discord.Interaction,cadastro:int):
            actor = await self.actor(i)
            s.admin(actor)
            row = s.one("SELECT * FROM registrations WHERE id=? AND status='approved'",(cadastro,))
            if not row:
                raise DomainError('Cadastro aprovado não encontrado; pendentes usam /cadastro_tentar.')
            await i.response.defer(ephemeral=True)
            try:
                target = await self.sync_member(dict(row),actor)
            finally:
                await self.update_request_messages(cadastro)
            await answer(i,f'Apelido sincronizado: {target}' if target else 'Cargo sem prefixo configurado. Apelido atual preservado.')

        @tree.command(name='trabalho',description='Consulta retiradas abertas')
        async def work(i:discord.Interaction):
            await self.action(i,'open')

        @tree.command(name='retirar',description='Registra sementes')
        async def withdraw(i:discord.Interaction):
            await self.action(i,'withdraw')

        @tree.command(name='vender',description='Calcula e confirma venda de uma retirada')
        async def sell(i:discord.Interaction):
            await self.action(i,'quote')

        @tree.command(name='entrega',description='Informa dinheiro entregue à gerência')
        async def report(i:discord.Interaction):
            await self.action(i,'report')

        @tree.command(name='repasses',description='Consulta repasses pendentes próprios ou de funcionário')
        async def pending(i:discord.Interaction,funcionario:discord.Member|None=None):
            a = await self.actor(i)
            if funcionario and funcionario.id != a.id:
                s.cashier(a)
            await answer(i,self.pending_text(funcionario.id if funcionario else a.id))

        @tree.command(name='financeiro',description='Consulta caixa recebido e pendências')
        async def finance(i:discord.Interaction):
            await self.action(i,'finance')

        @tree.command(name='recebimento',description='Confirma ou rejeita uma entrega informada')
        async def receive(i:discord.Interaction,repasse:int,confirmar:bool,motivo:str=''):
            s.receive(await self.actor(i),repasse,confirmar,motivo)
            await self.refresh_panels()
            await answer(i,'Decisão financeira registrada.',embed=self.payment_embed(repasse))

        @tree.command(name='cadastros',description='Consulta cadastros pendentes privados')
        async def registrations(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} — Discord {r['user']} — {r['name']} — documento {r['document']} — pombo {r['mail']}\nPeão: {r['role_state']} | Apelido: {r['nickname_state']} | {r['sync_error'] or '—'}" for r in s.rows("SELECT * FROM registrations WHERE status='pending' OR nickname_state='failed' ORDER BY id")) or 'Nenhum cadastro ou apelido pendente.')

        @tree.command(name='solicitacao',description='Republica botões de uma solicitação pendente')
        async def request(i:discord.Interaction,cadastro:int):
            s.admin(await self.actor(i))
            r = s.one("SELECT id FROM registrations WHERE id=? AND status='pending'",(cadastro,))
            if not r:
                raise DomainError('Cadastro inexistente ou decidido.')
            await i.response.defer(ephemeral=True)
            await self.publish_request(cadastro)
            await answer(i,'Solicitação republicada. Botões antigos continuam sujeitos à decisão única.')

        @tree.command(name='cadastro_corrigir',description='Corrige cadastro preservando histórico')
        async def correct(i:discord.Interaction,cadastro:int,nome:str,documento:str,correio:str,motivo:str):
            actor = await self.actor(i)
            s.correct_registration(actor,cadastro,nome,documento,correio,motivo)
            await i.response.defer(ephemeral=True)
            row = s.one('SELECT * FROM registrations WHERE id=?',(cadastro,))
            try:
                if row['status'] == 'approved':
                    await self.sync_member(dict(row),actor)
            except (DomainError,discord.HTTPException):
                await answer(i,'Cadastro corrigido com histórico; apelido pendente. Confira /cadastros e use /apelido_sincronizar após resolver a hierarquia.')
            else:
                await answer(i,'Cadastro corrigido com histórico.' + (' Apelido sincronizado.' if row['status']=='approved' else ' O apelido será sincronizado na aprovação.'))
            await self.update_request_messages(cadastro)

        @tree.command(name='retirada_cancelar',description='Revisão administrativa de perda/cancelamento completo')
        async def cancel(i:discord.Interaction,retirada:int,motivo:str):
            s.cancel_withdrawal(await self.actor(i),retirada,motivo)
            await answer(i,'Retirada cancelada com histórico.')

        @tree.command(name='ajuste',description='Lançamento financeiro com motivo; valor assinado em dinheiro')
        async def adjust(i:discord.Interaction,valor:str,motivo:str):
            amount = Decimal(valor.replace(',','.'))
            if not amount.is_finite() or amount != amount.quantize(Decimal('.01')):
                raise DomainError('Informe dinheiro com no máximo duas casas decimais.')
            lid = s.adjust(await self.actor(i),int(amount*100),motivo)
            await self.refresh_panels()
            await answer(i,f'Ajuste #{lid} lançado: {money(int(amount*100))}.')

        @tree.command(name='estorno',description='Estorna uma entrada/ajuste uma única vez')
        async def reverse(i:discord.Interaction,lancamento:int,motivo:str):
            lid = s.adjust(await self.actor(i),0,motivo,reverse=lancamento)
            await self.refresh_panels()
            await answer(i,f'Estorno #{lid} registrado. A dívida original permanece no histórico; eventual nova cobrança exige revisão.')

        @tree.command(name='auditoria',description='Consulta os últimos eventos privados da auditoria')
        async def audit(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} {r['created']} | {r['actor']} | {r['event']} | {r['payload']}" for r in s.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 8')) or 'Sem eventos.')

    async def close(self):
        self.restart_watch.cancel()
        await super().close()
        self.store.db.close()
