import asyncio
import json
import logging
import random
from datetime import datetime
from decimal import Decimal, InvalidOperation
import discord
from discord import app_commands
from discord.ext import tasks
from .config import Config
from .store import Store, Actor, DomainError
from .periods import now, period
from .ocr import TesseractReader, parse_prices

log = logging.getLogger(__name__)

def money(cents):
    return f"${Decimal(cents)/100:.2f}"

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
            'service': [('Solicitar registro','register'),('Consultar meu cadastro','status')],
            'work': [('Retirar sementes','withdraw'),('Retiradas abertas','open'),('Registrar venda','quote'),('Repasses pendentes','pending'),('Informar entrega','report')],
            'admin': [('Taxa da fazenda','rate'),('Rendimento','yield'),('Corrigir tabela','table'),('Consultar pendências','finance'),('Confirmar ou rejeitar entrega','receive')],
            'registration': [('Aprovar','approve'),('Recusar','reject')],
            'quote': [('Confirmar venda calculada','sell')],
        }[kind]
        for label, action in actions:
            button = discord.ui.Button(label=label, custom_id=f'bukowski:{kind}:{record or 0}:{action}', style=discord.ButtonStyle.secondary)
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
        self.reader = reader or TesseractReader(config.ocr_lang,config.tesseract)
        self.tree = Tree(self)
        self.install_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.ocr_slots = asyncio.Semaphore(2)
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
            for row in self.store.rows("SELECT id FROM tables WHERE state='processing'"):
                self.store.db.execute("UPDATE tables SET state='pending',error='Processamento interrompido; valide manualmente' WHERE id=?",(row['id'],))
            channel = self.get_channel(self.config.channels['PRICES_CHANNEL'])
            if channel:
                try:
                    async for message in channel.history(limit=None,after=datetime.fromisoformat(period(now())),oldest_first=True):
                        await self.on_message(message)
                except discord.HTTPException:
                    log.exception('Não foi possível recuperar imagens do período')

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
        embed = discord.Embed(title=f'Solicitação #{registration}', description=f'<@{user.id}>',color=0x997133)
        embed.set_thumbnail(url=user.display_avatar.url)
        for label, value in [('Personagem',r['name']),('Documento',r['document']),('Pombo/correio',r['mail'])]:
            embed.add_field(name=label,value=discord.utils.escape_markdown(value))
        await self.get_channel(self.config.channels['REQUESTS_CHANNEL']).send(embed=embed,view=Panel(self,'registration',registration))

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
            await answer(i, f"Cadastro #{r['id']}: {r['status']}. Motivo: {r['reason'] or '—'}" if r else 'Nenhum cadastro registrado.')
        elif action == 'approve':
            s.admin(a)
            await i.response.defer(ephemeral=True)
            async def grant(user):
                member = await i.guild.fetch_member(user)
                role = i.guild.get_role(self.config.p1)
                if role is None:
                    raise DomainError('Cargo P1 não encontrado.')
                await member.add_roles(role,reason=f'Cadastro #{record} aprovado por {a.id}')
            user = await s.approve(a,record,grant)
            await self.notify(user,'Seu registro na Fazenda Bukowski foi aprovado. Cargo P1 concedido.')
            await answer(i,'Registro aprovado e cargo P1 concedido.')
        elif action == 'reject':
            s.admin(a)
            async def submit(i,a,v):
                user = await s.reject(a,record,v[0])
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
                await answer(i,f"Cálculo da retirada #{q['withdrawal']}: {q['units']} unidades × {money(q['price'])} = {money(q['total'])}.\nTaxa guardada: {q['rate']}%. Fazenda: {money(q['farm'])}. Funcionário: {money(q['employee'])}.\nEste cálculo usa sementes, rendimento e tabela; não comprova a venda no jogo. A confirmação fecha a retirada completa.",view=Panel(self,'quote',q['id']))
            await self.form(i,'Calcular venda',[('ID da retirada aberta',False)],submit)
        elif action == 'sell':
            payment = s.sell(a,record)
            await answer(i,f'Venda calculada registrada. Repasse #{payment} pendente; informe a entrega após entregar o dinheiro.')
        elif action == 'pending':
            await answer(i,self.pending_text(a.id))
        elif action == 'report':
            async def submit(i,a,v):
                receiver = await i.guild.fetch_member(int(v[1]))
                if not self.config.finance.intersection(r.id for r in receiver.roles):
                    raise DomainError('Quem recebeu precisa ter cargo financeiro autorizado.')
                s.report_payment(a,int(v[0]),receiver.id)
                await answer(i,'Entrega informada. O caixa só será creditado após confirmação da gerência.')
                try:
                    await self.get_channel(self.config.channels['LOG_CHANNEL']).send(f'Entrega informada: repasse #{v[0]}, funcionário <@{a.id}>, recebedor <@{receiver.id}>. Gerência: use /recebimento.')
                except discord.HTTPException:
                    log.exception('Aviso de entrega falhou; pendência permanece consultável')
            await self.form(i,'Informar entrega',[('ID do repasse',False),('ID Discord de quem recebeu',False)],submit)
        elif action == 'rate':
            s.admin(a)
            async def submit(i,a,v):
                s.set_rate(a,v[0]); await answer(i,'Taxa configurada para novas retiradas.')
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
                await answer(i,'Decisão financeira registrada.')
            await self.form(i,'Recebimento',[('ID do repasse',False),('confirmar ou rejeitar',False),('Motivo (obrigatório para rejeitar)',True)],submit)

    def pending_text(self, user=None):
        rows = self.store.rows("SELECT * FROM payments WHERE state!='received'" + (' AND user=?' if user else '') + ' ORDER BY id',(user,) if user else ())
        return '\n'.join(f"#{r['id']} — funcionário {r['user']}: {money(r['amount'])}; {r['state']}" for r in rows) or 'Nenhum repasse pendente.'

    async def on_message(self, message):
        if message.author.bot or not message.guild or message.guild.id != self.config.guild or message.channel.id != self.config.channels['PRICES_CHANNEL']:
            return
        attachments = [a for a in message.attachments if (a.content_type or '').startswith('image/') or a.filename.lower().endswith(('.png','.jpg','.jpeg','.webp'))]
        if not attachments:
            return
        try:
            self.validate_channels(message.guild)
        except DomainError:
            log.exception('Leitura bloqueada por configuração insegura')
            return
        member = await message.guild.fetch_member(message.author.id)
        actor = Actor(member.id,frozenset(r.id for r in member.roles))
        tid = self.store.begin_table(message.id,actor,json.dumps([{'id':a.id,'url':a.url,'filename':a.filename} for a in attachments]),message.created_at)
        if tid is None:
            return
        raw, prices, error = '', {}, None
        try:
            if len(attachments) != 1 or attachments[0].size > 10_000_000:
                raise ValueError('Envie uma única imagem de até 10 MB por mensagem.')
            async with self.ocr_slots:
                data = await attachments[0].read()
                raw, prices = await asyncio.to_thread(self.reader.read,data)
        except Exception as exc:
            error = str(exc)[:1000]
            raw = getattr(exc,'raw_text','')
        state = self.store.finish_table(tid,actor,raw,prices,error)
        table = '\n'.join(f'{p}: {money(c)}' for p,c in prices.items())
        try:
            await message.reply((f'Tabela #{tid}: {state}.\n' + (table if prices else 'Leitura não validada.') + '\nA vigência usa a publicação; o horário da captura no jogo não é comprovado.\n' + ('Gerência: corrija em /tabela manual.' if state != 'valid' else ''))[:1950],mention_author=False)
            if state == 'pending':
                await self.get_channel(self.config.channels['LOG_CHANNEL']).send(f'Tabela #{tid} pendente: {message.jump_url}. {error or "Autor sem cargo administrativo"}. Use /tabelas e /tabela manual.')
        except discord.HTTPException:
            log.exception('Resposta da tabela falhou; extração foi persistida')

    @tasks.loop(seconds=30)
    async def restart_watch(self):
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
        embed = discord.Embed(title='As porteiras se abrem',description=f'{member.mention}\n{random.choice(phrases)}\nSolicite seu registro em <#{self.config.channels["SERVICE_CHANNEL"]}>.')
        embed.set_thumbnail(url=member.display_avatar.url)
        await self.get_channel(self.config.channels['ARRIVALS_CHANNEL']).send(embed=embed)

    async def on_member_remove(self, member):
        if member.guild.id != self.config.guild:
            return
        with self.store.tx():
            self.store.db.execute("UPDATE registrations SET status='departed' WHERE user=? AND status IN ('approved','pending')",(member.id,))
            self.store.audit(Actor(member.id),'membro_saiu',{'type':'saída'})
        phrases = ['Um nome foi riscado do livro da Fazenda Bukowski. Sua estrada agora segue longe destas porteiras.','Mais um deixou nosso círculo. A fazenda segue, e o livro guarda sua passagem.','As porteiras se fecharam para este forasteiro. Seu vínculo com a fazenda chegou ao fim.']
        # Conservatively reports departure: no inference of kick/ban from leaving.
        embed = discord.Embed(title='Saída',description=f'{member.mention}\n{random.choice(phrases)}')
        embed.set_thumbnail(url=member.display_avatar.url)
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
        role = guild.get_role(self.config.p1)
        if not role or role.is_default() or role.managed:
            raise DomainError('P1 deve ser um cargo comum válido.')
        if role.permissions.administrator:
            raise DomainError('P1 não deve possuir permissão Administrador.')
        for key in ('REQUESTS_CHANNEL','LOG_CHANNEL'):
            if guild.get_channel(self.config.channels[key]).permissions_for(role).view_channel:
                raise DomainError(f'{key}: P1 não pode visualizar informações administrativas.')

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
                        if old.author.id == self.user.id and old.content == text:
                            msg = old
                            break
                if msg:
                    await msg.edit(content=text,view=Panel(self,kind))
                else:
                    msg = await channel.send(text,view=Panel(self,kind))
                self.store.db.execute('INSERT OR REPLACE INTO panels VALUES(?,?,?)',(kind,channel.id,msg.id))
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
            await answer(i,'Taxa definida para novas retiradas.')
        @taxa.command(name='consultar',description='Consulta a taxa configurada')
        async def rate_get(i:discord.Interaction):
            await self.actor(i)
            await answer(i,f"Taxa da fazenda: {s.get_setting('rate') or 'não configurada'}%")
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
        tree.add_command(tabela)

        @tree.command(name='precos',description='Consulta preços do período atual')
        async def prices(i:discord.Interaction):
            await self.actor(i)
            t,prices = s.current_table()
            await answer(i,f"Tabela #{t['id']} — período {t['period']}\n"+'\n'.join(f'{p}: {money(c)}' for p,c in prices.items()))

        @tree.command(name='tabelas',description='Lista tabelas recentes para revisão')
        async def tables(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} — {r['state']} — {r['published']} — {r['error'] or ''}" for r in s.rows('SELECT * FROM tables ORDER BY id DESC LIMIT 15')) or 'Nenhuma tabela.')

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
            await answer(i,'Decisão financeira registrada.')

        @tree.command(name='cadastros',description='Consulta cadastros pendentes privados')
        async def registrations(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} — Discord {r['user']} — {r['name']} — documento {r['document']} — correio {r['mail']}" for r in s.rows("SELECT * FROM registrations WHERE status='pending' ORDER BY id")) or 'Nenhum cadastro pendente.')

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
            s.correct_registration(await self.actor(i),cadastro,nome,documento,correio,motivo)
            await answer(i,'Cadastro corrigido com histórico.')

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
            await answer(i,f'Ajuste #{lid} lançado: {money(int(amount*100))}.')

        @tree.command(name='estorno',description='Estorna uma entrada/ajuste uma única vez')
        async def reverse(i:discord.Interaction,lancamento:int,motivo:str):
            lid = s.adjust(await self.actor(i),0,motivo,reverse=lancamento)
            await answer(i,f'Estorno #{lid} registrado. A dívida original permanece no histórico; eventual nova cobrança exige revisão.')

        @tree.command(name='auditoria',description='Consulta os últimos eventos privados da auditoria')
        async def audit(i:discord.Interaction):
            s.admin(await self.actor(i))
            await answer(i,'\n'.join(f"#{r['id']} {r['created']} | {r['actor']} | {r['event']} | {r['payload']}" for r in s.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 8')) or 'Sem eventos.')

    async def close(self):
        self.restart_watch.cancel()
        await super().close()
        self.store.db.close()
