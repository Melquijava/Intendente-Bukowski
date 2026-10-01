"""Identidade Bukowski: verde profundo, ouro envelhecido e linguagem de livro-caixa."""
from datetime import datetime
from decimal import Decimal
import discord
from .periods import TZ, next_restart
from .store import DomainError

GREEN = 0x173D2A
GOLD = 0xB39858
LEATHER = 0x75523B
FOOTER = 'Intendente Bukowski • Guardião da Fazenda'
STATES = {'pending':'Em análise','approved':'Aprovado','rejected':'Recusado','departed':'Vínculo encerrado','reported':'Entrega informada','received':'Recebido','valid':'Válida','expired':'Período encerrado','processing':'Em leitura'}
SYNC_STATES = {'not_attempted':'não tentado','granted':'concedido','failed':'falhou','synced':'sincronizado','pending':'pendente','unmapped':'prefixo não configurado'}

def money(cents):
    return f'${Decimal(cents)/100:.2f}'

def time_label(value):
    if isinstance(value,str):
        value = datetime.fromisoformat(value)
    return value.astimezone(TZ).strftime('%d/%m/%Y às %H:%M') + ' • Brasília'

class Brand:
    def __init__(self, config):
        self.config = config

    def embed(self, title, text='', color=GREEN, banner=False):
        e = discord.Embed(title=title[:256],description=text[:4096] or None,color=color)
        e.set_footer(text=FOOTER)
        if self.config.badge_url:
            e.set_thumbnail(url=self.config.badge_url)
        if banner and self.config.banner_url:
            e.set_image(url=self.config.banner_url)
        return e

    @staticmethod
    def field(e, name, value, inline=False):
        e.add_field(name=name[:256],value=str(value)[:1024] or '—',inline=inline)

    def panel(self, kind, store):
        if kind == 'service':
            e = self.embed('AS PORTEIRAS DA BUKOWSKI','A estrada trouxe você até nossas porteiras. Aqui, o nome tem valor e a palavra fica no livro.\nApresente-se à Fazenda Bukowski e aguarde a análise da administração.',banner=True)
            self.field(e,'O que trazer','Nome do personagem • Documento no jogo • Número do pombo.\nDocumento e pombo são informações diferentes; mantenha os zeros iniciais.')
            self.field(e,'Seu primeiro passo','Use **Apresentar documentos**. Seus dados seguem para a mesa privada da administração.')
        elif kind == 'work':
            e = self.embed('LIVRO DE LABUTA','Da semente ao repasse, cada etapa tem seu lugar no livro da fazenda.',banner=True)
            try:
                table,prices = store.current_table()
                self.field(e,'Cotações vigentes',f"Tabela #{table['id']} • válida neste período")
                listing = '\n'.join(f'**{p.title()}** — {money(c)}' for p,c in prices.items())
                self.field(e,'Produtos e preços',listing if len(listing)<=1000 else listing[:900]+'\nConsulte a lista completa em /precos.')
            except DomainError:
                self.field(e,'Cotações vigentes','Aguardando nova tabela em alta. Novas retiradas e cálculos de venda estão bloqueados.')
            self.field(e,'Próximo reinício',time_label(next_restart(store.clock())))
            self.field(e,'Como trabalhar','**1.** Registre as sementes retiradas.\n**2.** Confira e confirme a venda calculada.\n**3.** Entregue o repasse e informe quem recebeu.\nO caixa só recebe crédito após confirmação da gerência.')
        else:
            e = self.embed('MESA DO INTENDENTE','O livro guarda os nomes. O caixa guarda somente o dinheiro confirmado.',GOLD,banner=True)
            for title,query in [('Cadastros em análise',"SELECT COUNT(*) n FROM registrations WHERE status='pending'"),('Repasses pendentes',"SELECT COUNT(*) n FROM payments WHERE state='pending'"),('Entregas a confirmar',"SELECT COUNT(*) n FROM payments WHERE state='reported'")]:
                self.field(e,title,store.one(query)['n'],True)
            balance = store.one('SELECT COALESCE(SUM(amount),0) total FROM ledger')['total']
            self.field(e,'Caixa efetivamente recebido',money(balance),True)
            rate = store.get_setting('rate')
            self.field(e,'Taxa da fazenda',f'{rate}%' if rate is not None else 'Não configurada — retiradas bloqueadas',True)
            self.field(e,'Revisão e histórico','Cadastro e apelidos: /cadastros • /cadastro_tentar • /apelido_sincronizar\nPreços: /tabelas • /tabela reler • /tabela manual\nRevisões: /cadastro_corrigir • /retirada_cancelar • /ajuste • /estorno • /auditoria')
        return e

    def registration(self, row, avatar=None):
        e = self.embed(f"DOCUMENTOS À MESA • #{row['id']}",'Uma nova assinatura aguarda lugar no livro da Fazenda Bukowski.',GOLD)
        if avatar:
            e.set_thumbnail(url=avatar)
        for title,value in [('Personagem',row['name']),('Documento',row['document']),('Pombo',row['mail']),('Solicitante',f"<@{row['user']}>"),('Apresentado em',time_label(row['created'])),('Situação',STATES.get(row['status'],row['status']))]:
            self.field(e,title,discord.utils.escape_markdown(str(value)) if title in ('Personagem','Documento','Pombo') else value,True)
        if row.get('decider'):
            self.field(e,'Responsável',f"<@{row['decider']}> • {time_label(row['decided'])}")
        if row.get('reason'):
            self.field(e,'Motivo',row['reason'])
        if row.get('role_state') == 'granted' or row.get('sync_error'):
            self.field(e,'Cargo e apelido',f"Peão: {SYNC_STATES.get(row.get('role_state'),row.get('role_state'))} • Apelido: {SYNC_STATES.get(row.get('nickname_state'),row.get('nickname_state'))}\n{row.get('sync_error') or 'Operações concluídas.'}")
        return e

    def quote(self, q):
        e = self.embed('ACERTO DA LABUTA',f"Retirada #{q['withdrawal']} • confira os valores antes de fechar o lote.",GOLD)
        for title,value in [('Produto',q['product'].title()),('Sementes',q['seeds']),('Rendimento',f"{q['yield']} unidades/semente"),('Produção calculada',f"{q['units']} unidades"),('Preço vigente',money(q['price'])),('Total calculado',money(q['total'])),('Taxa da retirada',q['rate']+'%'),('Parte da fazenda',money(q['farm'])),('Parte do funcionário',money(q['employee']))]:
            self.field(e,title,value,True)
        self.field(e,'Antes de confirmar','A confirmação fecha a retirada completa. O cálculo usa sementes, rendimento e cotação; não comprova a venda no jogo.')
        return e

    def prices(self, table, prices, url=None):
        e = self.embed('COTAÇÕES DO CONDADO',f"Tabela #{table['id']} • {STATES.get(table['state'],table['state'])}",GOLD)
        for name,cents in list(prices.items())[:20]:
            self.field(e,name.title(),money(cents),True)
        self.field(e,'Publicação',f"<@{table['author']}> • {time_label(table['published'])}")
        self.field(e,'Validade',f"Até {time_label(next_restart(datetime.fromisoformat(table['published'])))}. O período usa a publicação no Discord.")
        self.field(e,'Origem',f'[Abrir publicação]({url})' if url else 'Atualização manual auditada.')
        self.field(e,'Registro fiel','A captura não comprova quando o print foi tirado no jogo.' + (' Consulte /precos para os demais produtos.' if len(prices)>20 else ''))
        return e

    def reading_issue(self, tid, code, message, prices=None):
        e = self.embed('COTAÇÕES SOB REVISÃO',f'Tabela #{tid} • nenhuma cotação parcial foi ativada.',LEATHER)
        self.field(e,'O que aconteceu',message)
        self.field(e,'Próximo passo','Administração: consulte /tabelas e tente /tabela reler ou corrija em /tabela manual.\nSe o período encerrou, publique uma nova imagem.')
        if prices:
            self.field(e,'Valores extraídos — não validados','\n'.join(f'{p.title()}: {money(v)}' for p,v in prices.items()))
        return e

    def arrival(self, member, phrase, leaving=False):
        title = 'UM FORASTEIRO ÀS PORTEIRAS' if not leaving else 'UM NOME DEIXA O LIVRO'
        e = self.embed(title,f'{member.mention}\n{phrase}',LEATHER if leaving else GREEN)
        e.set_thumbnail(url=member.display_avatar.url)
        self.field(e,'Saída' if leaving else 'Apresente-se', 'O histórico permanece no livro. O motivo da saída não foi atribuído.' if leaving else f"Registre nome, documento e pombo no atendimento <#{self.config.channels['SERVICE_CHANNEL']}>.")
        return e
